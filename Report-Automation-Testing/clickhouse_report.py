"""
ClickHouse-backed funnel + campaign-performance builders for the daily marketing report.

Drop-in replacements for the Postgres functions in dailyrollup.py / excel_generation.py:
  - get_meta_funnel_metrics_ch()    ->  same dict shape as dailyrollup.get_meta_funnel_metrics()
  - get_google_funnel_metrics_ch()  ->  same dict shape as excel_generation.get_google_funnel_metrics()
  - get_campaign_data_ch()          ->  same DataFrame columns as dailyrollup.get_campaign_data()

Sources (all gold, brand-filtered):
  - gold.fct_meta_ads_daily       impressions / clicks / landing_page_views / add_to_cart /
                                  initiate_checkout / spend  (Meta pixel-reported, reconciles to fct_daily_pnl)
  - gold.fct_google_ads_daily     google impressions / clicks
  - gold.fct_order_attribution    attributed orders + revenue by lt_platform / lt_campaign_id
  - gold.fct_order_items.net_cogs COGS per order (the column that ties to fct_daily_pnl.net_cogs)
  - gold.fct_daily_pnl            meta_spend / google_spend (canonical daily ad spend)

Revenue is reported ex-GST (gross_revenue / GST divisor), consistent with channel_performance.py.
"""
from __future__ import annotations

import os
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from amazon_entity_report import get_clickhouse_client
except ImportError:  # pragma: no cover
    get_clickhouse_client = None

GST_DIVISOR = 1.0 + float(os.getenv("GST_RATE", "0.18"))


def _get_brand_id(brand_id: Optional[int]) -> int:
    if brand_id is not None:
        return int(brand_id)
    raw = os.getenv("CLICKHOUSE_BRAND_ID")
    if raw:
        return int(raw)
    try:
        from global_config import get_global_config
        return int(get_global_config("CLICKHOUSE_BRAND_ID", "20"))
    except (ImportError, ValueError, TypeError):
        return 20


def _to_date_str(value) -> str:
    if value is None:
        return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _resolve_range(start_date, end_date) -> tuple[str, str]:
    """Mirror the Postgres helpers: if no dates given, fall back to timeframe_config (else yesterday)."""
    if start_date is None and end_date is None:
        try:
            from timeframe_config import get_timeframe_config
            tf = get_timeframe_config(None, None)
            return (
                tf["start_date"].strftime("%Y-%m-%d"),
                tf["end_date"].strftime("%Y-%m-%d"),
            )
        except Exception:
            y = _to_date_str(None)
            return y, y
    s = _to_date_str(start_date)
    e = _to_date_str(end_date) if end_date is not None else s
    return s, e


def _client():
    if get_clickhouse_client is None:
        raise ImportError("clickhouse-connect is required for ClickHouse report builders.")
    return get_clickhouse_client()


def _rate(numer: float, denom: float) -> float:
    return (numer / denom * 100.0) if denom else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Meta funnel
# ──────────────────────────────────────────────────────────────────────────────
def _empty_meta_funnel() -> dict:
    return {
        'impressions': 0, 'clicks': 0, 'landing_page_views': 0, 'add_to_cart': 0,
        'checkout': 0, 'orders': 0, 'net_profit': 0.0,
        'ctr': 0.0, 'landing_page_rate': 0.0, 'add_to_cart_rate': 0.0,
        'checkout_rate': 0.0, 'conversion_rate': 0.0, 'profit_per_order': 0.0,
        'drop_off_impressions_to_clicks': 0.0, 'drop_off_clicks_to_landing': 0.0,
        'drop_off_landing_to_cart': 0.0, 'drop_off_cart_to_checkout': 0.0,
        'drop_off_checkout_to_orders': 0.0, 'drop_off_cart_to_orders': 0.0,
    }


def get_meta_funnel_metrics_ch(start_date=None, end_date=None, brand_id: Optional[int] = None) -> dict:
    """Meta funnel (Impressions -> Clicks -> LPV -> ATC -> Checkout -> Orders) from ClickHouse gold."""
    bid = _get_brand_id(brand_id)
    s, e = _resolve_range(start_date, end_date)
    client = _client()
    params = {"b": bid, "s": s, "e": e}

    ads = client.query(
        """
        SELECT sum(impressions), sum(clicks), sum(landing_page_views),
               sum(add_to_cart), sum(initiate_checkout)
        FROM gold.fct_meta_ads_daily
        WHERE brand_id = %(b)s AND report_date >= toDate(%(s)s) AND report_date <= toDate(%(e)s)
        """,
        parameters=params,
    ).result_rows[0]
    impressions = float(ads[0] or 0)
    clicks = float(ads[1] or 0)
    landing_page_views = float(ads[2] or 0)
    add_to_cart = float(ads[3] or 0)
    checkout = float(ads[4] or 0)

    orow = client.query(
        """
        SELECT count() AS orders,
               sum(a.gross_revenue) / %(gst)s AS revenue,
               sum(coalesce(ic.net_cogs, 0)) AS cogs
        FROM gold.fct_order_attribution AS a
        LEFT JOIN (
            SELECT order_id, sum(toFloat64(net_cogs)) AS net_cogs
            FROM gold.fct_order_items
            WHERE brand_id = %(b)s AND order_date >= toDate(%(s)s) AND order_date <= toDate(%(e)s)
            GROUP BY order_id
        ) AS ic ON a.order_id = ic.order_id
        WHERE a.brand_id = %(b)s AND a.order_date >= toDate(%(s)s) AND a.order_date <= toDate(%(e)s)
          AND lower(coalesce(a.lt_platform, '')) = 'meta'
        """,
        parameters={**params, "gst": GST_DIVISOR},
    ).result_rows[0]
    orders = float(orow[0] or 0)
    revenue = float(orow[1] or 0)
    cogs = float(orow[2] or 0)

    spend = float(
        client.query(
            """
            SELECT sum(toFloat64(meta_spend)) FROM gold.fct_daily_pnl
            WHERE brand_id = %(b)s AND report_date >= toDate(%(s)s) AND report_date <= toDate(%(e)s)
            """,
            parameters=params,
        ).result_rows[0][0]
        or 0
    )

    net_profit = revenue - cogs - spend
    return {
        'impressions': int(round(impressions)),
        'clicks': int(round(clicks)),
        'landing_page_views': int(round(landing_page_views)),
        'add_to_cart': int(round(add_to_cart)),
        'checkout': int(round(checkout)),
        'orders': int(round(orders)),
        'net_profit': round(net_profit, 2),
        'ctr': round(_rate(clicks, impressions), 2),
        'landing_page_rate': round(_rate(landing_page_views, clicks), 2),
        'add_to_cart_rate': round(_rate(add_to_cart, landing_page_views), 2),
        'checkout_rate': round(_rate(checkout, add_to_cart), 2),
        'conversion_rate': round(_rate(orders, clicks), 2),
        'profit_per_order': round((net_profit / orders) if orders else 0.0, 2),
        'drop_off_impressions_to_clicks': round(_rate(impressions - clicks, impressions), 2),
        'drop_off_clicks_to_landing': round(_rate(clicks - landing_page_views, clicks), 2),
        'drop_off_landing_to_cart': round(_rate(landing_page_views - add_to_cart, landing_page_views), 2),
        'drop_off_cart_to_checkout': round(_rate(add_to_cart - checkout, add_to_cart), 2),
        'drop_off_checkout_to_orders': round(_rate(checkout - orders, checkout), 2),
        'drop_off_cart_to_orders': round(_rate(add_to_cart - orders, add_to_cart), 2),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Google funnel
# ──────────────────────────────────────────────────────────────────────────────
def get_google_funnel_metrics_ch(start_date=None, end_date=None, brand_id: Optional[int] = None) -> dict:
    """Google performance (Clicks, CTR, Orders) from ClickHouse gold.

    interaction_rate is not available in gold.fct_google_ads_daily (it lived only in the
    stale serve layer), so it is returned as 0.0.
    """
    bid = _get_brand_id(brand_id)
    s, e = _resolve_range(start_date, end_date)
    client = _client()
    params = {"b": bid, "s": s, "e": e}

    g = client.query(
        """
        SELECT sum(clicks), sum(impressions)
        FROM gold.fct_google_ads_daily
        WHERE brand_id = %(b)s AND report_date >= toDate(%(s)s) AND report_date <= toDate(%(e)s)
        """,
        parameters=params,
    ).result_rows[0]
    clicks = float(g[0] or 0)
    impressions = float(g[1] or 0)

    orders = int(
        client.query(
            """
            SELECT count() FROM gold.fct_order_attribution
            WHERE brand_id = %(b)s AND order_date >= toDate(%(s)s) AND order_date <= toDate(%(e)s)
              AND lower(coalesce(lt_platform, '')) = 'google'
            """,
            parameters=params,
        ).result_rows[0][0]
        or 0
    )

    return {
        'clicks': int(round(clicks)),
        'ctr': round(_rate(clicks, impressions), 2),
        'interaction_rate': 0.0,
        'orders': orders,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Meta campaign performance
# ──────────────────────────────────────────────────────────────────────────────
def get_campaign_data_ch(start_date=None, end_date=None, brand_id: Optional[int] = None) -> pd.DataFrame:
    """Meta campaign-level performance from ClickHouse gold (one row per campaign).

    Columns match dailyrollup.get_campaign_data() so report_renderer can consume it unchanged.
    """
    bid = _get_brand_id(brand_id)
    s, e = _resolve_range(start_date, end_date)
    client = _client()
    params = {"b": bid, "s": s, "e": e, "gst": GST_DIVISOR}

    sql = """
        WITH ads AS (
            SELECT campaign_id,
                   any(campaign_name) AS campaign_name,
                   sum(impressions) AS impressions,
                   sum(clicks) AS clicks,
                   sum(spend) AS spend,
                   sum(landing_page_views) AS lpv,
                   sum(add_to_cart) AS atc,
                   sum(initiate_checkout) AS ic
            FROM gold.fct_meta_ads_daily
            WHERE brand_id = %(b)s AND report_date >= toDate(%(s)s) AND report_date <= toDate(%(e)s)
            GROUP BY campaign_id
        ),
        ordr AS (
            SELECT a.lt_campaign_id AS campaign_id,
                   count() AS orders,
                   sum(a.gross_revenue) / %(gst)s AS revenue,
                   sum(coalesce(ic.net_cogs, 0)) AS cogs
            FROM gold.fct_order_attribution AS a
            LEFT JOIN (
                SELECT order_id, sum(toFloat64(net_cogs)) AS net_cogs
                FROM gold.fct_order_items
                WHERE brand_id = %(b)s AND order_date >= toDate(%(s)s) AND order_date <= toDate(%(e)s)
                GROUP BY order_id
            ) AS ic ON a.order_id = ic.order_id
            WHERE a.brand_id = %(b)s AND a.order_date >= toDate(%(s)s) AND a.order_date <= toDate(%(e)s)
              AND lower(coalesce(a.lt_platform, '')) = 'meta'
            GROUP BY campaign_id
        )
        SELECT ads.campaign_name AS campaign_name,
               ads.impressions AS impressions,
               ads.clicks AS clicks,
               ads.spend AS spend,
               ads.lpv AS lpv,
               coalesce(o.orders, 0) AS purchases,
               coalesce(o.revenue, 0) AS shopify_revenue,
               coalesce(o.cogs, 0) AS cogs
        FROM ads LEFT JOIN ordr AS o ON ads.campaign_id = o.campaign_id
        WHERE ads.spend > 0 OR coalesce(o.orders, 0) > 0
        ORDER BY ads.spend DESC
    """
    result = client.query(sql, parameters=params)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    if df.empty:
        return df

    for col in ("impressions", "clicks", "spend", "lpv", "purchases", "shopify_revenue", "cogs"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["net_profit"] = df["shopify_revenue"] - df["cogs"] - df["spend"]
    df["gross_roas"] = (df["shopify_revenue"] / df["spend"]).replace([float("inf"), float("-inf")], 0).fillna(0)
    df["net_roas"] = ((df["shopify_revenue"] - df["cogs"]) / df["spend"]).replace([float("inf"), float("-inf")], 0).fillna(0)
    df["be_roas"] = ((df["cogs"] + df["spend"]) / df["spend"]).replace([float("inf"), float("-inf")], 0).fillna(0)
    df["ctr"] = (df["clicks"] / df["impressions"] * 100.0).replace([float("inf"), float("-inf")], 0).fillna(0)
    df["bounce_rate"] = ((df["clicks"] - df["lpv"]) / df["clicks"] * 100.0).replace([float("inf"), float("-inf")], 0).fillna(0).clip(lower=0, upper=100)
    df["conversion_rate"] = (df["purchases"] / df["clicks"] * 100.0).replace([float("inf"), float("-inf")], 0).fillna(0)
    df["cpp"] = (df["spend"] / df["purchases"]).replace([float("inf"), float("-inf")], 0).fillna(0)

    # Aliases / extra columns to match get_campaign_data() output contract
    df["sales"] = df["shopify_revenue"]
    df["roas"] = df["gross_roas"]
    df["breakeven_roas"] = df["be_roas"]
    df["channel"] = "Meta"
    df["date_start"] = e

    cols = [
        "date_start", "channel", "campaign_name",
        "net_profit", "ctr",
        "spend", "sales", "shopify_revenue", "bounce_rate",
        "gross_roas", "net_roas", "be_roas", "conversion_rate",
        "impressions", "cogs", "purchases", "clicks", "roas", "breakeven_roas", "cpp",
    ]
    df = df[[c for c in cols if c in df.columns]].copy()

    numeric = [c for c in df.columns if c not in ("date_start", "channel", "campaign_name")]
    df[numeric] = df[numeric].round(2)
    return df
