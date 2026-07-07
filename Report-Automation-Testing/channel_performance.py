"""
Channel performance metrics from ClickHouse gold tables.
Used for daily / WTD / MTD report bar charts (revenue, COGS, ad spend, net profit, orders by platform).
"""
from __future__ import annotations

import os
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_USE_API_ONLY = os.getenv("USE_API_ONLY", "false").lower() in ("1", "true", "yes")
_USE_API_FALLBACK = os.getenv("USE_API_FALLBACK", "true").lower() in ("1", "true", "yes")

try:
    from amazon_entity_report import get_clickhouse_client
except ImportError:
    get_clickhouse_client = None

PLATFORM_ORDER = ["meta", "google", "organic", "amazon", "other"]
PLATFORM_LABELS = {
    "meta": "Meta",
    "google": "Google",
    "organic": "Organic",
    "amazon": "Amazon",
    "other": "Other",
}
PLATFORM_COLORS = {
    "meta": "#1877F2",
    "google": "#E53935",
    "organic": "#2EAA63",
    "amazon": "#FF9900",
    "other": "#78909C",
}
PLATFORM_MARKERS = {
    "meta": "o",
    "google": "s",
    "organic": "^",
    "amazon": "D",
    "other": "X",
}

METRIC_COLORS = {
    "revenue": "#1A7F4E",
    "cogs": "#7B1FA2",
    "ad_spend": "#E07B00",
    "net_profit": "#1565C0",
    "orders": "#4A56E2",
}
METRIC_LABELS = {
    "revenue": "Gross Revenue",
    "cogs": "COGS",
    "ad_spend": "Ad Spend",
    "net_profit": "Net Profit",
    "orders": "Orders",
}

_CURRENCY_SYMBOL: Optional[str] = None
_RUPEE_CHAR = "\u20b9"


def _currency_symbol() -> str:
    """Return ₹ when the active chart font supports it, otherwise Rs."""
    global _CURRENCY_SYMBOL
    if _CURRENCY_SYMBOL is not None:
        return _CURRENCY_SYMBOL

    if os.getenv("CURRENCY_USE_RS", "").lower() in ("1", "true", "yes"):
        _CURRENCY_SYMBOL = "Rs"
        return _CURRENCY_SYMBOL

    families = matplotlib.rcParams.get("font.sans-serif", ["DejaVu Sans"])
    if isinstance(families, str):
        families = [families]
    # DejaVu Sans (default Agg backend font) does not reliably render ₹ in PNG output.
    if any("dejavu" in str(f).lower() for f in families):
        _CURRENCY_SYMBOL = "Rs"
        return _CURRENCY_SYMBOL

    try:
        from matplotlib import font_manager, ft2font

        for name in families:
            path = font_manager.findfont(font_manager.FontProperties(family=name))
            font = ft2font.FT2Font(path)
            if font.get_char_index(ord(_RUPEE_CHAR)) != 0:
                _CURRENCY_SYMBOL = _RUPEE_CHAR
                return _CURRENCY_SYMBOL
    except Exception:
        pass

    _CURRENCY_SYMBOL = "Rs"
    return _CURRENCY_SYMBOL


def _to_date_str(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def get_brand_id() -> int:
    raw = os.getenv("CLICKHOUSE_BRAND_ID")
    if raw:
        return int(raw)
    try:
        from global_config import get_global_config

        return int(get_global_config("CLICKHOUSE_BRAND_ID", "20"))
    except (ImportError, ValueError, TypeError):
        return 20


def _amazon_row_from_dashboard(dash: dict, report_date: str) -> Optional[dict]:
    """Build one Amazon channel row from a historical/dashboard payload."""
    amz = dash.get("amazon") or {}
    if not amz:
        return None
    ad_bd = dash.get("ad_spend_breakdown") or {}
    raw_spend = ad_bd.get("amazon")
    if isinstance(raw_spend, dict):
        spend = float(raw_spend.get("total", 0) or 0)
    else:
        spend = float(raw_spend or 0)
    rev = float(amz.get("net_sales") or amz.get("gross_sales") or 0)
    co = float(amz.get("cogs") or 0)
    orders = int(amz.get("orders") or 0)
    if rev == 0 and co == 0 and spend == 0 and orders == 0:
        return None
    net_profit = rev - co - spend
    return {
        "report_date": report_date,
        "platform": "amazon",
        "attributed_orders": orders,
        "gross_revenue_excl_gst": round(rev, 2),
        "cogs": round(co, 2),
        "ad_spend": round(spend, 2),
        "net_profit": round(net_profit, 2),
        "gross_roas": round(rev / spend, 2) if spend > 0 else None,
    }


def _append_amazon_rows_from_dashboard(
    df: pd.DataFrame, start_str: str, end_str: str
) -> pd.DataFrame:
    """Merge Amazon daily rows when the primary source omits them (e.g. attribution)."""
    from api_data_fetcher import fetch_historical_dashboard

    if not df.empty and "amazon" in df["platform"].values:
        return df

    dates = pd.date_range(start_str, end_str, freq="D").strftime("%Y-%m-%d").tolist()
    if not dates:
        dates = [end_str]

    amazon_rows: list[dict] = []
    for d in dates:
        dash = fetch_historical_dashboard(d, d)
        if not dash:
            continue
        row = _amazon_row_from_dashboard(dash, d)
        if row:
            amazon_rows.append(row)

    if not amazon_rows:
        return df

    amz_df = pd.DataFrame(amazon_rows)
    if df.empty:
        return amz_df
    return pd.concat([df, amz_df], ignore_index=True)


def _fetch_channel_performance_from_attribution(start_str: str, end_str: str) -> pd.DataFrame:
    """Per-day channel rows from marketing attribution (matches entity-report sheets)."""
    from api_data_fetcher import fetch_marketing_hourly
    from dailyrollup import transform_attribution_data

    df = fetch_marketing_hourly(start_str, end_str)
    if df.empty:
        return pd.DataFrame()

    t = transform_attribution_data(df)
    if "date_start" not in t.columns:
        t["date_start"] = end_str

    source_map = {"Meta Ads": "meta", "Google Ads": "google", "Organic": "organic"}
    rows: list[dict] = []
    group_cols = [c for c in ("date_start", "source") if c in t.columns]
    if not group_cols:
        return pd.DataFrame()

    for keys, sub in t.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        key_map = dict(zip(group_cols, keys))
        src = key_map.get("source")
        platform = source_map.get(src)
        if not platform:
            continue
        d = str(key_map.get("date_start", end_str))[:10]
        rev = float(pd.to_numeric(sub.get("shopify_revenue"), errors="coerce").fillna(0).sum()) if "shopify_revenue" in sub.columns else 0.0
        co = float(pd.to_numeric(sub.get("shopify_cogs"), errors="coerce").fillna(0).sum()) if "shopify_cogs" in sub.columns else 0.0
        spend = (
            float(pd.to_numeric(sub.get("spend"), errors="coerce").fillna(0).sum())
            if platform != "organic" and "spend" in sub.columns
            else 0.0
        )
        orders = int(pd.to_numeric(sub.get("shopify_orders"), errors="coerce").fillna(0).sum()) if "shopify_orders" in sub.columns else 0
        net_profit = rev - co - spend
        rows.append({
            "report_date": d,
            "platform": platform,
            "attributed_orders": orders,
            "gross_revenue_excl_gst": round(rev, 2),
            "cogs": round(co, 2),
            "ad_spend": round(spend, 2),
            "net_profit": round(net_profit, 2),
            "gross_roas": round(rev / spend, 2) if spend > 0 else None,
        })
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    return _append_amazon_rows_from_dashboard(df, start_str, end_str)


def _fetch_channel_performance_from_dashboard_by_day(start_str: str, end_str: str) -> pd.DataFrame:
    """Per-day channel rows from historical/dashboard (one API call per day)."""
    from api_data_fetcher import fetch_historical_dashboard

    dates = pd.date_range(start_str, end_str, freq="D").strftime("%Y-%m-%d").tolist()
    if not dates:
        dates = [end_str]

    rows: list[dict] = []
    for d in dates:
        dash = fetch_historical_dashboard(d, d)
        if not dash:
            continue
        ns = dash.get("net_sales_breakdown") or {}
        cogs_bd = dash.get("cogs_breakdown") or {}
        ad_bd = dash.get("ad_spend_breakdown") or {}
        orders_bd = dash.get("orders_breakdown") or {}
        for platform in ("meta", "google", "organic", "amazon", "other"):
            if platform == "amazon":
                amz_row = _amazon_row_from_dashboard(dash, d)
                if amz_row:
                    rows.append(amz_row)
                continue
            raw_spend = ad_bd.get(platform) if platform != "organic" else 0
            if isinstance(raw_spend, dict):
                spend = float(raw_spend.get("total", 0) or 0)
            else:
                spend = float(raw_spend or 0) if platform != "organic" else 0.0
            rev = float(ns.get(platform) or 0) if not isinstance(ns.get(platform), dict) else 0.0
            co = float(cogs_bd.get(platform) or 0) if not isinstance(cogs_bd.get(platform), dict) else 0.0
            raw_orders = orders_bd.get(platform) or 0
            oc = int(raw_orders.get("orders", 0) if isinstance(raw_orders, dict) else raw_orders or 0)
            net_profit = rev - co - spend
            rows.append({
                "report_date": d,
                "platform": platform,
                "attributed_orders": oc,
                "gross_revenue_excl_gst": round(rev, 2),
                "cogs": round(co, 2),
                "ad_spend": round(spend, 2),
                "net_profit": round(net_profit, 2),
                "gross_roas": round(rev / spend, 2) if spend > 0 else None,
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _fetch_channel_performance_from_api(start_str: str, end_str: str) -> pd.DataFrame:
    """Build channel performance daily rows from GET /v1/historical/time-patterns."""
    from api_data_fetcher import fetch_historical_time_patterns
    from api_response_transformers import time_patterns_daily_df

    data = fetch_historical_time_patterns(start_str, end_str)
    if not data:
        return pd.DataFrame()
    daily = time_patterns_daily_df(data)
    if daily.empty:
        return pd.DataFrame()

    rows = []
    for _, row in daily.iterrows():
        d = str(row.get("sale_date", ""))[:10]
        rev = float(row.get("revenue", 0) or 0)
        cogs = float(row.get("cogs", 0) or 0)
        spend = float(row.get("total_ad_spend", 0) or 0)
        np = float(row.get("net_profit", 0) or 0)
        if np == 0:
            np = rev - cogs - spend
        for platform in PLATFORM_ORDER:
            share = 0.25 if platform != "other" else 0.0
            if platform == "other":
                continue
            rows.append({
                "report_date": d,
                "platform": platform,
                "attributed_orders": 0,
                "gross_revenue_excl_gst": rev * share,
                "cogs": cogs * share,
                "ad_spend": spend * share if platform != "organic" else 0.0,
                "net_profit": np * share,
                "net_roas": ((rev - cogs) / spend) if spend > 0 and platform != "organic" else None,
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_channel_performance(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch daily channel performance rows (API-first, ClickHouse fallback)."""
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)

    if _USE_API_ONLY or _USE_API_FALLBACK:
        try:
            use_attribution = os.getenv("CHANNEL_FROM_ATTRIBUTION", "false").lower() in ("1", "true", "yes")
            df = pd.DataFrame()
            if use_attribution:
                df = _fetch_channel_performance_from_attribution(start_str, end_str)
            if df.empty:
                df = _fetch_channel_performance_from_dashboard_by_day(start_str, end_str)
            if not df.empty:
                return df
        except Exception as e:
            if _USE_API_ONLY:
                raise
            logger.warning("channel performance API failed (%s); using ClickHouse", e)

    if _USE_API_ONLY:
        return pd.DataFrame()

    if get_clickhouse_client is None:
        raise ImportError(
            "clickhouse-connect is required. Install with: pip install clickhouse-connect"
        )

    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    if brand_id is None:
        brand_id = get_brand_id()

    query = """
        SELECT
            toString(d.report_date) AS report_date,
            ch.platform AS platform,
            coalesce(
                if(ch.platform = 'amazon', amz.attributed_orders, c.attributed_orders), 0
            ) AS attributed_orders,
            round(coalesce(
                if(ch.platform = 'amazon', amz.gross_revenue_excl_gst, c.gross_revenue_excl_gst), 0
            ), 2) AS gross_revenue_excl_gst,
            round(coalesce(
                if(ch.platform = 'amazon', amz.cogs, g.cogs), 0
            ), 2) AS cogs,
            coalesce(
                if(ch.platform = 'amazon', amz.ad_spend, s.ad_spend), 0
            ) AS ad_spend,
            round(
                coalesce(
                    if(ch.platform = 'amazon', amz.gross_revenue_excl_gst, c.gross_revenue_excl_gst), 0
                )
                - coalesce(if(ch.platform = 'amazon', amz.cogs, g.cogs), 0)
                - coalesce(if(ch.platform = 'amazon', amz.ad_spend, s.ad_spend), 0),
                2
            ) AS net_profit,
            round(
                if(
                    coalesce(if(ch.platform = 'amazon', amz.ad_spend, s.ad_spend), 0) > 0,
                    coalesce(
                        if(ch.platform = 'amazon', amz.gross_revenue_excl_gst, c.gross_revenue_excl_gst), 0
                    ) / coalesce(if(ch.platform = 'amazon', amz.ad_spend, s.ad_spend), 0),
                    NULL
                ),
                2
            ) AS gross_roas
        FROM (
            SELECT DISTINCT report_date
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
        ) AS d
        CROSS JOIN (
            SELECT arrayJoin(['meta', 'google', 'organic', 'amazon', 'other']) AS platform
        ) AS ch
        LEFT JOIN (
            SELECT
                order_date AS report_date,
                coalesce(nullIf(lt_platform, ''), 'other') AS platform,
                toInt64(count()) AS attributed_orders,
                toFloat64(sum(gross_revenue)) / 1.18 AS gross_revenue_excl_gst
            FROM gold.fct_order_attribution
            WHERE brand_id = %(brand_id)s
              AND order_date >= toDate(%(start_date)s)
              AND order_date <= toDate(%(end_date)s)
            GROUP BY report_date, platform
        ) AS c
            ON d.report_date = c.report_date AND ch.platform = c.platform
        LEFT JOIN (
            SELECT
                a.order_date AS report_date,
                coalesce(nullIf(a.lt_platform, ''), 'other') AS platform,
                toFloat64(sum(coalesce(ic.cogs, 0))) AS cogs
            FROM gold.fct_order_attribution AS a
            LEFT JOIN (
                SELECT
                    brand_id,
                    order_id,
                    sum(toFloat64(net_cost)) AS cogs
                FROM gold.fct_order_items
                WHERE brand_id = %(brand_id)s
                  AND order_date >= toDate(%(start_date)s)
                  AND order_date <= toDate(%(end_date)s)
                  AND coalesce(included_in_pnl_cogs, 1) = 1
                GROUP BY brand_id, order_id
            ) AS ic
                ON a.brand_id = ic.brand_id AND a.order_id = ic.order_id
            WHERE a.brand_id = %(brand_id)s
              AND a.order_date >= toDate(%(start_date)s)
              AND a.order_date <= toDate(%(end_date)s)
            GROUP BY report_date, platform
        ) AS g
            ON d.report_date = g.report_date AND ch.platform = g.platform
        LEFT JOIN (
            SELECT 'meta' AS platform, report_date, toFloat64(meta_spend) AS ad_spend
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'google', report_date, toFloat64(google_spend)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'organic', report_date, toFloat64(0)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'amazon', report_date, toFloat64(amazon_spend)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'other', report_date, toFloat64(0)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
        ) AS s
            ON d.report_date = s.report_date AND ch.platform = s.platform
        LEFT JOIN (
            SELECT
                report_date,
                toInt64(coalesce(amazon_orders, 0)) AS attributed_orders,
                toFloat64(coalesce(amazon_gross_revenue, 0)) AS gross_revenue_excl_gst,
                toFloat64(
                    coalesce(amazon_product_cost, 0) + coalesce(amazon_platform_fees, 0)
                ) AS cogs,
                toFloat64(coalesce(amazon_spend, 0)) AS ad_spend
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
        ) AS amz
            ON d.report_date = amz.report_date AND ch.platform = 'amazon'
        ORDER BY report_date, ch.platform
    """
    client = get_clickhouse_client()
    params = {
        "brand_id": int(brand_id),
        "start_date": start_str,
        "end_date": end_str,
    }
    result = client.query(query, parameters=params)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    if df.empty:
        return df

    for col in ("attributed_orders", "gross_revenue_excl_gst", "cogs", "ad_spend", "net_profit"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "gross_roas" in df.columns:
        df["gross_roas"] = pd.to_numeric(df["gross_roas"], errors="coerce")
    if "attributed_orders" in df.columns:
        df["attributed_orders"] = df["attributed_orders"].astype(int)
    return df


def aggregate_channel_performance(df: pd.DataFrame) -> pd.DataFrame:
    """Sum daily rows into one row per platform."""
    if df.empty:
        return df
    agg = (
        df.groupby("platform", as_index=False)
        .agg(
            attributed_orders=("attributed_orders", "sum"),
            gross_revenue_excl_gst=("gross_revenue_excl_gst", "sum"),
            cogs=("cogs", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
    )
    agg["net_profit"] = agg["gross_revenue_excl_gst"] - agg["cogs"] - agg["ad_spend"]
    agg["gross_roas"] = np.where(
        agg["ad_spend"] > 0,
        agg["gross_revenue_excl_gst"] / agg["ad_spend"],
        np.nan,
    )
    agg["net_roas"] = np.where(
        agg["ad_spend"] > 0,
        (agg["gross_revenue_excl_gst"] - agg["cogs"]) / agg["ad_spend"],
        np.nan,
    )
    agg["gross_revenue_excl_gst"] = agg["gross_revenue_excl_gst"].round(2)
    agg["cogs"] = agg["cogs"].round(2)
    agg["ad_spend"] = agg["ad_spend"].round(2)
    agg["net_profit"] = agg["net_profit"].round(2)
    agg["gross_roas"] = agg["gross_roas"].round(2)
    agg["net_roas"] = agg["net_roas"].round(2)
    order_map = {p: i for i, p in enumerate(PLATFORM_ORDER)}
    agg["_sort"] = agg["platform"].map(order_map).fillna(99)
    return agg.sort_values("_sort").drop(columns=["_sort"]).reset_index(drop=True)


def fetch_channel_attributed_canonical(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """
    Independent per-channel attribution recompute using the SAME canonical definitions as
    the dashboard / PDF channel table: net-attributed-sales (deduped fct_orders join) and
    ad spend from the daily ad tables (fct_meta_ads_daily / fct_google_ads_daily).

    Returns a DataFrame [platform, net_sales, ad_spend] for meta / google / organic.
    Used by the ROAS reconciliation so the Calc side matches the PDF side (no method gap).
    """
    if get_clickhouse_client is None:
        raise ImportError("clickhouse-connect is required.")
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    if brand_id is None:
        brand_id = get_brand_id()
    client = get_clickhouse_client()
    p = {"b": int(brand_id), "s": start_str, "e": end_str}

    sales_sql = """
        WITH order_channel AS (
            SELECT a.brand_id, a.order_id,
                any(multiIf(
                    lowerUTF8(trimBoth(coalesce(a.lt_platform,''))) IN ('meta','facebook','instagram','fb','ig'),'meta',
                    lowerUTF8(trimBoth(coalesce(a.lt_platform,''))) IN ('google','google_ads'),'google',
                    'organic')) AS channel
            FROM gold.fct_order_attribution AS a
            WHERE a.brand_id=%(b)s AND a.order_date>=toDate(%(s)s) AND a.order_date<=toDate(%(e)s)
              AND coalesce(a.is_test,0)=0 AND lowerUTF8(trimBoth(coalesce(a.order_status,'')))!='voided'
            GROUP BY a.brand_id, a.order_id
        ),
        orders_dedup AS (
            SELECT brand_id, order_id, argMax(order_date,_loaded_at) order_date,
                argMax(order_status,_loaded_at) order_status, argMax(is_test,_loaded_at) is_test,
                argMax(is_revenue_adjustment,_loaded_at) is_rev_adj,
                toFloat64(argMax(net_revenue,_loaded_at)) nr, toFloat64(argMax(net_revenue_excl_tax,_loaded_at)) nret,
                toFloat64(argMax(gross_revenue,_loaded_at)) gr, toFloat64(argMax(gross_revenue_excl_tax,_loaded_at)) gret,
                toFloat64(argMax(total_discounts,_loaded_at)) td, toFloat64(argMax(total_tax,_loaded_at)) tt
            FROM gold.fct_orders WHERE brand_id=%(b)s GROUP BY brand_id, order_id
        ),
        base AS (
            SELECT coalesce(oc.channel,'organic') AS channel,
                if(o.nr>0 AND o.gret>o.nret, o.gret-o.nret, if(o.tt>0 AND o.gr>0, o.td*((o.gr-o.tt)/o.gr), o.td)) AS disc_excl,
                o.order_status, o.is_rev_adj, o.nr, o.nret, o.gret
            FROM orders_dedup o LEFT JOIN order_channel oc ON oc.brand_id=o.brand_id AND oc.order_id=o.order_id
            WHERE o.order_date>=toDate(%(s)s) AND o.order_date<=toDate(%(e)s)
              AND coalesce(o.is_test,0)=0 AND lowerUTF8(trimBoth(coalesce(o.order_status,'')))!='voided'
        )
        SELECT channel, round(sum(if(lowerUTF8(trimBoth(coalesce(order_status,'')))='cancelled',0,
            if(is_rev_adj=1,0,if(nr>0,nret,greatest(0,gret-disc_excl))))),2) AS net_sales
        FROM base GROUP BY channel
    """
    sales = {r[0]: float(r[1]) for r in client.query(sales_sql, parameters=p).result_rows}

    def _spend(table):
        q = (f"SELECT round(sum(toFloat64(spend)),2) FROM gold.{table} "
             "WHERE brand_id=%(b)s AND report_date>=toDate(%(s)s) AND report_date<=toDate(%(e)s)")
        v = client.query(q, parameters=p).result_rows[0][0]
        return float(v or 0)

    spend = {"meta": _spend("fct_meta_ads_daily"), "google": _spend("fct_google_ads_daily"), "organic": 0.0}
    rows = [{"platform": ch, "net_sales": round(sales.get(ch, 0.0), 2), "ad_spend": round(spend.get(ch, 0.0), 2)}
            for ch in ("meta", "google", "organic")]
    return pd.DataFrame(rows)


def reconcile_roas_with_pdf_metrics(
    api_metrics: dict,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
    roas_tolerance: float = 0.03,
    revenue_tolerance_pct: float = 0.01,
) -> Optional[dict]:
    """
    Compare gross ROAS and revenue from channel_performance (ClickHouse attribution)
    against the dashboard PDF metrics (api_metrics).

    Returns per-channel rows with pdf vs calculated values, or None on fetch failure.
    """
    try:
        # Independent attribution recompute using the SAME canonical definitions as the PDF
        # channel table (net-attributed-sales + daily ad spend), so the two sides reconcile
        # without a method gap.
        calc_df = fetch_channel_attributed_canonical(start_date, end_date, brand_id=brand_id)
    except Exception as exc:
        logger.warning("ROAS reconciliation skipped: %s", exc)
        return None

    if calc_df.empty:
        return None

    calc_by_platform = calc_df.set_index("platform")

    def _row(platform: str, pdf_key: str) -> dict:
        pdf_ch = api_metrics.get(pdf_key, {})
        pdf_sales = float(pdf_ch.get("sales", 0) or 0)
        pdf_spend = float(pdf_ch.get("ad_spend", 0) or 0)
        pdf_gross_roas = float(pdf_ch.get("gross_roas", 0) or 0)

        if platform in calc_by_platform.index:
            calc = calc_by_platform.loc[platform]
            calc_revenue = float(calc["net_sales"])
            calc_spend = float(calc["ad_spend"])
        else:
            calc_revenue = 0.0
            calc_spend = 0.0

        calc_gross_roas = (calc_revenue / calc_spend) if calc_spend > 0 else None
        sales_delta_pct = (
            (calc_revenue - pdf_sales) / pdf_sales * 100.0 if pdf_sales else None
        )
        roas_delta = (
            (calc_gross_roas - pdf_gross_roas)
            if calc_gross_roas is not None and pdf_spend > 0
            else None
        )
        revenue_match = (
            sales_delta_pct is None
            or abs(sales_delta_pct) <= revenue_tolerance_pct * 100.0
        )
        roas_match = (
            roas_delta is None
            or abs(roas_delta) <= roas_tolerance
        )

        return {
            "platform": platform,
            "label": PLATFORM_LABELS.get(platform, platform.title()),
            "pdf_sales": round(pdf_sales, 2),
            "calc_revenue": round(calc_revenue, 2),
            "sales_delta_pct": round(sales_delta_pct, 2) if sales_delta_pct is not None else None,
            "pdf_ad_spend": round(pdf_spend, 2),
            "calc_ad_spend": round(calc_spend, 2),
            "pdf_gross_roas": round(pdf_gross_roas, 2),
            "calc_gross_roas": round(calc_gross_roas, 2) if calc_gross_roas is not None else None,
            "roas_delta": round(roas_delta, 2) if roas_delta is not None else None,
            "revenue_match": revenue_match,
            "roas_match": roas_match,
        }

    channels = [_row(p, p) for p in ("meta", "google", "organic")]
    # "Attributed Total" = sum of the attributed channels on BOTH sides (not the all-up
    # dashboard total, which also includes Amazon and event-date returns/cancels and would
    # introduce a spurious gap).
    total_pdf_sales = sum(c["pdf_sales"] for c in channels)
    total_pdf_spend = sum(c["pdf_ad_spend"] for c in channels)
    total_pdf_gross = (total_pdf_sales / total_pdf_spend) if total_pdf_spend else 0.0
    total_calc_revenue = sum(c["calc_revenue"] for c in channels)
    total_calc_spend = sum(c["calc_ad_spend"] for c in channels)
    total_calc_gross = (
        total_calc_revenue / total_calc_spend if total_calc_spend > 0 else None
    )
    total_sales_delta = (
        (total_calc_revenue - total_pdf_sales) / total_pdf_sales * 100.0
        if total_pdf_sales
        else None
    )
    total_roas_delta = (
        (total_calc_gross - total_pdf_gross)
        if total_calc_gross is not None and total_pdf_spend > 0
        else None
    )
    total_revenue_match = (
        total_sales_delta is None
        or abs(total_sales_delta) <= revenue_tolerance_pct * 100.0
    )
    total_roas_match = (
        total_roas_delta is None
        or abs(total_roas_delta) <= roas_tolerance
    )

    return {
        "channels": channels,
        "total": {
            "pdf_sales": round(total_pdf_sales, 2),
            "calc_revenue": round(total_calc_revenue, 2),
            "sales_delta_pct": round(total_sales_delta, 2) if total_sales_delta is not None else None,
            "pdf_gross_roas": round(total_pdf_gross, 2),
            "calc_gross_roas": round(total_calc_gross, 2) if total_calc_gross is not None else None,
            "roas_delta": round(total_roas_delta, 2) if total_roas_delta is not None else None,
            "revenue_match": total_revenue_match,
            "roas_match": total_roas_match,
        },
        "all_match": all(r["revenue_match"] and r["roas_match"] for r in channels)
        and total_revenue_match
        and total_roas_match,
    }


def _format_inr(value: float) -> str:
    sym = _currency_symbol()
    if abs(value) >= 100_000:
        return f"{sym}{value / 100_000:.1f}L"
    if abs(value) >= 1_000:
        return f"{sym}{value / 1_000:.1f}K"
    return f"{sym}{value:,.0f}"


def _format_inr_axis(value: float, _pos=None) -> str:
    """Linear y-axis ticks: keep one unit (K or L) so spacing reads correctly."""
    sym = _currency_symbol()
    av = abs(value)
    if av >= 1_000_000:
        return f"{sym}{value / 100_000:.1f}L"
    if av >= 1_000:
        return f"{sym}{value / 1_000:.0f}K"
    return f"{sym}{value:,.0f}"


def _adjust_color(hex_color: str, *, lighten: float = 0.0, darken: float = 0.0) -> str:
    import matplotlib.colors as mcolors

    rgb = np.array(mcolors.to_rgb(hex_color))
    if lighten > 0:
        rgb = rgb + (1.0 - rgb) * lighten
    if darken > 0:
        rgb = rgb * (1.0 - darken)
    return mcolors.to_hex(np.clip(rgb, 0, 1))


def _platform_palette(platform: str) -> dict[str, str]:
    """Platform-branded shades: revenue (base), ad spend (darker), orders (lighter)."""
    base = PLATFORM_COLORS.get(platform, "#666666")
    return {
        "revenue": base,
        "ad_spend": _adjust_color(base, darken=0.30),
        "orders": _adjust_color(base, lighten=0.45),
    }


def _add_bar_labels(ax, bars, values, fmt_fn, min_height_frac=0.0, zero_label: Optional[str] = None):
    """Place value labels above (or below) bars; skip near-zero bars unless zero_label set."""
    vals = [float(v) for v in values]
    ymax = max(max(vals), 0.0) if vals else 1
    ymin = min(min(vals), 0.0) if vals else 0
    span = max(ymax - ymin, ymax, 1.0)
    pad = span * 0.03
    for bar, val in zip(bars, vals):
        if val == 0:
            if zero_label:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    pad * 0.35,
                    zero_label,
                    ha="center",
                    va="bottom",
                    fontsize=7.5,
                    fontweight="500",
                    color="#999999",
                )
            continue
        if ymax > 0 and val > 0 and val / ymax < min_height_frac:
            continue
        if val >= 0:
            y_pos = bar.get_height() + pad
            va = "bottom"
        else:
            y_pos = bar.get_height() - pad
            va = "top"
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            y_pos,
            fmt_fn(val),
            ha="center",
            va=va,
            fontsize=8.5,
            fontweight="600",
            color="#1a1a1a",
        )


def _day_count_in_range(start_str: str, end_str: str) -> int:
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        return (end_dt - start_dt).days + 1
    except ValueError:
        return 1


def _prepare_daily_roas(
    raw: pd.DataFrame,
    *,
    min_plot_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    Daily gross ROAS per platform (revenue / ad spend).

    When a day's ad spend is zero, the previous day's spend is used for ROAS so
    delayed ad reporting (e.g. Amazon) does not suppress the metric.
    """
    if raw.empty:
        return raw
    daily = raw.copy()
    daily["report_date"] = pd.to_datetime(daily["report_date"])
    daily["gross_roas"] = np.nan

    for platform in PLATFORM_ORDER:
        plat = daily[daily["platform"] == platform].sort_values("report_date")
        if plat.empty:
            continue
        prev_spend = 0.0
        roas_vals = []
        for _, row in plat.iterrows():
            spend = float(row.get("ad_spend") or 0)
            rev = float(row.get("gross_revenue_excl_gst") or 0)
            effective_spend = spend if spend > 0 else prev_spend
            if effective_spend > 0:
                roas_vals.append(rev / effective_spend)
            else:
                roas_vals.append(np.nan)
            if spend > 0:
                prev_spend = spend
        daily.loc[plat.index, "gross_roas"] = roas_vals

    if min_plot_date:
        min_dt = pd.to_datetime(min_plot_date)
        daily = daily[daily["report_date"] >= min_dt]

    order_map = {p: i for i, p in enumerate(PLATFORM_ORDER)}
    daily["_sort"] = daily["platform"].map(order_map).fillna(99)
    return daily.sort_values(["report_date", "_sort"]).drop(columns=["_sort"])


def _plot_roas_by_day(
    ax,
    raw: pd.DataFrame,
    *,
    roas_trend_days: int = 7,
    legend_below: bool = True,
    min_plot_date: Optional[str] = None,
    platforms: Optional[list[str]] = None,
    title: Optional[str] = None,
    show_legend: bool = True,
) -> bool:
    """Line chart: gross ROAS per channel for each day in the window. Returns False if nothing plotted."""
    daily = _prepare_daily_roas(raw, min_plot_date=min_plot_date)
    if daily.empty:
        ax.set_visible(False)
        return False

    plot_platforms = platforms or [p for p in ("meta", "google") if p in PLATFORM_ORDER]
    daily = daily[daily["platform"].isin(plot_platforms)]
    if daily.empty:
        ax.set_visible(False)
        return False

    dates = sorted(daily["report_date"].unique())
    if len(dates) < 2:
        ax.set_visible(False)
        return False

    x = np.arange(len(dates))
    date_labels = [pd.Timestamp(d).strftime("%d %b") for d in dates]
    plotted = False

    for platform in plot_platforms:
        plat = daily[daily["platform"] == platform]
        if plat.empty:
            continue
        series = (
            plat.set_index("report_date")["gross_roas"]
            .reindex(dates)
        )
        y = series.values.astype(float)
        if np.all(np.isnan(y)):
            continue
        plotted = True
        color = PLATFORM_COLORS.get(platform, "#666666")
        label = PLATFORM_LABELS.get(platform, platform.title())
        marker = PLATFORM_MARKERS.get(platform, "o")
        ax.plot(
            x,
            y,
            marker=marker,
            markersize=5,
            linewidth=2.2,
            color=color,
            markerfacecolor=color,
            markeredgecolor="white",
            markeredgewidth=0.8,
            label=label,
            zorder=3,
        )
        for xi, val in zip(x, y):
            if np.isnan(val) or val <= 0:
                continue
            ax.annotate(
                f"{val:.2f}x",
                (xi, val),
                textcoords="offset points",
                xytext=(0, 6),
                ha="center",
                fontsize=6.5,
                fontweight="600",
                color=color,
            )

    if not plotted:
        ax.set_visible(False)
        return False

    if title:
        chart_title = title
    elif len(plot_platforms) == 1:
        label = PLATFORM_LABELS.get(plot_platforms[0], plot_platforms[0].title())
        chart_title = f"{label} ROAS — Last {roas_trend_days} Days"
    else:
        chart_title = f"ROAS by Channel — Last {roas_trend_days} Days"

    ax.set_title(
        chart_title,
        fontsize=11,
        fontweight="bold",
        color="#1a1a1a",
        pad=10,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(
        date_labels,
        fontsize=7 if len(dates) > 14 else 8,
        rotation=90,
        ha="center",
    )
    ax.set_ylabel("ROAS", fontsize=10, color="#333333", labelpad=8)
    ax.set_facecolor("#FAFBFC")
    ax.grid(
        True,
        which="both",
        axis="both",
        alpha=0.28,
        linestyle="-",
        linewidth=0.65,
        color="#E2E8F0",
        zorder=0,
    )
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#BBBBBB")
    ax.spines["bottom"].set_color("#BBBBBB")

    valid = daily["gross_roas"].dropna()
    if not valid.empty:
        ymax = float(valid.max())
        ax.set_ylim(0, max(ymax * 1.35, 0.5))
    if show_legend and len(plot_platforms) > 1:
        legend_kwargs = dict(
            frameon=True,
            fontsize=8,
            title="Channels",
            title_fontsize=8,
            edgecolor="#DDDDDD",
            facecolor="white",
        )
        if legend_below:
            ax.legend(
                loc="upper center",
                bbox_to_anchor=(0.5, -0.20),
                ncol=len(plot_platforms),
                **legend_kwargs,
            )
        else:
            ax.legend(loc="upper right", ncol=1, **legend_kwargs)
    return True


def plot_channel_performance(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    save_path: Optional[str] = None,
    brand_id: Optional[int] = None,
    period_label: Optional[str] = None,
    include_roas_trend: bool = False,
    roas_trend_days: int = 7,
    amazon_roas_trend_days: int = 14,
) -> Optional[str]:
    """
    Grouped bar chart: revenue, COGS, ad spend, net profit, and orders by channel.
    Optionally adds ROAS trend panels below: Meta/Google (roas_trend_days) and
    Amazon (amazon_roas_trend_days) side by side.
    """
    try:
        raw = fetch_channel_performance(start_date, end_date, brand_id=brand_id)
        if raw.empty:
            logger.warning(
                "No channel performance data for %s to %s",
                _to_date_str(start_date),
                _to_date_str(end_date),
            )
            return None

        df = aggregate_channel_performance(raw)
        if df.empty or (
            df["gross_revenue_excl_gst"].sum() == 0
            and df["cogs"].sum() == 0
            and df["ad_spend"].sum() == 0
            and df["attributed_orders"].sum() == 0
        ):
            logger.warning("All channel performance metrics zero — skipping chart.")
            return None

        platforms = [
            p
            for p in PLATFORM_ORDER
            if p in df["platform"].values
            and (
                df.loc[df["platform"] == p, "gross_revenue_excl_gst"].sum()
                + df.loc[df["platform"] == p, "cogs"].sum()
                + df.loc[df["platform"] == p, "ad_spend"].sum()
                + df.loc[df["platform"] == p, "attributed_orders"].sum()
            )
            > 0
        ]
        plot_df = df.set_index("platform").reindex(platforms).fillna(0).reset_index()
        channel_labels = [PLATFORM_LABELS.get(p, p.title()) for p in platforms]

        start_str = _to_date_str(start_date)
        end_str = _to_date_str(end_date)
        if period_label:
            title = f"Channel Performance — {period_label}"
        elif start_str == end_str:
            try:
                dt = datetime.strptime(end_str, "%Y-%m-%d")
                title = f"Channel Performance — {dt.strftime('%d %b %Y')}"
            except ValueError:
                title = f"Channel Performance — {end_str}"
        else:
            try:
                s_dt = datetime.strptime(start_str, "%Y-%m-%d")
                e_dt = datetime.strptime(end_str, "%Y-%m-%d")
                title = (
                    f"Channel Performance — {s_dt.strftime('%d %b')} to "
                    f"{e_dt.strftime('%d %b %Y')}"
                )
            except ValueError:
                title = f"Channel Performance — {start_str} to {end_str}"

        total_rev = float(plot_df["gross_revenue_excl_gst"].sum())
        total_cogs = float(plot_df["cogs"].sum())
        total_spend = float(plot_df["ad_spend"].sum())
        total_net_profit = float(plot_df["net_profit"].sum())
        total_orders = int(plot_df["attributed_orders"].sum())
        total_gross_roas = total_rev / total_spend if total_spend > 0 else 0

        rev_vals = plot_df["gross_revenue_excl_gst"].values.astype(float)
        cogs_vals = plot_df["cogs"].values.astype(float)
        spend_vals = plot_df["ad_spend"].values.astype(float)
        profit_vals = plot_df["net_profit"].values.astype(float)
        order_vals = plot_df["attributed_orders"].values.astype(float)

        n = len(platforms)
        x = np.arange(n)
        bar_w = 0.18
        offsets = np.array([-1.5, -0.5, 0.5, 1.5], dtype=float) * bar_w

        trend_raw = pd.DataFrame()
        show_roas_trend = False
        show_amazon_roas = False
        if include_roas_trend:
            try:
                end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
                window_days = max(roas_trend_days, amazon_roas_trend_days)
                trend_start = end_dt - timedelta(days=window_days - 1)
                fetch_start = trend_start - timedelta(days=1)
                trend_raw = fetch_channel_performance(fetch_start, end_str, brand_id=brand_id)
                if not trend_raw.empty:
                    main_start = end_dt - timedelta(days=roas_trend_days - 1)
                    main_roas = _prepare_daily_roas(
                        trend_raw,
                        min_plot_date=main_start.strftime("%Y-%m-%d"),
                    )
                    show_roas_trend = len(main_roas["report_date"].unique()) >= 2
                    amazon_start = end_dt - timedelta(days=amazon_roas_trend_days - 1)
                    amazon_roas = _prepare_daily_roas(
                        trend_raw,
                        min_plot_date=amazon_start.strftime("%Y-%m-%d"),
                    )
                    amazon_roas = amazon_roas[amazon_roas["platform"] == "amazon"]
                    show_amazon_roas = (
                        not amazon_roas.empty
                        and len(amazon_roas["report_date"].unique()) >= 2
                        and amazon_roas["gross_roas"].notna().any()
                    )
            except Exception as trend_err:
                logger.warning("Could not load ROAS trend data: %s", trend_err)

        if show_roas_trend or show_amazon_roas:
            fig = plt.figure(figsize=(16, 10.5), facecolor="white")
            if show_roas_trend and show_amazon_roas:
                gs = fig.add_gridspec(
                    2, 2,
                    height_ratios=[1.5, 1],
                    width_ratios=[1, 1],
                    hspace=0.42,
                    wspace=0.22,
                )
                ax1 = fig.add_subplot(gs[0, :])
                ax_roas = fig.add_subplot(gs[1, 0])
                ax_amazon_roas = fig.add_subplot(gs[1, 1])
            else:
                gs = fig.add_gridspec(2, 1, height_ratios=[1.5, 1], hspace=0.42)
                ax1 = fig.add_subplot(gs[0])
                ax_roas = fig.add_subplot(gs[1]) if show_roas_trend else None
                ax_amazon_roas = fig.add_subplot(gs[1]) if show_amazon_roas else None
            ax2 = ax1.twinx()
        else:
            fig, ax1 = plt.subplots(figsize=(14, 7), facecolor="white")
            ax2 = ax1.twinx()
            ax_roas = None
            ax_amazon_roas = None

        rev_colors = [METRIC_COLORS["revenue"]] * n
        cogs_colors = [METRIC_COLORS["cogs"]] * n
        spend_colors = [METRIC_COLORS["ad_spend"]] * n
        profit_colors = [
            METRIC_COLORS["net_profit"] if val >= 0 else "#C62828"
            for val in profit_vals
        ]

        bars_rev = ax1.bar(
            x + offsets[0],
            rev_vals,
            bar_w,
            color=rev_colors,
            edgecolor="white",
            linewidth=1.0,
            zorder=3,
        )
        bars_cogs = ax1.bar(
            x + offsets[1],
            cogs_vals,
            bar_w,
            color=cogs_colors,
            edgecolor="white",
            linewidth=1.0,
            zorder=3,
        )
        bars_spend = ax1.bar(
            x + offsets[2],
            spend_vals,
            bar_w,
            color=spend_colors,
            edgecolor="white",
            linewidth=1.0,
            zorder=3,
        )
        bars_profit = ax1.bar(
            x + offsets[3],
            profit_vals,
            bar_w,
            color=profit_colors,
            edgecolor="white",
            linewidth=1.0,
            zorder=3,
        )

        ax2.plot(
            x,
            order_vals,
            color=METRIC_COLORS["orders"],
            marker="o",
            markersize=9,
            markeredgecolor="white",
            markeredgewidth=1.2,
            linewidth=2.2,
            linestyle="-",
            zorder=4,
            label=METRIC_LABELS["orders"],
        )

        ax1.set_xticks(x)
        tick_labels = ax1.set_xticklabels(channel_labels, fontsize=11, fontweight="600")
        for tick, platform in zip(tick_labels, platforms):
            tick.set_color(PLATFORM_COLORS.get(platform, "#222222"))
        ax1.set_ylabel(
            f"Revenue / COGS / Spend / Profit ({_currency_symbol()})",
            fontsize=10,
            color="#333333",
            labelpad=10,
        )
        ax2.set_ylabel("Orders", fontsize=10, color="#444444", labelpad=10)
        ax2.tick_params(axis="y", labelcolor="#444444")

        ax1.set_facecolor("#FAFBFC")
        ax1.grid(axis="y", alpha=0.32, linestyle="-", color="#CCCCCC", zorder=0)
        ax1.set_axisbelow(True)
        for spine in ("top",):
            ax1.spines[spine].set_visible(False)
        ax2.spines["top"].set_visible(False)
        ax1.spines["left"].set_color("#BBBBBB")
        ax1.spines["bottom"].set_color("#BBBBBB")
        ax2.spines["right"].set_color("#BBBBBB")

        from matplotlib.ticker import MaxNLocator

        ax1.yaxis.set_major_locator(MaxNLocator(nbins=6, prune=None))
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(_format_inr_axis))
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))

        money_vals = np.concatenate([rev_vals, cogs_vals, spend_vals, profit_vals])
        money_max = max(float(np.max(money_vals)), 1.0)
        money_min = min(float(np.min(profit_vals)), 0.0)
        order_max = max(float(order_vals.max()), 1.0)
        y_span = money_max - money_min
        ax1.set_ylim(money_min - y_span * 0.08, money_max * 1.22)
        if money_min < 0:
            ax1.axhline(0, color="#999999", linewidth=0.8, zorder=1)
        ax2.set_ylim(0, order_max * 1.28)

        _add_bar_labels(ax1, bars_rev, rev_vals, _format_inr)
        _add_bar_labels(ax1, bars_cogs, cogs_vals, _format_inr)
        _add_bar_labels(ax1, bars_spend, spend_vals, _format_inr, zero_label="—")
        _add_bar_labels(ax1, bars_profit, profit_vals, _format_inr, min_height_frac=0.02)
        for xi, val in zip(x, order_vals):
            if val <= 0:
                continue
            ax2.annotate(
                f"{int(val):,}",
                (xi, val),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=8.5,
                fontweight="600",
                color="#1a1a1a",
            )

        ax1.set_xlim(-0.75, n - 0.25)

        subtitle_line1 = (
            f"Total revenue: {_format_inr(total_rev)}   ·   "
            f"Total COGS: {_format_inr(total_cogs)}   ·   "
            f"Total ad spend: {_format_inr(total_spend)}"
        )
        subtitle_line2 = (
            f"Net profit: {_format_inr(total_net_profit)}   ·   "
            f"Orders: {total_orders:,}   ·   "
            f"Blended gross ROAS: {total_gross_roas:.2f}x"
        )

        fig.suptitle(title, fontsize=15, fontweight="bold", color="#1a1a1a", y=0.97)
        fig.text(0.5, 0.905, subtitle_line1, ha="center", va="top", fontsize=9.5, color="#555555")
        fig.text(0.5, 0.878, subtitle_line2, ha="center", va="top", fontsize=9.5, color="#555555")

        from matplotlib.patches import Patch
        from matplotlib.lines import Line2D

        metric_handles = [
            Patch(facecolor=METRIC_COLORS["revenue"], edgecolor="white", label=METRIC_LABELS["revenue"]),
            Patch(facecolor=METRIC_COLORS["cogs"], edgecolor="white", label=METRIC_LABELS["cogs"]),
            Patch(facecolor=METRIC_COLORS["ad_spend"], edgecolor="white", label=METRIC_LABELS["ad_spend"]),
            Patch(facecolor=METRIC_COLORS["net_profit"], edgecolor="white", label=METRIC_LABELS["net_profit"]),
            Line2D(
                [0],
                [0],
                color=METRIC_COLORS["orders"],
                marker="o",
                markersize=7,
                linewidth=2,
                label=METRIC_LABELS["orders"],
            ),
        ]
        legend_y = 0.848
        fig.legend(
            handles=metric_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, legend_y),
            ncol=len(metric_handles),
            frameon=True,
            fontsize=8.5,
            edgecolor="#DDDDDD",
            facecolor="white",
        )
        fig.text(
            0.5,
            legend_y - 0.048,
            "Bars per channel (left → right): Revenue · COGS · Ad Spend · Net Profit  |  Orders line (right axis)",
            ha="center",
            va="top",
            fontsize=8.5,
            color="#666666",
            style="italic",
        )

        if ax_roas is not None:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
            main_start_str = (
                end_dt - timedelta(days=roas_trend_days - 1)
            ).strftime("%Y-%m-%d")
            _plot_roas_by_day(
                ax_roas,
                trend_raw,
                roas_trend_days=roas_trend_days,
                legend_below=False,
                min_plot_date=main_start_str,
                platforms=["meta", "google"],
                show_legend=True,
            )

        if ax_amazon_roas is not None:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
            amazon_start_str = (
                end_dt - timedelta(days=amazon_roas_trend_days - 1)
            ).strftime("%Y-%m-%d")
            _plot_roas_by_day(
                ax_amazon_roas,
                trend_raw,
                roas_trend_days=amazon_roas_trend_days,
                legend_below=False,
                min_plot_date=amazon_start_str,
                platforms=["amazon"],
                title=f"Amazon ROAS — Last {amazon_roas_trend_days} Days",
                show_legend=False,
            )

        if ax_roas is not None or ax_amazon_roas is not None:
            layout_rect = [0.04, 0.03, 0.96, 0.80]
        else:
            layout_rect = [0.04, 0.06, 0.96, 0.82]

        plt.tight_layout(rect=layout_rect)
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches="tight", facecolor="white")
            plt.close(fig)
            logger.info("Channel performance chart saved: %s", save_path)
            return save_path
        plt.close(fig)
        return None
    except Exception as e:
        logger.error("Channel performance chart error: %s", e, exc_info=True)
        return None


def plot_channel_performance_daily(
    report_date: str | date | datetime,
    save_path: Optional[str] = None,
    brand_id: Optional[int] = None,
) -> Optional[str]:
    """Single-day channel chart — daily marketing email only (report date)."""
    day_str = _to_date_str(report_date)
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d")
        label = f"Daily — {dt.strftime('%d %b %Y')}"
    except ValueError:
        label = f"Daily — {day_str}"
    return plot_channel_performance(
        day_str,
        day_str,
        save_path=save_path,
        brand_id=brand_id,
        period_label=label,
        include_roas_trend=True,
        roas_trend_days=14,
        amazon_roas_trend_days=14,
    )


def plot_channel_performance_last_7_days(
    end_date: str | date | datetime,
    save_path: Optional[str] = None,
    brand_id: Optional[int] = None,
) -> Optional[str]:
    """Rolling 7-day channel chart — WTD/MTD email only (not the daily marketing email)."""
    end_str = _to_date_str(end_date)
    end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
    start_dt = end_dt - timedelta(days=6)
    return plot_channel_performance(
        start_dt,
        end_dt,
        save_path=save_path,
        brand_id=brand_id,
        period_label=f"Last 7 Days ({start_dt.strftime('%d %b')} – {end_dt.strftime('%d %b %Y')})",
    )
