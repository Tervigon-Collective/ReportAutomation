"""
General Statistics ("dashboard") metrics for the marketing PDF top section.

Primary source is the backend endpoint
    GET {BASE_URL}/v1/historical/dashboard?brand_id&company_id&start_date&end_date
(the same data the General Statistics dashboard cards + channel-breakdown modal use).

When that endpoint is unreachable (e.g. not deployed at the configured BASE_URL),
we fall back to computing the identical payload directly from ClickHouse `gold`:
  - period totals  -> dashboard_master.sql  (the backend "master query")
  - channel split  -> per-order attribution net sales / orders / COGS + Meta/Google spend

Both paths return the same dict shape, and `build_pdf_api_metrics()` turns it into the
{meta, google, organic, total} structure consumed by
report_renderer.build_daily_pdf_context().
"""
from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_MASTER_SQL = _HERE / "dashboard_master.sql"

# Real (non-CTE) gold tables that need the `gold.` prefix on this cluster.
_REAL_TABLES = [
    "fct_orders", "fct_order_attribution", "fct_order_items", "fct_meta_ads_daily",
    "fct_google_ads_daily", "fct_amazon_ads_campaigns_daily",
    "fct_amazon_order_items", "fct_amazon_sp_order_pnl",
]

# Channel mapping used by the dashboard. Amazon is a separate marketplace, so for the
# Shopify channel table it is folded into 'organic' (alongside unattributed orders).
_CHANNEL_MAP = """multiIf(
  lowerUTF8(trimBoth(coalesce(a.lt_platform,''))) IN ('meta','facebook','instagram','fb','ig'),'meta',
  lowerUTF8(trimBoth(coalesce(a.lt_platform,''))) IN ('google','google_ads'),'google',
  'organic')"""


def _to_date_str(value: str | date | datetime) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


# --------------------------------------------------------------------------- API

def _fetch_from_api(brand_id: int, company_id: int, start: str, end: str) -> Optional[dict]:
    """Return the raw `data` object from the historical dashboard endpoint, or None."""
    try:
        from api_data_fetcher import fetch_historical_dashboard_cached
        return fetch_historical_dashboard_cached(start, end)
    except ImportError:
        return None


def _api_to_stats(data: dict) -> dict:
    """Normalise the API response into our internal stats dict (see _clickhouse_stats)."""
    def f(*keys, default=0.0):
        cur = data
        for k in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k, default if k == keys[-1] else {})
        try:
            return float(cur)
        except (TypeError, ValueError):
            return default

    ad = data.get("ad_spend_breakdown", {}) or {}
    amazon = data.get("amazon", {}) or {}
    rc = data.get("returns_cancels", {}) or {}

    def chan(breakdown_key, ch, sub=None):
        b = (data.get(breakdown_key, {}) or {}).get(ch, 0)
        if isinstance(b, dict):
            b = b.get(sub, 0) if sub else 0
        try:
            return float(b)
        except (TypeError, ValueError):
            return 0.0

    channels = {}
    for ch in ("meta", "google", "organic"):
        amz_spend = 0.0
        spend = chan("ad_spend_breakdown", ch)
        channels[ch] = {
            "sales": chan("net_sales_breakdown", ch),
            "cogs": chan("cogs_breakdown", ch),
            "ad_spend": spend if ch != "organic" else 0.0,
            "order_count": int(chan("orders_breakdown", ch)),
        }
    amz_ad = ad.get("amazon", {})
    amazon_spend = float(amz_ad.get("total", 0)) if isinstance(amz_ad, dict) else float(amz_ad or 0)

    return {
        "totals": {
            "net_sales": f("net_sales"),
            "gross_sales": f("gross_sales"),
            "total_cogs": f("total_cogs"),
            "total_ad_spend": f("total_ad_spend"),
            "total_orders": int(f("total_orders")),
            "net_profit": f("net_profit"),
            "returns_cancels": int(rc.get("total_count", 0) or 0),
            "cancelled_orders": int(rc.get("cancelled_count", 0) or 0),
            "returned_orders": int(rc.get("returned_count", 0) or 0),
            "cancelled_amount": float(rc.get("cancelled_amount", 0) or 0),
            "returned_amount": float(rc.get("returned_amount", 0) or 0),
            "returns_cancels_amount": float(rc.get("total_amount", 0) or 0),
            "amazon_net_revenue": float(amazon.get("net_sales", 0) or 0),
            "amazon_net_cogs": float(amazon.get("cogs", 0) or 0),
            "amazon_spend": amazon_spend,
            "amazon_orders": int(amazon.get("orders", 0) or 0),
        },
        "channels": channels,
        "source": "api",
    }


# -------------------------------------------------------------------- ClickHouse

def _prefixed_master_sql(hourly_spend: bool = False) -> str:
    sql = _MASTER_SQL.read_text()
    tables = list(_REAL_TABLES)
    if hourly_spend:
        # Single-day view: Meta/Google spend from the hourly tables (matches the dashboard's
        # single-day query exactly; daily vs hourly differs by a few rupees). Amazon stays daily.
        sql = sql.replace("FROM fct_meta_ads_daily", "FROM fct_meta_ads_hourly")
        sql = sql.replace("FROM fct_google_ads_daily", "FROM fct_google_campaigns_hourly")
        tables = tables + ["fct_meta_ads_hourly", "fct_google_campaigns_hourly"]
    for t in tables:
        sql = re.sub(r"(FROM|JOIN)\s+" + t + r"\b", r"\1 gold." + t, sql)
    return sql


def fetch_daily_net_profit_series(
    brand_id: int,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
):
    """
    Per-day net profit series for the net-profit chart, rolled up from `fct_order_items`
    (+ Meta/Google/Amazon ad-spend tables) via the master query — NOT from `fct_daily_pnl`.

    Returns a DataFrame with columns:
        sale_date (datetime), revenue (net sales), cogs (net COGS),
        total_ad_spend, net_profit
    one row per day in [start_date, end_date].
    """
    import pandas as pd
    from amazon_entity_report import get_clickhouse_client

    start, end = _to_date_str(start_date), _to_date_str(end_date)
    client = get_clickhouse_client()
    res = client.query(_prefixed_master_sql(),
                       parameters={"brandId": brand_id, "startDate": start, "endDate": end})
    df = pd.DataFrame(res.result_rows, columns=res.column_names)
    if df.empty:
        return pd.DataFrame(columns=["sale_date", "revenue", "cogs", "total_ad_spend", "net_profit"])
    out = pd.DataFrame({
        "sale_date": pd.to_datetime(df["report_date"]),
        "revenue": pd.to_numeric(df["net_sales"], errors="coerce").fillna(0),
        "cogs": pd.to_numeric(df["total_cogs"], errors="coerce").fillna(0),
        "total_ad_spend": pd.to_numeric(df["total_ad_spend"], errors="coerce").fillna(0),
        "net_profit": pd.to_numeric(df["net_profit"], errors="coerce").fillna(0),
    }).sort_values("sale_date").reset_index(drop=True)
    return out


def _clickhouse_stats(brand_id: int, start: str, end: str) -> dict:
    import pandas as pd
    from amazon_entity_report import get_clickhouse_client

    client = get_clickhouse_client()
    P = {"brandId": brand_id, "startDate": start, "endDate": end}
    single_day = start == end  # single-day view sources Meta/Google spend from hourly tables

    # --- period totals from the master query (sum daily rows in Python) ---
    res = client.query(_prefixed_master_sql(hourly_spend=single_day), parameters=P)
    df = pd.DataFrame(res.result_rows, columns=res.column_names).apply(pd.to_numeric, errors="coerce")
    t = df.sum()
    totals = {
        "net_sales": float(t["net_sales"]),
        "gross_sales": float(t["gross_sales"]),
        "total_cogs": float(t["total_cogs"]),
        "total_ad_spend": float(t["total_ad_spend"]),
        "total_orders": int(t["total_orders"]),
        "returns_cancels": int(t["returns_cancels"]),
        "cancelled_orders": int(t["cancelled_orders"]),
        "returned_orders": int(t["returned_orders"]),
        "cancelled_amount": float(t.get("cancelled_revenue_excl", 0) or 0),
        "returned_amount": float(t.get("returned_revenue_excl", 0) or 0),
        "returns_cancels_amount": float(t.get("cancelled_revenue_excl", 0) or 0) + float(t.get("returned_revenue_excl", 0) or 0),
        "amazon_net_revenue": float(t["amazon_net_revenue"]),
        "amazon_net_cogs": float(t["amazon_net_cogs"]),
        "amazon_spend": float(t["amazon_spend"]),
        "amazon_orders": int(t["amazon_orders"]),
    }
    totals["net_profit"] = round(
        totals["net_sales"] - totals["total_cogs"] - totals["total_ad_spend"], 2
    )

    # --- channel split: net sales + orders (per-order attribution, canonical rule) ---
    bp = {"b": brand_id, "s": start, "e": end}
    ns_sql = f"""
    WITH order_channel AS (
      SELECT a.brand_id, a.order_id, any({_CHANNEL_MAP}) AS channel
      FROM gold.fct_order_attribution a
      WHERE a.brand_id={{b:Int64}} AND a.order_date>=toDate({{s:String}}) AND a.order_date<=toDate({{e:String}})
        AND coalesce(a.is_test,0)=0 AND lowerUTF8(trimBoth(coalesce(a.order_status,'')))!='voided'
      GROUP BY a.brand_id, a.order_id),
    orders_dedup AS (
      SELECT brand_id, order_id, argMax(order_date,_loaded_at) order_date, argMax(order_status,_loaded_at) order_status,
        argMax(is_test,_loaded_at) is_test, argMax(is_revenue_adjustment,_loaded_at) is_rev_adj,
        toFloat64(argMax(net_revenue,_loaded_at)) nr, toFloat64(argMax(net_revenue_excl_tax,_loaded_at)) nret,
        toFloat64(argMax(gross_revenue,_loaded_at)) gr, toFloat64(argMax(gross_revenue_excl_tax,_loaded_at)) gret,
        toFloat64(argMax(total_discounts,_loaded_at)) td, toFloat64(argMax(total_tax,_loaded_at)) tt
      FROM gold.fct_orders WHERE brand_id={{b:Int64}} GROUP BY brand_id, order_id),
    base AS (
      SELECT coalesce(oc.channel,'organic') AS channel, o.order_id,
        if(o.nr>0 AND o.gret>o.nret, o.gret-o.nret, if(o.tt>0 AND o.gr>0, o.td*((o.gr-o.tt)/o.gr), o.td)) AS disc_excl,
        o.order_status, o.is_rev_adj, o.nr, o.nret, o.gret
      FROM orders_dedup o LEFT JOIN order_channel oc ON oc.brand_id=o.brand_id AND oc.order_id=o.order_id
      WHERE o.order_date>=toDate({{s:String}}) AND o.order_date<=toDate({{e:String}})
        AND coalesce(o.is_test,0)=0 AND lowerUTF8(trimBoth(coalesce(o.order_status,'')))!='voided')
    SELECT channel, toInt64(count()) AS orders,
      round(sum(if(lowerUTF8(trimBoth(coalesce(order_status,'')))='cancelled',0,
        if(is_rev_adj=1,0,if(nr>0,nret,greatest(0,gret-disc_excl))))),2) AS net_sales
    FROM base GROUP BY channel
    """
    ns = {r[0]: {"orders": int(r[1]), "net_sales": float(r[2])}
          for r in client.query(ns_sql, parameters=bp).result_rows}

    # --- channel COGS (net_cogs per line item, restricted to valid order universe) ---
    cogs_sql = f"""
    WITH order_channel AS (
      SELECT a.brand_id, a.order_id, any({_CHANNEL_MAP}) AS channel
      FROM gold.fct_order_attribution a
      WHERE a.brand_id={{b:Int64}} AND a.order_date>=toDate({{s:String}}) AND a.order_date<=toDate({{e:String}})
        AND coalesce(a.is_test,0)=0 AND lowerUTF8(trimBoth(coalesce(a.order_status,'')))!='voided'
      GROUP BY a.brand_id, a.order_id)
    SELECT coalesce(oc.channel,'organic') AS channel, round(sum(toFloat64(coalesce(i.net_cogs,0))),2) AS cogs
    FROM gold.fct_order_items i INNER JOIN order_channel oc ON oc.brand_id=i.brand_id AND oc.order_id=i.order_id
    WHERE i.brand_id={{b:Int64}} AND i.order_date>=toDate({{s:String}}) AND i.order_date<=toDate({{e:String}})
      AND coalesce(i.is_gift_card,0)=0
    GROUP BY coalesce(oc.channel,'organic')
    """
    cogs = {r[0]: float(r[1]) for r in client.query(cogs_sql, parameters=bp).result_rows}

    def _spend(table):
        q = (f"SELECT round(sum(toFloat64(spend)),2) FROM gold.{table} "
             "WHERE brand_id={b:Int64} AND report_date>=toDate({s:String}) AND report_date<=toDate({e:String})")
        v = client.query(q, parameters=bp).result_rows[0][0]
        return float(v or 0)

    if single_day:
        spend = {"meta": _spend("fct_meta_ads_hourly"), "google": _spend("fct_google_campaigns_hourly"), "organic": 0.0}
    else:
        spend = {"meta": _spend("fct_meta_ads_daily"), "google": _spend("fct_google_ads_daily"), "organic": 0.0}

    channels = {}
    for ch in ("meta", "google", "organic"):
        channels[ch] = {
            "sales": round(ns.get(ch, {}).get("net_sales", 0.0), 2),
            "order_count": ns.get(ch, {}).get("orders", 0),
            "cogs": round(cogs.get(ch, 0.0), 2),
            "ad_spend": round(spend.get(ch, 0.0), 2),
        }

    return {"totals": totals, "channels": channels, "source": "clickhouse"}


# --------------------------------------------------------------------------- API

def fetch_general_statistics(
    brand_id: int,
    company_id: int,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    prefer_api: bool = True,
) -> dict:
    """Return the General Statistics payload, API-first with a ClickHouse fallback."""
    start, end = _to_date_str(start_date), _to_date_str(end_date)
    api_only = os.getenv("USE_API_ONLY", "false").lower() in ("1", "true", "yes")
    if prefer_api or api_only:
        data = _fetch_from_api(brand_id, company_id, start, end)
        if data is not None:
            return _api_to_stats(data)
        if api_only:
            return {"totals": {}, "channels": {}, "source": "api_empty"}
    if api_only:
        return {"totals": {}, "channels": {}, "source": "api_only"}
    return _clickhouse_stats(brand_id, start, end)


def build_pdf_api_metrics(stats: dict) -> dict:
    """
    Convert fetch_general_statistics() output into the {meta, google, organic, amazon, total}
    structure that report_renderer.build_daily_pdf_context() expects.

    Channel rows are the attributed split (Meta/Google/Organic + Amazon marketplace).
    Total is the all-up General Statistics headline (same cards as the dashboard).

    Channel money columns will not equal Total on their own — build_daily_pdf_context
    adds a Returned/Cancelled deduction and an Other/Timing residual so:

        Meta + Google + Organic + Amazon + Returns + Residual  ==  Total
    """
    def _sd(n, d):
        return (n / d) if d else 0.0

    def _enrich(ch):
        s, ad, co, oc = ch["sales"], ch["ad_spend"], ch["cogs"], ch["order_count"]
        return {
            **ch,
            "net_profit": round(s - co - ad, 2),
            "gross_roas": round(_sd(s, ad), 2),
            "net_roas": round(_sd(s - co, ad), 2),
            "be_roas": round(_sd(co + ad, ad), 2),
            "cpp": round(_sd(ad, oc), 2),
            "quantity": oc,
        }

    ch = {k: _enrich(stats["channels"][k]) for k in ("meta", "google", "organic")}
    t = stats["totals"]
    # Amazon as its own channel row, sourced from the all-up totals' Amazon fields.
    amazon = _enrich({
        "sales": round(t.get("amazon_net_revenue", 0.0), 2),
        "ad_spend": round(t.get("amazon_spend", 0.0), 2),
        "cogs": round(t.get("amazon_net_cogs", 0.0), 2),
        "order_count": int(t.get("amazon_orders", 0)),
    })
    # All-up Total matches General Statistics cards:
    #   Net Profit = net_sales - total_cogs - total_ad_spend
    #   Blended ROAS (reported as gross_roas key for template) = net_sales / ad_spend
    #   Net ROAS = (net_sales - cogs) / ad_spend
    # Dashboard "Gross ROAS" (gross_sales / ad_spend) is kept as dashboard_gross_roas.
    total = {
        "sales": round(t["net_sales"], 2),
        "gross_sales": round(t.get("gross_sales", 0.0), 2),
        "ad_spend": round(t["total_ad_spend"], 2),
        "cogs": round(t["total_cogs"], 2),
        "net_profit": round(t["net_profit"], 2),
        "gross_roas": round(_sd(t["net_sales"], t["total_ad_spend"]), 2),
        "dashboard_gross_roas": round(_sd(t.get("gross_sales", 0.0), t["total_ad_spend"]), 2),
        "net_roas": round(_sd(t["net_sales"] - t["total_cogs"], t["total_ad_spend"]), 2),
        "be_roas": round(_sd(t["total_cogs"] + t["total_ad_spend"], t["total_ad_spend"]), 2),
        "order_count": int(t["total_orders"]),
        "quantity": int(t["total_orders"]),
        "cpp": round(_sd(t["total_ad_spend"], t["total_orders"]), 2),
        "returns_cancels": int(t.get("returns_cancels", 0) or 0),
        "cancelled_orders": int(t.get("cancelled_orders", 0) or 0),
        "returned_orders": int(t.get("returned_orders", 0) or 0),
        "cancelled_amount": round(float(t.get("cancelled_amount", 0) or 0), 2),
        "returned_amount": round(float(t.get("returned_amount", 0) or 0), 2),
        "returns_cancels_amount": round(float(t.get("returns_cancels_amount", 0) or 0), 2),
    }
    return {"meta": ch["meta"], "google": ch["google"], "organic": ch["organic"],
            "amazon": amazon, "total": total}


def get_dashboard_pdf_metrics(
    timeframe_start=None,
    timeframe_end=None,
    brand_id: Optional[int] = None,
    company_id: Optional[int] = None,
) -> dict:
    """
    Drop-in replacement for api_data_fetcher.get_organized_metrics_for_pdf(): returns the
    {meta, google, organic, total} dict for the marketing PDF top section, sourced from the
    General Statistics dashboard (API-first, ClickHouse fallback).

    timeframe_start / timeframe_end are datetime-like (as passed by generate_pdf_report).
    When omitted, defaults to today's IST date (single-day). brand_id/company_id default to
    CLICKHOUSE_BRAND_ID (20) and DASHBOARD_COMPANY_ID (19).
    """
    import pytz
    ist = pytz.timezone("Asia/Kolkata")

    def _ist_date(v) -> str:
        if isinstance(v, datetime):
            v = v.astimezone(ist) if v.tzinfo else ist.localize(v)
            return v.strftime("%Y-%m-%d")
        return _to_date_str(v)

    if timeframe_start is not None and timeframe_end is not None:
        start, end = _ist_date(timeframe_start), _ist_date(timeframe_end)
    else:
        start = end = datetime.now(ist).strftime("%Y-%m-%d")

    if brand_id is None:
        brand_id = int(os.getenv("CLICKHOUSE_BRAND_ID", "20"))
    if company_id is None:
        company_id = int(os.getenv("DASHBOARD_COMPANY_ID", "19"))

    stats = fetch_general_statistics(brand_id, company_id, start, end)
    logger.info("[dashboard] PDF metrics %s..%s brand=%s (source=%s)",
                start, end, brand_id, stats.get("source"))
    return build_pdf_api_metrics(stats)
