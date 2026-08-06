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
# Prefer order-date cohort (same as daily Net Profit charts) for PM metrics.
_USE_ORDER_DATE_COHORT = os.getenv("CHANNEL_FROM_ORDER_DATE_COHORT", "true").lower() in (
    "1",
    "true",
    "yes",
)

try:
    from amazon_entity_report import get_clickhouse_client
except ImportError:
    get_clickhouse_client = None

PLATFORM_ORDER = ["meta", "google", "organic", "amazon", "other"]
# Days with spend below this floor produce undefined ROAS (avoid 100×–1000×
# spikes when ads ingest collapses while marketplace sales continue).
MIN_SPEND_FOR_ROAS = 100.0
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
    "gross_profit": "#1565C0",
    "orders": "#4A56E2",
}
METRIC_LABELS = {
    "revenue": "Gross Sales",
    "cogs": "Gross COGS",
    "ad_spend": "Ad Spend",
    "net_profit": "Gross Profit",
    "gross_profit": "Gross Profit",
    "orders": "Orders",
}

# Stakeholder-facing visual system (daily email charts)
TYPE_SCALE = {
    "title": 13,
    "subtitle": 9,
    "axis": 9,
    "tick": 8,
    "legend": 8,
    "label": 7,
}
LINE_STYLE = {
    "primary": 1.9,
    "secondary": 1.45,
    "marker": 3.0,
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
        "gross_roas": (
            round(rev / spend, 2) if spend >= MIN_SPEND_FOR_ROAS else None
        ),
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
            "gross_roas": round(rev / spend, 2) if spend >= MIN_SPEND_FOR_ROAS else None,
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
                "gross_roas": round(rev / spend, 2) if spend >= MIN_SPEND_FOR_ROAS else None,
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


def _fetch_channel_performance_from_order_date_cohort(
    start_str: str,
    end_str: str,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """
    Map order-date cohort rows into the channel_performance schema.

    Revenue = cohort net sales (gross − returns − cancels of orders placed that day).
    Same definition as the daily Net Profit dual-cohort charts.
    """
    from dashboard_stats import fetch_order_date_cohort_rows

    if brand_id is None:
        brand_id = get_brand_id()
    rows = fetch_order_date_cohort_rows(int(brand_id), start_str, end_str)
    if rows is None or rows.empty:
        return pd.DataFrame()

    out = pd.DataFrame(
        {
            "report_date": pd.to_datetime(rows["report_date"]).dt.strftime("%Y-%m-%d"),
            "platform": rows["platform"].astype(str),
            "attributed_orders": pd.to_numeric(rows["orders"], errors="coerce")
            .fillna(0)
            .astype(int),
            # Bars use order-date net sales (after returns/cancels)
            "gross_revenue_excl_gst": pd.to_numeric(rows["net_sales"], errors="coerce")
            .fillna(0)
            .round(2),
            "gross_sales": pd.to_numeric(rows["gross_sales"], errors="coerce")
            .fillna(0)
            .round(2),
            "gross_cogs": pd.to_numeric(rows["gross_cogs"], errors="coerce")
            .fillna(0)
            .round(2),
            "cogs": pd.to_numeric(rows["net_cogs"], errors="coerce").fillna(0).round(2),
            "ad_spend": pd.to_numeric(rows["ad_spend"], errors="coerce").fillna(0).round(2),
            "net_profit": pd.to_numeric(rows["net_profit"], errors="coerce")
            .fillna(0)
            .round(2),
        }
    )
    spend = out["ad_spend"].astype(float)
    net_sales = out["gross_revenue_excl_gst"].astype(float)
    gross_sales = out["gross_sales"].astype(float)
    gross_cogs = out["gross_cogs"].astype(float)
    cogs = out["cogs"].astype(float)
    # Classic Gross ROAS = gross sales / spend
    out["dashboard_gross_roas"] = np.where(
        spend >= MIN_SPEND_FOR_ROAS,
        (gross_sales / spend).round(2),
        np.nan,
    )
    # Sales ROAS = net sales / spend
    out["gross_roas"] = np.where(
        spend >= MIN_SPEND_FOR_ROAS,
        (net_sales / spend).round(2),
        np.nan,
    )
    # Contribution ROAS = (gross sales − gross cogs) / spend
    # (user framing: gross sales − gross cogs vs ad spend)
    out["contrib_roas"] = np.where(
        spend >= MIN_SPEND_FOR_ROAS,
        ((gross_sales - gross_cogs) / spend).round(2),
        np.nan,
    )
    # Net ROAS = (net sales − net cogs) / spend
    out["net_roas"] = np.where(
        spend >= MIN_SPEND_FOR_ROAS,
        ((net_sales - cogs) / spend).round(2),
        np.nan,
    )
    # Contribution after ads (₹) — not a ROAS multiple
    out["contrib_after_ads"] = (gross_sales - gross_cogs - spend).round(2)
    return out.reset_index(drop=True)


def fetch_channel_performance(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch daily channel performance rows (order-date cohort first, then API/CH)."""
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    if brand_id is None:
        brand_id = get_brand_id()

    if _USE_ORDER_DATE_COHORT:
        try:
            df = _fetch_channel_performance_from_order_date_cohort(
                start_str, end_str, brand_id=brand_id
            )
            if not df.empty:
                logger.info(
                    "channel performance from order-date cohort (%d rows, %s→%s)",
                    len(df),
                    start_str,
                    end_str,
                )
                return df
            logger.warning(
                "order-date cohort returned empty for %s→%s; falling back",
                start_str,
                end_str,
            )
        except Exception as e:
            logger.warning(
                "order-date cohort channel fetch failed (%s); falling back", e
            )

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
    for col in ("gross_sales", "gross_cogs"):
        if col not in df.columns:
            df = df.copy()
            df[col] = (
                df["gross_revenue_excl_gst"] if col == "gross_sales" else df["cogs"]
            )
    agg = (
        df.groupby("platform", as_index=False)
        .agg(
            attributed_orders=("attributed_orders", "sum"),
            gross_revenue_excl_gst=("gross_revenue_excl_gst", "sum"),
            gross_sales=("gross_sales", "sum"),
            gross_cogs=("gross_cogs", "sum"),
            cogs=("cogs", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
    )
    agg["net_profit"] = agg["gross_revenue_excl_gst"] - agg["cogs"] - agg["ad_spend"]
    # Gross profit (user parity): gross sales − gross cogs − ad spend
    agg["gross_profit"] = agg["gross_sales"] - agg["gross_cogs"] - agg["ad_spend"]
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
    agg["contrib_roas"] = np.where(
        agg["ad_spend"] > 0,
        (agg["gross_sales"] - agg["gross_cogs"]) / agg["ad_spend"],
        np.nan,
    )
    agg["gross_revenue_excl_gst"] = agg["gross_revenue_excl_gst"].round(2)
    agg["gross_sales"] = agg["gross_sales"].round(2)
    agg["gross_cogs"] = agg["gross_cogs"].round(2)
    agg["cogs"] = agg["cogs"].round(2)
    agg["ad_spend"] = agg["ad_spend"].round(2)
    agg["net_profit"] = agg["net_profit"].round(2)
    agg["gross_profit"] = agg["gross_profit"].round(2)
    agg["gross_roas"] = agg["gross_roas"].round(2)
    agg["net_roas"] = agg["net_roas"].round(2)
    agg["contrib_roas"] = agg["contrib_roas"].round(2)
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


def _smooth_line_segments(x: np.ndarray, y: np.ndarray, *, points_per_seg: int = 12):
    """
    Shape-preserving smooth curves between finite points (PCHIP).
    Returns (x_smooth, y_smooth) with NaN breaks where the source has gaps.
    Falls back to the raw series if scipy is unavailable or too few points.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 2:
        return x, y

    try:
        from scipy.interpolate import PchipInterpolator
    except ImportError:
        return x, y

    xs_parts: list[np.ndarray] = []
    ys_parts: list[np.ndarray] = []
    finite = np.isfinite(y)
    i = 0
    n = len(x)
    while i < n:
        if not finite[i]:
            i += 1
            continue
        j = i
        while j < n and finite[j]:
            j += 1
        seg_x = x[i:j]
        seg_y = y[i:j]
        if len(seg_x) == 1:
            xs_parts.append(seg_x)
            ys_parts.append(seg_y)
        elif len(seg_x) == 2:
            xs_parts.append(seg_x)
            ys_parts.append(seg_y)
        else:
            # densify each contiguous run
            n_out = max(len(seg_x) * points_per_seg, len(seg_x) + 1)
            x_new = np.linspace(seg_x[0], seg_x[-1], n_out)
            try:
                y_new = PchipInterpolator(seg_x, seg_y)(x_new)
            except Exception:
                y_new = np.interp(x_new, seg_x, seg_y)
            xs_parts.append(x_new)
            ys_parts.append(y_new)
        # gap separator so matplotlib does not bridge NaN holes
        if j < n:
            xs_parts.append(np.array([np.nan]))
            ys_parts.append(np.array([np.nan]))
        i = j

    if not xs_parts:
        return x, y
    return np.concatenate(xs_parts), np.concatenate(ys_parts)


def _place_roas_labels(ax, label_specs: list[dict], *, y_span: float):
    """
    Place ROAS labels. Same-day labels are vertically fanned; endpoint callouts
    (offset_x > 4) are stacked to the right of the last point.
    """
    if not label_specs:
        return

    by_x: dict[int, list[dict]] = {}
    for spec in label_specs:
        key = int(round(spec["x"]))
        by_x.setdefault(key, []).append(spec)

    base_gap = max(9.0, min(16.0, 11.0 + (3.0 - min(3.0, y_span)) * 1.2))

    for day_specs in by_x.values():
        day_specs.sort(key=lambda s: s["y"])
        n = len(day_specs)
        for rank, spec in enumerate(day_specs):
            prefer_above = bool(spec.get("prefer_above", True))
            ox = float(spec.get("offset_x", 0.0))
            is_endpoint = ox >= 4.0
            if n == 1:
                offset_y = 8.0 if prefer_above else -10.0
            elif is_endpoint:
                # Right-side stack: spread by rank so all final values stay readable
                mid = (n - 1) / 2.0
                offset_y = (rank - mid) * max(base_gap, 13.0)
                ox = 10.0 + (rank % 2) * 2.0
            elif n == 2:
                offset_y = -11.0 if rank == 0 else 9.0
            else:
                mid = (n - 1) / 2.0
                offset_y = (rank - mid) * base_gap
                if abs(offset_y) < 6.0:
                    offset_y = 7.0 if prefer_above else -9.0
            if spec["y"] <= 0:
                offset_y = -abs(offset_y)
            ax.annotate(
                spec["text"],
                (spec["x"], spec["y"]),
                textcoords="offset points",
                xytext=(ox, offset_y),
                ha="left" if is_endpoint else "center",
                va="center" if is_endpoint else ("bottom" if offset_y >= 0 else "top"),
                fontsize=spec.get("fontsize", 6.0),
                fontweight=spec.get("fontweight", "600"),
                color=spec["color"],
                zorder=6,
                annotation_clip=False,
            )


def _prepare_daily_roas(
    raw: pd.DataFrame,
    *,
    min_plot_date: Optional[str] = None,
    metric: str = "contrib_roas",
) -> pd.DataFrame:
    """
    Daily ROAS per platform.

    metric:
      - contrib_roas: (gross_sales − gross_cogs) / spend  ← default (user definition)
      - net_roas: (net_sales − net_cogs) / spend
      - gross_roas: net_sales / spend                     (Sales ROAS)
      - dashboard_gross_roas: gross_sales / spend

    ROAS is undefined (NaN) when that day's spend is below MIN_SPEND_FOR_ROAS.
    Do NOT borrow prior-day spend — that produced Amazon 900×+ spikes when ads
    reporting dropped to near-zero while marketplace Net Sales kept posting.
    """
    if raw.empty:
        return raw
    daily = raw.copy()
    daily["report_date"] = pd.to_datetime(daily["report_date"])
    for col in ("gross_roas", "net_roas", "dashboard_gross_roas", "contrib_roas"):
        if col not in daily.columns:
            daily[col] = np.nan

    for platform in PLATFORM_ORDER:
        plat = daily[daily["platform"] == platform].sort_values("report_date")
        if plat.empty:
            continue
        g_roas, n_roas, d_roas, c_roas = [], [], [], []
        for _, row in plat.iterrows():
            spend = float(row.get("ad_spend") or 0)
            net_sales = float(row.get("gross_revenue_excl_gst") or 0)
            cogs = float(row.get("cogs") or 0)
            gross_sales = float(row.get("gross_sales") or net_sales)
            gross_cogs = float(row.get("gross_cogs") or cogs)
            if spend >= MIN_SPEND_FOR_ROAS:
                g_roas.append(net_sales / spend)
                n_roas.append((net_sales - cogs) / spend)
                d_roas.append(gross_sales / spend)
                c_roas.append((gross_sales - gross_cogs) / spend)
            else:
                g_roas.append(np.nan)
                n_roas.append(np.nan)
                d_roas.append(np.nan)
                c_roas.append(np.nan)
        daily.loc[plat.index, "gross_roas"] = g_roas
        daily.loc[plat.index, "net_roas"] = n_roas
        daily.loc[plat.index, "dashboard_gross_roas"] = d_roas
        daily.loc[plat.index, "contrib_roas"] = c_roas

    if min_plot_date:
        min_dt = pd.to_datetime(min_plot_date)
        daily = daily[daily["report_date"] >= min_dt]

    metric_col = metric if metric in daily.columns else "contrib_roas"
    daily["plot_roas"] = pd.to_numeric(daily[metric_col], errors="coerce")

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
    metric: str = "contrib_roas",
    include_total: bool = True,
) -> bool:
    """Line chart: ROAS per channel (+ optional blended Total) for the window."""
    daily = _prepare_daily_roas(raw, min_plot_date=min_plot_date, metric=metric)
    if daily.empty:
        ax.set_visible(False)
        return False

    plot_platforms = platforms or [
        p for p in ("meta", "google", "amazon") if p in PLATFORM_ORDER
    ]
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
    n_days = len(dates)
    # Mid-chart labels only on Total (every N days). Channel values appear as
    # right-side endpoint callouts — this is the only layout that stays readable
    # when Meta / Google / Total trade inside a tight 0.8–1.5 band.
    label_step = 5 if n_days > 20 else 3
    label_specs: list[dict] = []
    series_prefer_above = {
        "meta": True,
        "google": False,
        "amazon": True,
        "organic": False,
        "other": True,
        "_total": True,
    }

    def _draw_smooth_series(y_vals, *, color, label, marker, zorder, series_key, is_total=False):
        nonlocal plotted
        y = np.asarray(y_vals, dtype=float)
        if np.all(np.isnan(y)):
            return
        plotted = True
        x_s, y_s = _smooth_line_segments(x, y, points_per_seg=24)
        ax.plot(
            x_s,
            y_s,
            linewidth=2.25 if is_total else LINE_STYLE["primary"],
            color=color,
            solid_capstyle="round",
            solid_joinstyle="round",
            label=label,
            zorder=zorder,
            alpha=0.95,
            antialiased=True,
        )
        marker_step = 2 if len(x) > 20 else 1
        ax.plot(
            x[::marker_step],
            y[::marker_step],
            linestyle="none",
            marker=marker,
            markersize=3.4 if is_total else LINE_STYLE["marker"],
            color=color,
            markerfacecolor=color,
            markeredgecolor="white",
            markeredgewidth=0.55,
            zorder=zorder + 1,
        )
        prefer_above = series_prefer_above.get(series_key, True)
        last_finite = None
        for i in range(len(y) - 1, -1, -1):
            if np.isfinite(y[i]):
                last_finite = i
                break
        for i, (xi, val) in enumerate(zip(x, y)):
            if np.isnan(val):
                continue
            is_last = last_finite is not None and i == last_finite
            if is_last:
                label_specs.append(
                    {
                        "x": float(xi),
                        "y": float(val),
                        "text": f"{val:.2f}",
                        "color": color,
                        "fontsize": TYPE_SCALE["label"],
                        "fontweight": "700",
                        "prefer_above": prefer_above,
                        "offset_x": 10.0,
                    }
                )
                continue
            if is_total:
                if i == 0 or (i % label_step) != 0:
                    continue
                label_specs.append(
                    {
                        "x": float(xi),
                        "y": float(val),
                        "text": f"{val:.2f}",
                        "color": color,
                        "fontsize": TYPE_SCALE["label"],
                        "fontweight": "700",
                        "prefer_above": False if val <= 0 else True,
                        "offset_x": 0.0,
                    }
                )
                continue
            # Channel mid-labels only on clear peaks (keeps the crowded band clean)
            if val < 2.0 or i == 0 or i >= len(y) - 1:
                continue
            left = y[i - 1]
            right = y[i + 1]
            if np.isnan(left) or np.isnan(right):
                continue
            if val >= left and val >= right and val >= (np.nanmax(y) * 0.55):
                label_specs.append(
                    {
                        "x": float(xi),
                        "y": float(val),
                        "text": f"{val:.2f}",
                        "color": color,
                        "fontsize": TYPE_SCALE["label"] - 0.3,
                        "fontweight": "600",
                        "prefer_above": True,
                        "offset_x": 0.0,
                    }
                )

    for platform in plot_platforms:
        plat = daily[daily["platform"] == platform]
        if plat.empty:
            continue
        series = plat.set_index("report_date")["plot_roas"].reindex(dates)
        _draw_smooth_series(
            series.values.astype(float),
            color=PLATFORM_COLORS.get(platform, "#666666"),
            label=PLATFORM_LABELS.get(platform, platform.title()),
            marker=PLATFORM_MARKERS.get(platform, "o"),
            zorder=3,
            series_key=platform,
        )

    if include_total and plotted:
        paid = daily[daily["platform"].isin(plot_platforms)].copy()
        totals = []
        for d in dates:
            day = paid[paid["report_date"] == d]
            spend = float(pd.to_numeric(day["ad_spend"], errors="coerce").fillna(0).sum())
            net_sales = float(
                pd.to_numeric(day["gross_revenue_excl_gst"], errors="coerce")
                .fillna(0)
                .sum()
            )
            cogs = float(pd.to_numeric(day["cogs"], errors="coerce").fillna(0).sum())
            if "gross_sales" in day.columns:
                gross_sales = float(
                    pd.to_numeric(day["gross_sales"], errors="coerce").fillna(0).sum()
                )
            else:
                gross_sales = net_sales
            if "gross_cogs" in day.columns:
                gross_cogs = float(
                    pd.to_numeric(day["gross_cogs"], errors="coerce").fillna(0).sum()
                )
            else:
                gross_cogs = cogs
            if spend < MIN_SPEND_FOR_ROAS:
                totals.append(np.nan)
            elif metric == "dashboard_gross_roas":
                totals.append(gross_sales / spend)
            elif metric == "gross_roas":
                totals.append(net_sales / spend)
            elif metric == "contrib_roas":
                totals.append((gross_sales - gross_cogs) / spend)
            else:
                totals.append((net_sales - cogs) / spend)
        y_tot = np.asarray(totals, dtype=float)
        if not np.all(np.isnan(y_tot)):
            _draw_smooth_series(
                y_tot,
                color="#0F172A",
                label="Total",
                marker="P",
                zorder=4,
                series_key="_total",
                is_total=True,
            )

    if not plotted:
        ax.set_visible(False)
        return False

    metric_label = {
        "contrib_roas": "ROAS",
        "net_roas": "Net ROAS",
        "gross_roas": "Sales ROAS",
        "dashboard_gross_roas": "Gross ROAS",
    }.get(metric, "ROAS")

    if title:
        chart_title = title
    else:
        chart_title = f"{metric_label} Trend by Channel — Last {roas_trend_days} Days"

    ax.set_title(
        chart_title,
        fontsize=TYPE_SCALE["title"] - 1,
        fontweight="bold",
        color="#1a1a1a",
        pad=18,
    )
    formula_caption = {
        "contrib_roas": "ROAS = (Sales - COGS) / Ad Spend · Total = blended paid channels",
        "net_roas": "Net ROAS = (Net Sales - Net COGS) / Ad Spend · Total = blended paid channels",
        "gross_roas": "Sales ROAS = Net Sales / Ad Spend · Total = blended paid channels",
        "dashboard_gross_roas": "Gross ROAS = Gross Sales / Ad Spend · Total = blended paid channels",
    }.get(metric, "ROAS · Total = blended paid channels")
    ax.text(
        0.5,
        1.015,
        formula_caption,
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=TYPE_SCALE["subtitle"] - 1,
        color="#666666",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(
        date_labels,
        fontsize=TYPE_SCALE["tick"] - 1 if len(dates) > 20 else TYPE_SCALE["tick"],
        rotation=90,
        ha="center",
    )
    # Room on the right for endpoint callouts
    ax.set_xlim(-0.6, len(dates) - 0.4 + 1.35)
    ax.set_ylabel(metric_label, fontsize=TYPE_SCALE["axis"], color="#333333", labelpad=8)
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

    valid = daily["plot_roas"].dropna()
    y_span = 1.0
    if not valid.empty:
        ymax = float(valid.max())
        ymin = float(valid.min())
        lower = min(ymin * 1.15, 0.0) if ymin < 0 else 0.0
        # Fit every channel fully — no soft-cap that clips Amazon spikes
        upper = max(ymax * 1.22, 0.5)
        ax.set_ylim(lower, upper)
        y_span = upper - lower
        if lower < 0:
            ax.axhline(0, color="#999999", linewidth=0.8, zorder=1)
    if label_specs:
        _place_roas_labels(ax, label_specs, y_span=y_span)
    if show_legend:
        n_legend = len(plot_platforms) + (1 if include_total else 0)
        legend_kwargs = dict(
            frameon=True,
            fontsize=TYPE_SCALE["legend"],
            title="Channels",
            title_fontsize=TYPE_SCALE["legend"],
            edgecolor="#DDDDDD",
            facecolor="white",
        )
        if legend_below:
            ax.legend(
                loc="upper center",
                bbox_to_anchor=(0.5, -0.22),
                ncol=min(4, n_legend),
                **legend_kwargs,
            )
        else:
            ax.legend(loc="upper left", ncol=1, **legend_kwargs)
    return True


def fetch_top_subchannels(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
    limit: int = 5,
) -> pd.DataFrame:
    """
    Top sub-channels by sessions from gold.mart_funnel_daily
    (same source as dashboard Funnel → Sub-channel tab).
    """
    if get_clickhouse_client is None:
        return pd.DataFrame()
    if brand_id is None:
        brand_id = get_brand_id()
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    sql = f"""
        SELECT
            lowerUTF8(trimBoth(coalesce(m.channel, '(none)'))) AS sub_channel,
            sum(toInt64(coalesce(m.sessions, 0))) AS sessions,
            sum(toInt64(coalesce(m.pdp_sessions, 0))) AS pdp_sessions,
            sum(toInt64(coalesce(m.atc_sessions, 0))) AS atc_sessions,
            sum(toInt64(coalesce(m.checkout_sessions, 0))) AS checkout_sessions,
            sum(toInt64(coalesce(m.converted_sessions, 0))) AS converted_sessions,
            sum(toInt64(coalesce(m.purchases, 0))) AS purchases
        FROM gold.mart_funnel_daily AS m
        WHERE m.brand_id = %(b)s
          AND m.report_date >= toDate(%(s)s)
          AND m.report_date <= toDate(%(e)s)
        GROUP BY sub_channel
        ORDER BY sessions DESC
        LIMIT {int(limit)}
    """
    try:
        client = get_clickhouse_client()
        result = client.query(sql, parameters={"b": int(brand_id), "s": start_str, "e": end_str})
        cols = list(result.column_names)
        rows = [dict(zip(cols, row)) for row in result.result_rows]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        for col in (
            "sessions",
            "pdp_sessions",
            "atc_sessions",
            "checkout_sessions",
            "converted_sessions",
            "purchases",
        ):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        # Match dashboard Sub-channel "Purchase" column (= converted_sessions)
        df["orders"] = df["converted_sessions"]
        return df
    except Exception as e:
        logger.warning("Could not load sub-channel funnel: %s", e)
        return pd.DataFrame()


def _format_compact_int(value: float | int) -> str:
    v = float(value)
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}K".rstrip("0").rstrip(".")
    return f"{int(v):,}"


def _plot_subchannel_panel(ax, sub_df: pd.DataFrame, *, period_label: str = "") -> bool:
    """Right-column panel: top sub-channels with Sessions → ATC → Orders."""
    from matplotlib.patches import Rectangle

    if sub_df is None or sub_df.empty:
        ax.set_visible(False)
        return False

    ax.set_facecolor("#FAFBFC")
    for spine in ("top", "right", "left", "bottom"):
        ax.spines[spine].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    title = "Top 5 Sub-channels"
    if period_label:
        title = f"{title} · {period_label}"
    ax.set_title(title, fontsize=11, fontweight="bold", color="#1a1a1a", pad=10, loc="left")
    ax.text(
        0.02,
        0.92,
        "Sessions  →  ATC  →  Orders",
        transform=ax.transAxes,
        fontsize=8.0,
        color="#666666",
        ha="left",
        va="top",
    )

    # Header
    headers = ["Sub-channel", "Sessions", "ATC", "Orders", "S→O"]
    xs = [0.02, 0.38, 0.56, 0.72, 0.88]
    for x, h in zip(xs, headers):
        ax.text(
            x,
            0.82,
            h,
            transform=ax.transAxes,
            fontsize=7.5,
            fontweight="700",
            color="#555555",
            ha="left" if x < 0.3 else "right",
            va="center",
        )
    ax.plot([0.02, 0.98], [0.78, 0.78], transform=ax.transAxes, color="#E2E8F0", linewidth=0.8)

    n = len(sub_df)
    row_h = 0.12
    y0 = 0.68
    max_sessions = max(float(sub_df["sessions"].max()), 1.0)

    for i, row in enumerate(sub_df.itertuples(index=False)):
        y = y0 - i * row_h
        name = str(getattr(row, "sub_channel", "") or "—")
        sessions = int(getattr(row, "sessions", 0) or 0)
        atc = int(getattr(row, "atc_sessions", 0) or 0)
        orders = int(getattr(row, "orders", 0) or 0)
        conv = (orders / sessions * 100.0) if sessions > 0 else 0.0

        # Session bar background
        bar_w = 0.28 * (sessions / max_sessions)
        ax.add_patch(
            Rectangle(
                (0.02, y - 0.035),
                bar_w,
                0.06,
                transform=ax.transAxes,
                facecolor="#DBEAFE",
                edgecolor="none",
                zorder=1,
            )
        )
        ax.text(
            0.02,
            y,
            name[:18],
            transform=ax.transAxes,
            fontsize=8.5,
            fontweight="600",
            color="#1a1a1a",
            ha="left",
            va="center",
            zorder=2,
        )
        ax.text(
            0.38,
            y,
            _format_compact_int(sessions),
            transform=ax.transAxes,
            fontsize=8.5,
            color="#1a1a1a",
            ha="right",
            va="center",
        )
        ax.text(
            0.56,
            y,
            _format_compact_int(atc),
            transform=ax.transAxes,
            fontsize=8.5,
            color="#1a1a1a",
            ha="right",
            va="center",
        )
        ax.text(
            0.72,
            y,
            _format_compact_int(orders),
            transform=ax.transAxes,
            fontsize=8.5,
            fontweight="600",
            color="#1a1a1a",
            ha="right",
            va="center",
        )
        ax.text(
            0.88,
            y,
            f"{conv:.1f}%",
            transform=ax.transAxes,
            fontsize=8.0,
            color="#555555",
            ha="right",
            va="center",
        )

        # Mini funnel spark: sessions → atc → orders as proportional dots
        funnel_x = [0.42, 0.50, 0.58]
        # Keep funnel visual in the gap between name and numbers — skip if crowded
        _ = (n, funnel_x)  # layout reserved; numbers already show the funnel

    ax.text(
        0.02,
        0.02,
        "Source: session funnel mart · S→O = orders / sessions",
        transform=ax.transAxes,
        fontsize=6.5,
        color="#888888",
        ha="left",
        va="bottom",
    )
    return True


def _plot_roas_dual_axis_trend(
    ax,
    raw: pd.DataFrame,
    *,
    roas_trend_days: int = 30,
    min_plot_date: Optional[str] = None,
) -> bool:
    """
    All Channels dual-axis trend:
      left  — Gross Sales + Ad Spend + Gross Profit bars (blended)
      right — Total (blended) ROAS only — channel lines live on a separate chart
      labels — Total ROAS numbers on the line
    """
    if raw is None or raw.empty:
        ax.set_visible(False)
        return False

    daily = raw.copy()
    daily["report_date"] = pd.to_datetime(daily["report_date"])
    if min_plot_date:
        daily = daily[daily["report_date"] >= pd.to_datetime(min_plot_date)]
    if daily.empty:
        ax.set_visible(False)
        return False

    for col in ("gross_sales", "gross_cogs", "ad_spend", "cogs", "gross_revenue_excl_gst"):
        if col not in daily.columns:
            if col == "gross_sales":
                daily[col] = daily.get("gross_revenue_excl_gst", 0)
            elif col == "gross_cogs":
                daily[col] = daily.get("cogs", 0)
            else:
                daily[col] = 0
        daily[col] = pd.to_numeric(daily[col], errors="coerce").fillna(0)

    paid_platforms = ["meta", "google", "amazon"]
    paid = daily[daily["platform"].isin(paid_platforms)]
    if paid.empty:
        paid = daily

    totals = (
        paid.groupby("report_date", as_index=False)
        .agg(
            gross_sales=("gross_sales", "sum"),
            gross_cogs=("gross_cogs", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
        .sort_values("report_date")
    )
    totals["gross_profit"] = (
        totals["gross_sales"] - totals["gross_cogs"] - totals["ad_spend"]
    )
    if len(totals) < 2:
        ax.set_visible(False)
        return False

    dates = list(totals["report_date"])
    x = np.arange(len(dates))
    date_labels = [pd.Timestamp(d).strftime("%d %b") for d in dates]
    n = len(dates)

    bar_w = 0.22
    offsets = np.array([-0.75, 0.0, 0.75], dtype=float) * bar_w

    gs_vals = totals["gross_sales"].values.astype(float)
    sp_vals = totals["ad_spend"].values.astype(float)
    gp_vals = totals["gross_profit"].values.astype(float)

    ax.bar(
        x + offsets[0],
        gs_vals,
        bar_w,
        color=METRIC_COLORS["revenue"],
        alpha=0.55,
        edgecolor="white",
        linewidth=0.3,
        label="Gross Sales",
        zorder=2,
    )
    ax.bar(
        x + offsets[1],
        sp_vals,
        bar_w,
        color=METRIC_COLORS["ad_spend"],
        alpha=0.85,
        edgecolor="white",
        linewidth=0.3,
        label="Ad Spend",
        zorder=2,
    )
    gp_colors = [
        METRIC_COLORS["gross_profit"] if v >= 0 else "#C62828" for v in gp_vals
    ]
    ax.bar(
        x + offsets[2],
        gp_vals,
        bar_w,
        color=gp_colors,
        alpha=0.90,
        edgecolor="white",
        linewidth=0.3,
        label="Gross Profit",
        zorder=2,
    )

    money = np.concatenate([gs_vals, sp_vals, gp_vals])
    money_max = max(float(np.nanmax(money)), 1.0)
    money_min = min(float(np.nanmin(gp_vals)), 0.0)
    span = max(money_max - money_min, 1.0)
    ax.set_ylim(money_min - span * 0.05, money_max * 1.22)
    if money_min < 0:
        ax.axhline(0, color="#999999", linewidth=0.7, zorder=1)

    # Blended Total ROAS = (GS − GC) / Spend across paid channels
    y_tot = np.full(n, np.nan)
    for i, d in enumerate(dates):
        day = totals[totals["report_date"] == d].iloc[0]
        spend = float(day["ad_spend"])
        if spend >= MIN_SPEND_FOR_ROAS:
            y_tot[i] = (float(day["gross_sales"]) - float(day["gross_cogs"])) / spend

    ax2 = ax.twinx()
    if not np.all(np.isnan(y_tot)):
        x_s, y_s = _smooth_line_segments(x, y_tot, points_per_seg=28)
        ax2.plot(
            x_s,
            y_s,
            color="#0F172A",
            linewidth=2.5,
            solid_capstyle="round",
            solid_joinstyle="round",
            label="Total ROAS",
            zorder=6,
            alpha=0.95,
            antialiased=True,
        )
        marker_step = 2 if n > 20 else 1
        ax2.plot(
            x[::marker_step],
            y_tot[::marker_step],
            linestyle="none",
            marker="o",
            markersize=4.2,
            color="#0F172A",
            markerfacecolor="#0F172A",
            markeredgecolor="white",
            markeredgewidth=0.5,
            zorder=7,
        )

        finite = y_tot[~np.isnan(y_tot)]
        rmax = float(np.nanmax(finite)) if len(finite) else 1.0
        rmin = float(np.nanmin(finite)) if len(finite) else 0.0
        lower = min(rmin * 1.05, 0.0) if rmin < 0 else 0.0
        ax2.set_ylim(lower, max(rmax * 1.28, 1.5))

        # Numbers on the Total ROAS line (every few days + last point)
        label_step = 5 if n > 24 else 4 if n > 16 else 3
        label_idx = set(range(0, n, label_step))
        last_finite = None
        for i in range(n - 1, -1, -1):
            if np.isfinite(y_tot[i]):
                last_finite = i
                break
        if last_finite is not None:
            label_idx.add(last_finite)
        for i in sorted(label_idx):
            val = y_tot[i]
            if np.isnan(val):
                continue
            ax2.annotate(
                f"{val:.2f}",
                (x[i], val),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                va="bottom",
                fontsize=6.5,
                fontweight="700",
                color="#0F172A",
                zorder=8,
                annotation_clip=True,
            )

    ax.set_title(
        f"All Channels — Last {roas_trend_days} Days",
        fontsize=11,
        fontweight="bold",
        color="#1a1a1a",
        pad=8,
        loc="left",
    )
    ax.text(
        1.0,
        1.02,
        "ROAS = (GS − GC) ÷ Spend  ·  blended paid channels",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=7.0,
        color="#666666",
    )

    tick_step = 3 if n > 20 else 2
    tick_idx = list(range(0, n, tick_step))
    if tick_idx[-1] != n - 1:
        tick_idx.append(n - 1)
    ax.set_xticks([x[i] for i in tick_idx])
    ax.set_xticklabels(
        [date_labels[i] for i in tick_idx],
        fontsize=7.0,
        rotation=0,
        ha="center",
    )
    ax.set_ylabel(
        f"Amount ({_currency_symbol()})",
        fontsize=TYPE_SCALE["axis"],
        color="#333333",
    )
    ax2.set_ylabel("ROAS", fontsize=TYPE_SCALE["axis"], color="#333333")
    ax.set_facecolor("#FAFBFC")
    ax.grid(axis="y", alpha=0.22, linestyle="-", color="#E2E8F0", zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top",):
        ax.spines[spine].set_visible(False)
        ax2.spines[spine].set_visible(False)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(_format_inr_axis))
    ax.set_xlim(-0.6, n - 0.35)

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(
        h1 + h2,
        l1 + l2,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.14),
        ncol=4,
        fontsize=7.0,
        frameon=True,
        edgecolor="#E2E8F0",
        facecolor="white",
        framealpha=0.95,
    )
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
    Channel Performance canvas:
      top-left  — Gross Sales / Gross COGS / Ad Spend / Gross Profit bars
                  + Orders (twin axis)
      top-right — Top 5 sub-channels (Sessions → ATC → Orders)
      mid       — All Channels dual-axis (money bars + blended Total ROAS)
      bottom    — Channel-wise Gross ROAS Trend (Meta / Google / Amazon)

    Gross profit = gross sales − gross cogs − ad spend
    Total ROAS = (gross sales − gross cogs) / ad spend
    Channel Gross ROAS = gross sales / ad spend
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
        gs_col = "gross_sales" if "gross_sales" in df.columns else "gross_revenue_excl_gst"
        gc_col = "gross_cogs" if "gross_cogs" in df.columns else "cogs"
        if df.empty or (
            df[gs_col].sum() == 0
            and df[gc_col].sum() == 0
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
                df.loc[df["platform"] == p, gs_col].sum()
                + df.loc[df["platform"] == p, gc_col].sum()
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
            title = f"Channel Performance Summary — {period_label}"
        elif start_str == end_str:
            try:
                dt = datetime.strptime(end_str, "%Y-%m-%d")
                title = f"Channel Performance Summary — {dt.strftime('%d %b %Y')}"
            except ValueError:
                title = f"Channel Performance Summary — {end_str}"
        else:
            try:
                s_dt = datetime.strptime(start_str, "%Y-%m-%d")
                e_dt = datetime.strptime(end_str, "%Y-%m-%d")
                title = (
                    f"Channel Performance Summary — {s_dt.strftime('%d %b')} to "
                    f"{e_dt.strftime('%d %b %Y')}"
                )
            except ValueError:
                title = f"Channel Performance Summary — {start_str} to {end_str}"

        if "gross_sales" not in plot_df.columns:
            plot_df["gross_sales"] = plot_df["gross_revenue_excl_gst"]
        if "gross_cogs" not in plot_df.columns:
            plot_df["gross_cogs"] = plot_df["cogs"]
        if "gross_profit" not in plot_df.columns:
            plot_df["gross_profit"] = (
                plot_df["gross_sales"] - plot_df["gross_cogs"] - plot_df["ad_spend"]
            )

        total_gross_sales = float(plot_df["gross_sales"].sum())
        total_gross_cogs = float(plot_df["gross_cogs"].sum())
        total_spend = float(plot_df["ad_spend"].sum())
        total_gross_profit = float(plot_df["gross_profit"].sum())
        total_orders = int(plot_df["attributed_orders"].sum())
        total_contrib_roas = (
            (total_gross_sales - total_gross_cogs) / total_spend if total_spend > 0 else 0
        )

        rev_vals = plot_df["gross_sales"].values.astype(float)
        cogs_vals = plot_df["gross_cogs"].values.astype(float)
        spend_vals = plot_df["ad_spend"].values.astype(float)
        profit_vals = plot_df["gross_profit"].values.astype(float)
        order_vals = plot_df["attributed_orders"].values.astype(float)

        n = len(platforms)
        x = np.arange(n)
        bar_w = 0.18
        offsets = np.array([-1.5, -0.5, 0.5, 1.5], dtype=float) * bar_w

        trend_raw = pd.DataFrame()
        show_roas_trend = False
        if include_roas_trend:
            try:
                end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
                trend_start = end_dt - timedelta(days=roas_trend_days - 1)
                fetch_start = trend_start - timedelta(days=1)
                trend_raw = fetch_channel_performance(
                    fetch_start, end_str, brand_id=brand_id
                )
                if not trend_raw.empty:
                    main_roas = _prepare_daily_roas(
                        trend_raw,
                        min_plot_date=trend_start.strftime("%Y-%m-%d"),
                        metric="contrib_roas",
                    )
                    paid = main_roas[
                        main_roas["platform"].isin(["meta", "google", "amazon"])
                    ]
                    show_roas_trend = (
                        len(paid["report_date"].unique()) >= 2
                        and paid["plot_roas"].notna().any()
                    )
            except Exception as trend_err:
                logger.warning("Could not load ROAS trend data: %s", trend_err)

        sub_df = fetch_top_subchannels(start_str, end_str, brand_id=brand_id, limit=5)
        show_sub = not sub_df.empty

        # Layout: top 2-col (channel | sub-channel), mid All Channels, bottom channel ROAS
        if show_roas_trend and show_sub:
            fig = plt.figure(figsize=(18, 16.5), facecolor="white")
            gs = fig.add_gridspec(
                3,
                2,
                height_ratios=[1.05, 1.15, 1.05],
                width_ratios=[1.55, 1.0],
                hspace=0.48,
                wspace=0.28,
            )
            ax1 = fig.add_subplot(gs[0, 0])
            ax_sub = fig.add_subplot(gs[0, 1])
            ax_roas = fig.add_subplot(gs[1, :])
            ax_channel_roas = fig.add_subplot(gs[2, :])
        elif show_roas_trend:
            fig = plt.figure(figsize=(16, 15.0), facecolor="white")
            gs = fig.add_gridspec(
                3, 1, height_ratios=[1.05, 1.15, 1.05], hspace=0.48
            )
            ax1 = fig.add_subplot(gs[0])
            ax_sub = None
            ax_roas = fig.add_subplot(gs[1])
            ax_channel_roas = fig.add_subplot(gs[2])
        elif show_sub:
            fig = plt.figure(figsize=(18, 7.5), facecolor="white")
            gs = fig.add_gridspec(1, 2, width_ratios=[1.55, 1.0], wspace=0.28)
            ax1 = fig.add_subplot(gs[0, 0])
            ax_sub = fig.add_subplot(gs[0, 1])
            ax_roas = None
            ax_channel_roas = None
        else:
            fig, ax1 = plt.subplots(figsize=(14, 7), facecolor="white")
            ax_sub = None
            ax_roas = None
            ax_channel_roas = None

        ax2 = ax1.twinx()

        rev_colors = [METRIC_COLORS["revenue"]] * n
        cogs_colors = [METRIC_COLORS["cogs"]] * n
        spend_colors = [METRIC_COLORS["ad_spend"]] * n
        profit_colors = [
            METRIC_COLORS["gross_profit"] if val >= 0 else "#C62828"
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

        # Orders on twin axis
        ax2.plot(
            x,
            order_vals,
            color=METRIC_COLORS["orders"],
            marker="o",
            markersize=6.5,
            markeredgecolor="white",
            markeredgewidth=1.1,
            linewidth=LINE_STYLE["secondary"],
            linestyle="-",
            zorder=4,
            label=METRIC_LABELS["orders"],
        )

        ax1.set_xticks(x)
        tick_labels = ax1.set_xticklabels(
            channel_labels, fontsize=TYPE_SCALE["axis"] + 1, fontweight="600"
        )
        for tick, platform in zip(tick_labels, platforms):
            tick.set_color(PLATFORM_COLORS.get(platform, "#222222"))
        ax1.set_ylabel(
            f"Gross Sales / COGS / Spend / Profit ({_currency_symbol()})",
            fontsize=TYPE_SCALE["axis"],
            color="#333333",
            labelpad=8,
        )
        ax2.set_ylabel("Orders", fontsize=TYPE_SCALE["axis"], color="#444444", labelpad=8)
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
        ax1.set_ylim(money_min - y_span * 0.08, money_max * 1.28)
        if money_min < 0:
            ax1.axhline(0, color="#999999", linewidth=0.8, zorder=1)
        ax2.set_ylim(0, order_max * 1.35)

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
                fontsize=TYPE_SCALE["label"],
                fontweight="600",
                color="#1a1a1a",
            )

        ax1.set_xlim(-0.75, n - 0.25)
        ax1.set_title(
            "By Channel (Gross)",
            fontsize=11,
            fontweight="bold",
            color="#1a1a1a",
            pad=8,
            loc="left",
        )

        subtitle_line1 = (
            f"Gross sales: {_format_inr(total_gross_sales)}   ·   "
            f"Gross COGS: {_format_inr(total_gross_cogs)}   ·   "
            f"Ad spend: {_format_inr(total_spend)}"
        )
        subtitle_line2 = (
            f"Gross profit: {_format_inr(total_gross_profit)}   ·   "
            f"Orders: {total_orders:,}   ·   "
            f"Blended ROAS: {total_contrib_roas:.2f}x"
            f"  ·  GP = GS − GC − Spend"
        )

        fig.suptitle(
            title,
            fontsize=TYPE_SCALE["title"] + 2,
            fontweight="bold",
            color="#1a1a1a",
            y=0.985,
        )
        fig.text(
            0.5,
            0.955,
            subtitle_line1,
            ha="center",
            va="top",
            fontsize=TYPE_SCALE["subtitle"],
            color="#555555",
        )
        fig.text(
            0.5,
            0.935,
            subtitle_line2,
            ha="center",
            va="top",
            fontsize=TYPE_SCALE["subtitle"],
            color="#555555",
        )

        from matplotlib.patches import Patch
        from matplotlib.lines import Line2D

        metric_handles = [
            Patch(
                facecolor=METRIC_COLORS["revenue"],
                edgecolor="white",
                label=METRIC_LABELS["revenue"],
            ),
            Patch(
                facecolor=METRIC_COLORS["cogs"],
                edgecolor="white",
                label=METRIC_LABELS["cogs"],
            ),
            Patch(
                facecolor=METRIC_COLORS["ad_spend"],
                edgecolor="white",
                label=METRIC_LABELS["ad_spend"],
            ),
            Patch(
                facecolor=METRIC_COLORS["gross_profit"],
                edgecolor="white",
                label=METRIC_LABELS["gross_profit"],
            ),
            Line2D(
                [0],
                [0],
                color=METRIC_COLORS["orders"],
                marker="o",
                markersize=5,
                linewidth=LINE_STYLE["secondary"],
                label=METRIC_LABELS["orders"],
            ),
        ]
        # Local legend under left chart (avoids colliding with 2-col layout)
        ax1.legend(
            handles=metric_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=5,
            frameon=True,
            fontsize=7.0,
            edgecolor="#DDDDDD",
            facecolor="white",
        )

        if ax_sub is not None:
            day_label = ""
            try:
                if start_str == end_str:
                    day_label = datetime.strptime(end_str, "%Y-%m-%d").strftime("%d %b")
            except ValueError:
                day_label = end_str
            _plot_subchannel_panel(ax_sub, sub_df, period_label=day_label)

        if ax_roas is not None:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
            main_start_str = (
                end_dt - timedelta(days=roas_trend_days - 1)
            ).strftime("%Y-%m-%d")
            _plot_roas_dual_axis_trend(
                ax_roas,
                trend_raw,
                roas_trend_days=roas_trend_days,
                min_plot_date=main_start_str,
            )

        if ax_channel_roas is not None:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
            main_start_str = (
                end_dt - timedelta(days=roas_trend_days - 1)
            ).strftime("%Y-%m-%d")
            _plot_roas_by_day(
                ax_channel_roas,
                trend_raw,
                roas_trend_days=roas_trend_days,
                min_plot_date=main_start_str,
                platforms=["meta", "google", "amazon"],
                title=f"Channel-wise Gross ROAS Trend — Last {roas_trend_days} Days",
                metric="dashboard_gross_roas",
                include_total=False,
                legend_below=True,
            )

        if ax_roas is not None and ax_sub is not None:
            layout_rect = [0.04, 0.04, 0.96, 0.92]
        elif ax_roas is not None:
            layout_rect = [0.04, 0.04, 0.96, 0.92]
        else:
            layout_rect = [0.04, 0.08, 0.96, 0.90]

        plt.tight_layout(rect=layout_rect)
        # Avoid twin-axis / nested legend collisions with tight_layout
        try:
            fig.subplots_adjust(top=layout_rect[3], bottom=layout_rect[1], right=layout_rect[2])
        except Exception:
            pass
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
        roas_trend_days=30,
        amazon_roas_trend_days=30,
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
