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
            # Per-channel placement gross (excl GST) so the PDF SALES BRIDGE
            # (Gross -> -Ret -> -Cnl -> -Disc -> Net) is real instead of degrading
            # to gross==net. The historical route exposes gross_sales_breakdown.
            "gross_sales": chan("gross_sales_breakdown", ch),
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


# Order-date cohort: SAME unit-level formulas as dashboard_master.sql.
# Only the date axis differs — returns/cancels/return-COGS/cancel-COGS land on
# the parent order's order_date (not returned_at / cancelled_at / item created_at).
# Amazon stays on purchase_date (dashboard already uses that).
_ORDER_DATE_COHORT_SQL = """
WITH
orders_dedup AS (
  SELECT
    brand_id,
    order_id,
    argMax(order_date, _loaded_at) AS order_date,
    argMax(order_status, _loaded_at) AS order_status,
    argMax(is_test, _loaded_at) AS is_test,
    argMax(is_revenue_adjustment, _loaded_at) AS is_revenue_adjustment,
    toFloat64(argMax(net_revenue, _loaded_at)) AS net_revenue,
    toFloat64(argMax(net_revenue_excl_tax, _loaded_at)) AS net_revenue_excl_tax,
    toFloat64(argMax(gross_revenue, _loaded_at)) AS gross_revenue,
    toFloat64(argMax(gross_revenue_excl_tax, _loaded_at)) AS gross_revenue_excl_tax,
    toFloat64(argMax(total_discounts, _loaded_at)) AS total_discounts,
    toFloat64(argMax(total_tax, _loaded_at)) AS total_tax
  FROM gold.fct_orders
  WHERE brand_id = {brandId:Int64}
  GROUP BY brand_id, order_id
),
order_day AS (
  SELECT *
  FROM orders_dedup
  WHERE order_date >= toDate({startDate:String})
    AND order_date <= toDate({endDate:String})
    AND coalesce(is_test, 0) = 0
    AND lowerUTF8(trimBoth(coalesce(order_status, ''))) != 'voided'
    AND coalesce(is_revenue_adjustment, 0) = 0
),
attr AS (
  SELECT
    brand_id,
    order_id,
    argMax(lt_platform, _loaded_at) AS lt_platform
  FROM gold.fct_order_attribution
  WHERE brand_id = {brandId:Int64}
  GROUP BY brand_id, order_id
),
order_pnl_global AS (
  SELECT
    oi.brand_id AS brand_id,
    oi.order_id AS order_id,
    maxIf(toUInt8(1), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN') AS has_return,
    maxIf(toUInt8(1), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'CANCELLATION') AS has_cancel
  FROM gold.fct_order_items AS oi
  WHERE oi.brand_id = {brandId:Int64}
    AND coalesce(oi.is_gift_card, 0) = 0
  GROUP BY oi.brand_id, oi.order_id
),
-- Dashboard placement_order_revenue gross/discounts, split by attribution channel
placement_revenue AS (
  SELECT
    od.order_date AS report_date,
    {channelExpr} AS platform,
    count() AS orders,
    sum(
      (
        if(
          toFloat64(coalesce(od.net_revenue, 0)) > 0,
          toFloat64(coalesce(od.gross_revenue, 0))
            - (
              toFloat64(coalesce(od.gross_revenue, 0))
              * (
                toFloat64(coalesce(od.net_revenue, 0))
                - toFloat64(coalesce(od.net_revenue_excl_tax, 0))
              )
              / toFloat64(coalesce(od.net_revenue, 0))
            ),
          toFloat64(coalesce(od.gross_revenue_excl_tax, 0))
        )
      )
      + (
        if(
          toFloat64(coalesce(od.net_revenue, 0)) > 0
            AND toFloat64(coalesce(od.gross_revenue_excl_tax, 0))
              > toFloat64(coalesce(od.net_revenue_excl_tax, 0)),
          toFloat64(coalesce(od.gross_revenue_excl_tax, 0))
            - toFloat64(coalesce(od.net_revenue_excl_tax, 0)),
          if(
            toFloat64(coalesce(od.total_tax, 0)) > 0
              AND toFloat64(coalesce(od.gross_revenue, 0)) > 0,
            toFloat64(coalesce(od.total_discounts, 0))
              * (
                (
                  toFloat64(coalesce(od.gross_revenue, 0))
                  - toFloat64(coalesce(od.total_tax, 0))
                )
                / toFloat64(coalesce(od.gross_revenue, 0))
              ),
            toFloat64(coalesce(od.total_discounts, 0))
          )
        )
      )
    ) AS gross_sales,
    sum(
      if(
        toFloat64(coalesce(od.net_revenue, 0)) > 0
          AND toFloat64(coalesce(od.gross_revenue_excl_tax, 0))
            > toFloat64(coalesce(od.net_revenue_excl_tax, 0)),
        toFloat64(coalesce(od.gross_revenue_excl_tax, 0))
          - toFloat64(coalesce(od.net_revenue_excl_tax, 0)),
        if(
          toFloat64(coalesce(od.total_tax, 0)) > 0
            AND toFloat64(coalesce(od.gross_revenue, 0)) > 0,
          toFloat64(coalesce(od.total_discounts, 0))
            * (
              (
                toFloat64(coalesce(od.gross_revenue, 0))
                - toFloat64(coalesce(od.total_tax, 0))
              )
              / toFloat64(coalesce(od.gross_revenue, 0))
            ),
          toFloat64(coalesce(od.total_discounts, 0))
        )
      )
    ) AS discounts
  FROM order_day AS od
  LEFT JOIN attr AS a
    ON a.brand_id = od.brand_id AND a.order_id = od.order_id
  GROUP BY od.order_date, platform
),
-- Dashboard placement ACTIVE COGS (product+ship+pack+gateway), on order_date
placement_cogs AS (
  SELECT
    od.order_date AS report_date,
    {channelExpr} AS platform,
    sumIf(toFloat64(oi.total_cost), oi.pnl_refund_class = 'ACTIVE') AS product_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_shipping_cost, 0)),
      oi.pnl_refund_class = 'ACTIVE'
    ) AS shipping_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_packaging_cost, 0)),
      oi.pnl_refund_class = 'ACTIVE'
    ) AS packaging_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_gateway_fee, 0)),
      oi.pnl_refund_class = 'ACTIVE'
        AND coalesce(oi.is_cod, 0) = 0
        AND coalesce(oi.is_online_payment, 0) = 1
    ) AS payment_gateway_fees,
    -- GROSS placement COGS: product+ship+pack+gateway for ALL placed lines
    -- (ACTIVE + later-returned + later-cancelled), before returns/cancels netting.
    sum(toFloat64(oi.total_cost)) AS gross_product_cost,
    sum(toFloat64(coalesce(oi.placed_shipping_cost, 0))) AS gross_shipping_cost,
    sum(toFloat64(coalesce(oi.placed_packaging_cost, 0))) AS gross_packaging_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_gateway_fee, 0)),
      coalesce(oi.is_cod, 0) = 0
        AND coalesce(oi.is_online_payment, 0) = 1
    ) AS gross_gateway_fees
  FROM gold.fct_order_items AS oi
  INNER JOIN order_day AS od
    ON od.brand_id = oi.brand_id AND od.order_id = oi.order_id
  LEFT JOIN attr AS a
    ON a.brand_id = oi.brand_id AND a.order_id = oi.order_id
  WHERE oi.brand_id = {brandId:Int64}
    AND coalesce(oi.is_placement_gross_eligible, 0) = 1
    AND coalesce(oi.is_gift_card, 0) = 0
  GROUP BY od.order_date, platform
),
-- Dashboard returned revenue, attributed to parent order_date
returned AS (
  SELECT
    od.order_date AS report_date,
    {channelExpr} AS platform,
    countDistinctIf(oi.order_id, g.has_return = 1) AS returned_orders,
    sumIf(
      if(
        toFloat64(coalesce(oi.returned_revenue_excl_gst, 0)) > 0,
        toFloat64(oi.returned_revenue_excl_gst),
        toFloat64(coalesce(oi.net_pre_refund_excl_gst, 0))
          + toFloat64(coalesce(oi.discount_excl_gst, 0))
      ),
      g.has_return = 1
    ) AS returns_amount
  FROM gold.fct_order_items AS oi
  INNER JOIN order_day AS od
    ON od.brand_id = oi.brand_id AND od.order_id = oi.order_id
  INNER JOIN order_pnl_global AS g
    ON g.brand_id = oi.brand_id AND g.order_id = oi.order_id
  LEFT JOIN attr AS a
    ON a.brand_id = oi.brand_id AND a.order_id = oi.order_id
  WHERE oi.brand_id = {brandId:Int64}
    AND coalesce(oi.is_gift_card, 0) = 0
    AND g.has_return = 1
    AND coalesce(
      oi.returned_at,
      if(
        oi.order_status IN ('refunded', 'partially_refunded')
          AND oi.return_status = 'NO_RETURN'
          AND oi.refunded_quantity > 0,
        oi.refunded_at,
        NULL
      )
    ) IS NOT NULL
  GROUP BY od.order_date, platform
),
-- Dashboard cancelled revenue, attributed to parent order_date
cancelled AS (
  SELECT
    od.order_date AS report_date,
    {channelExpr} AS platform,
    countDistinctIf(oi.order_id, g.has_return = 0 AND g.has_cancel = 1) AS cancelled_orders,
    sumIf(
      if(
        toFloat64(coalesce(oi.cancelled_revenue_excl_gst, 0)) > 0,
        toFloat64(oi.cancelled_revenue_excl_gst),
        toFloat64(coalesce(oi.net_pre_refund_excl_gst, 0))
          + toFloat64(coalesce(oi.discount_excl_gst, 0))
      ),
      g.has_return = 0 AND g.has_cancel = 1
    ) AS cancels_amount
  FROM gold.fct_order_items AS oi
  INNER JOIN order_day AS od
    ON od.brand_id = oi.brand_id AND od.order_id = oi.order_id
  INNER JOIN order_pnl_global AS g
    ON g.brand_id = oi.brand_id AND g.order_id = oi.order_id
  LEFT JOIN attr AS a
    ON a.brand_id = oi.brand_id AND a.order_id = oi.order_id
  WHERE oi.brand_id = {brandId:Int64}
    AND oi.is_cancelled_line = 1
    AND coalesce(oi.is_gift_card, 0) = 0
    AND coalesce(
      oi.cancelled_at,
      if(oi.order_status = 'voided', oi.voided_at, NULL)
    ) IS NOT NULL
  GROUP BY od.order_date, platform
),
-- Dashboard return residual COGS, on order_date
return_cogs AS (
  SELECT
    od.order_date AS report_date,
    {channelExpr} AS platform,
    sumIf(
      toFloat64(coalesce(oi.rto_cost, 0)),
      upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN'
        AND (
          coalesce(oi.is_return_line, 0) = 1
          OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS')
        )
    ) AS return_rto_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_shipping_cost, 0)),
      upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN'
        AND (
          coalesce(oi.is_return_line, 0) = 1
          OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS')
        )
    ) AS return_shipping_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_packaging_cost, 0)),
      upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN'
        AND (
          coalesce(oi.is_return_line, 0) = 1
          OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS')
        )
    ) AS return_packaging_cost,
    sumIf(
      toFloat64(coalesce(oi.placed_gateway_fee, 0)),
      upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN'
        AND (
          coalesce(oi.is_return_line, 0) = 1
          OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS')
        )
        AND coalesce(oi.is_cod, 0) = 0
        AND coalesce(oi.is_online_payment, 0) = 1
    ) AS return_gateway_fees
  FROM gold.fct_order_items AS oi
  INNER JOIN order_day AS od
    ON od.brand_id = oi.brand_id AND od.order_id = oi.order_id
  LEFT JOIN attr AS a
    ON a.brand_id = oi.brand_id AND a.order_id = oi.order_id
  WHERE oi.brand_id = {brandId:Int64}
    AND upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN'
    AND coalesce(oi.is_gift_card, 0) = 0
    AND (
      coalesce(oi.is_return_line, 0) = 1
      OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS')
    )
    AND coalesce(
      oi.returned_at,
      if(
        oi.order_status IN ('refunded', 'partially_refunded')
          AND oi.return_status = 'NO_RETURN'
          AND oi.refunded_quantity > 0,
        oi.refunded_at,
        NULL
      )
    ) IS NOT NULL
  GROUP BY od.order_date, platform
),
-- Dashboard cancel gateway fees, on order_date
cancel_cogs AS (
  SELECT
    od.order_date AS report_date,
    {channelExpr} AS platform,
    sumIf(
      toFloat64(coalesce(oi.placed_gateway_fee, 0)),
      upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'CANCELLATION'
        AND coalesce(oi.is_cancelled_line, 0) = 1
        AND coalesce(oi.is_cod, 0) = 0
        AND coalesce(oi.is_online_payment, 0) = 1
    ) AS cancel_gateway_fees
  FROM gold.fct_order_items AS oi
  INNER JOIN order_day AS od
    ON od.brand_id = oi.brand_id AND od.order_id = oi.order_id
  LEFT JOIN attr AS a
    ON a.brand_id = oi.brand_id AND a.order_id = oi.order_id
  WHERE oi.brand_id = {brandId:Int64}
    AND upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'CANCELLATION'
    AND coalesce(oi.is_gift_card, 0) = 0
    AND coalesce(oi.is_cancelled_line, 0) = 1
    AND coalesce(
      oi.cancelled_at,
      if(oi.order_status = 'voided', oi.voided_at, NULL)
    ) IS NOT NULL
  GROUP BY od.order_date, platform
),
shopify_keys AS (
  SELECT report_date, platform FROM placement_revenue
  UNION DISTINCT
  SELECT report_date, platform FROM placement_cogs
  UNION DISTINCT
  SELECT report_date, platform FROM returned
  UNION DISTINCT
  SELECT report_date, platform FROM cancelled
  UNION DISTINCT
  SELECT report_date, platform FROM return_cogs
  UNION DISTINCT
  SELECT report_date, platform FROM cancel_cogs
),
shopify_cohort AS (
  SELECT
    k.report_date AS report_date,
    k.platform AS platform,
    coalesce(pr.orders, 0) AS orders,
    coalesce(pr.gross_sales, 0) AS gross_sales,
    coalesce(pr.discounts, 0) AS discounts,
    coalesce(re.returns_amount, 0) AS returns_amount,
    coalesce(ca.cancels_amount, 0) AS cancels_amount,
    coalesce(re.returned_orders, 0) AS returned_orders,
    coalesce(ca.cancelled_orders, 0) AS cancelled_orders,
    coalesce(pl.product_cost, 0)
      + coalesce(pl.shipping_cost, 0)
      + coalesce(pl.packaging_cost, 0)
      + coalesce(pl.payment_gateway_fees, 0) AS active_cogs,
    -- Gross COGS = placement COGS of ALL placed lines (pre-netting).
    coalesce(pl.gross_product_cost, 0)
      + coalesce(pl.gross_shipping_cost, 0)
      + coalesce(pl.gross_packaging_cost, 0)
      + coalesce(pl.gross_gateway_fees, 0) AS gross_cogs,
    (
      coalesce(rc.return_rto_cost, 0)
      + coalesce(rc.return_shipping_cost, 0)
      + coalesce(rc.return_packaging_cost, 0)
      + coalesce(rc.return_gateway_fees, 0)
    ) AS return_cogs,
    coalesce(cc.cancel_gateway_fees, 0) AS cancel_cogs,
    (
      coalesce(pl.product_cost, 0)
      + coalesce(pl.shipping_cost, 0)
      + coalesce(pl.packaging_cost, 0)
      + coalesce(pl.payment_gateway_fees, 0)
      + coalesce(cc.cancel_gateway_fees, 0)
      + coalesce(rc.return_rto_cost, 0)
      + coalesce(rc.return_shipping_cost, 0)
      + coalesce(rc.return_packaging_cost, 0)
      + coalesce(rc.return_gateway_fees, 0)
    ) AS net_cogs
  FROM shopify_keys AS k
  LEFT JOIN placement_revenue AS pr
    ON pr.report_date = k.report_date AND pr.platform = k.platform
  LEFT JOIN placement_cogs AS pl
    ON pl.report_date = k.report_date AND pl.platform = k.platform
  LEFT JOIN returned AS re
    ON re.report_date = k.report_date AND re.platform = k.platform
  LEFT JOIN cancelled AS ca
    ON ca.report_date = k.report_date AND ca.platform = k.platform
  LEFT JOIN return_cogs AS rc
    ON rc.report_date = k.report_date AND rc.platform = k.platform
  LEFT JOIN cancel_cogs AS cc
    ON cc.report_date = k.report_date AND cc.platform = k.platform
),
-- Dashboard amazon_daily (purchase_date axis — unchanged from master)
amz_items AS (
  SELECT
    oi.brand_id,
    toDate(oi.purchase_date) AS report_date,
    oi.amazon_order_id,
    oi.order_item_id,
    oi.pnl_refund_status,
    toFloat64(oi.item_price_amount) * toFloat64(oi.quantity_ordered) AS item_gross,
    toFloat64(coalesce(oi.total_cogs, 0)) AS product_cost
  FROM gold.fct_amazon_order_items AS oi
  WHERE oi.brand_id = {brandId:Int64}
    AND oi.purchase_date IS NOT NULL
    AND toDate(oi.purchase_date) >= toDate({startDate:String})
    AND toDate(oi.purchase_date) <= toDate({endDate:String})
),
amz_order_gross AS (
  SELECT brand_id, amazon_order_id, sum(item_gross) AS order_gross_total
  FROM amz_items
  GROUP BY brand_id, amazon_order_id
),
amz_pnl AS (
  SELECT
    brand_id,
    amazon_order_id,
    argMax(payout_basis, _gold_created_at) AS payout_basis,
    toFloat64(argMax(effective_gross_revenue, _gold_created_at)) AS gross_revenue,
    toFloat64(argMax(effective_refunds, _gold_created_at)) AS refund_amount,
    toFloat64(argMax(effective_commission, _gold_created_at)) AS commission,
    toFloat64(argMax(effective_closing, _gold_created_at)) AS closing,
    toFloat64(argMax(effective_shipping, _gold_created_at)) AS shipping,
    toFloat64(argMax(effective_tax_withheld, _gold_created_at)) AS tax_withheld,
    toFloat64(argMax(effective_other_service_fees, _gold_created_at)) AS other_fees
  FROM gold.fct_amazon_sp_order_pnl
  WHERE brand_id = {brandId:Int64}
    AND purchase_date IS NOT NULL
    AND toDate(purchase_date) >= toDate({startDate:String})
    AND toDate(purchase_date) <= toDate({endDate:String})
  GROUP BY brand_id, amazon_order_id
),
amz_item_pnl AS (
  SELECT
    ai.brand_id AS brand_id,
    ai.report_date AS report_date,
    ai.amazon_order_id AS amazon_order_id,
    ai.pnl_refund_status AS pnl_refund_status,
    ai.product_cost AS product_cost,
    coalesce(ap.payout_basis, 'NONE') AS payout_basis,
    -- ClickHouse LEFT JOIN fills missing String keys as '' (not NULL). Treat
    -- empty(ap.amazon_order_id) as "no PnL yet" and fall back to item_gross —
    -- same as the dashboard amazonHistoricalHelpers path. Otherwise same-day
    -- Amazon orders post COGS with ₹0 revenue → fake 0.00 / negative ROAS cliffs.
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.gross_revenue, ai.item_gross) * (ai.item_gross / og.order_gross_total),
        ai.item_gross
      )
    ) AS item_revenue,
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.refund_amount, 0) * (ai.item_gross / og.order_gross_total),
        toFloat64(0)
      )
    ) AS item_refunds,
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.commission, 0) * (ai.item_gross / og.order_gross_total),
        toFloat64(0)
      )
    ) AS item_commission,
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.closing, 0) * (ai.item_gross / og.order_gross_total),
        toFloat64(0)
      )
    ) AS item_closing,
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.shipping, 0) * (ai.item_gross / og.order_gross_total),
        toFloat64(0)
      )
    ) AS item_shipping,
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.tax_withheld, 0) * (ai.item_gross / og.order_gross_total),
        toFloat64(0)
      )
    ) AS item_tax_withheld,
    if(
      coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION',
      toFloat64(0),
      if(
        NOT empty(ap.amazon_order_id) AND coalesce(og.order_gross_total, 0) > 0,
        coalesce(ap.other_fees, 0) * (ai.item_gross / og.order_gross_total),
        toFloat64(0)
      )
    ) AS item_other_fees
  FROM amz_items AS ai
  LEFT JOIN amz_order_gross AS og
    ON og.brand_id = ai.brand_id AND og.amazon_order_id = ai.amazon_order_id
  LEFT JOIN amz_pnl AS ap
    ON ap.brand_id = ai.brand_id AND ap.amazon_order_id = ai.amazon_order_id
),
amazon_mkt AS (
  SELECT
    report_date,
    'amazon' AS platform,
    toUInt64(countDistinctIf(amazon_order_id, pnl_refund_status != 'CANCELLATION')) AS orders,
    sum(item_revenue + item_tax_withheld) AS gross_sales,
    toFloat64(0) AS discounts,
    greatest(sum(item_revenue + item_tax_withheld) - sum(item_revenue + item_refunds + item_tax_withheld), 0) AS returns_amount,
    toFloat64(0) AS cancels_amount,
    toUInt64(countDistinctIf(amazon_order_id, pnl_refund_status = 'RETURN')) AS returned_orders,
    toUInt64(countDistinctIf(amazon_order_id, pnl_refund_status = 'CANCELLATION')) AS cancelled_orders,
    sum(product_cost) AS active_cogs,
    -- Amazon has no gross/net COGS split (matches the main dashboard): marketplace
    -- fees are STANDING cost on every order, not a returns/cancels effect, so they
    -- belong in Gross COGS — not the reconciliation row. gross_cogs == net_cogs here,
    -- so Amazon never distorts the "Returns / Cancels / Disc" bridge line.
    -- NOTE: column order MUST match shopify_cohort for the UNION ALL below
    -- (active_cogs, gross_cogs, return_cogs, cancel_cogs, net_cogs).
    sum(
      CASE
        WHEN payout_basis = 'NONE' AND pnl_refund_status = 'CANCELLATION' THEN toFloat64(0)
        WHEN payout_basis = 'NONE' THEN product_cost
        WHEN pnl_refund_status = 'RETURN' THEN
          item_revenue + item_refunds + item_commission + item_closing
          + item_shipping + item_tax_withheld + item_other_fees
        ELSE
          product_cost + abs(item_commission) + abs(item_closing)
          + abs(item_shipping) + abs(item_tax_withheld) + abs(item_other_fees)
      END
    ) AS gross_cogs,
    toFloat64(0) AS return_cogs,
    toFloat64(0) AS cancel_cogs,
    sum(
      CASE
        WHEN payout_basis = 'NONE' AND pnl_refund_status = 'CANCELLATION' THEN toFloat64(0)
        WHEN payout_basis = 'NONE' THEN product_cost
        WHEN pnl_refund_status = 'RETURN' THEN
          item_revenue + item_refunds + item_commission + item_closing
          + item_shipping + item_tax_withheld + item_other_fees
        ELSE
          product_cost + abs(item_commission) + abs(item_closing)
          + abs(item_shipping) + abs(item_tax_withheld) + abs(item_other_fees)
      END
    ) AS net_cogs
  FROM amz_item_pnl
  GROUP BY report_date
),
meta_spend AS (
  SELECT report_date, sum(toFloat64(spend)) AS spend
  FROM gold.fct_meta_ads_daily
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:String})
    AND report_date <= toDate({endDate:String})
  GROUP BY report_date
),
google_spend AS (
  SELECT report_date, sum(toFloat64(spend)) AS spend
  FROM gold.fct_google_ads_daily
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:String})
    AND report_date <= toDate({endDate:String})
  GROUP BY report_date
),
amazon_spend AS (
  SELECT report_date, sum(toFloat64(cost)) AS spend
  FROM gold.fct_amazon_ads_campaigns_daily
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:String})
    AND report_date <= toDate({endDate:String})
  GROUP BY report_date
),
unioned AS (
  SELECT * FROM shopify_cohort
  UNION ALL
  SELECT * FROM amazon_mkt
)
SELECT
  u.report_date AS report_date,
  u.platform,
  u.orders,
  round(u.gross_sales, 2) AS gross_sales,
  round(u.discounts, 2) AS discounts,
  round(u.returns_amount, 2) AS returns_amount,
  round(u.cancels_amount, 2) AS cancels_amount,
  u.returned_orders,
  u.cancelled_orders,
  -- Dashboard: net_sales = gross - returns - cancels - discounts (+ amazon already net)
  round(
    if(
      u.platform = 'amazon',
      u.gross_sales - u.returns_amount,
      u.gross_sales - u.returns_amount - u.cancels_amount - u.discounts
    ),
    2
  ) AS net_sales,
  round(u.net_cogs, 2) AS net_cogs,
  round(u.active_cogs, 2) AS active_cogs,
  round(u.gross_cogs, 2) AS gross_cogs,
  round(u.return_cogs, 2) AS return_cogs,
  round(u.cancel_cogs, 2) AS cancel_cogs,
  round(
    multiIf(
      u.platform = 'meta', coalesce(ms.spend, 0),
      u.platform = 'google', coalesce(gs.spend, 0),
      u.platform = 'amazon', coalesce(az.spend, 0),
      0
    ),
    2
  ) AS ad_spend,
  round(
    if(
      u.platform = 'amazon',
      u.gross_sales - u.returns_amount,
      u.gross_sales - u.returns_amount - u.cancels_amount - u.discounts
    )
    - u.net_cogs
    - multiIf(
        u.platform = 'meta', coalesce(ms.spend, 0),
        u.platform = 'google', coalesce(gs.spend, 0),
        u.platform = 'amazon', coalesce(az.spend, 0),
        0
      ),
    2
  ) AS net_profit
FROM unioned AS u
LEFT JOIN meta_spend AS ms ON ms.report_date = u.report_date
LEFT JOIN google_spend AS gs ON gs.report_date = u.report_date
LEFT JOIN amazon_spend AS az ON az.report_date = u.report_date
ORDER BY u.report_date, u.platform
"""


_PAID_SPEND_BY_DAY_SQL = """
SELECT report_date, platform, round(spend, 2) AS spend
FROM (
  SELECT report_date, 'meta' AS platform, sum(toFloat64(spend)) AS spend
  FROM gold.fct_meta_ads_daily
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:String})
    AND report_date <= toDate({endDate:String})
  GROUP BY report_date
  UNION ALL
  SELECT report_date, 'google' AS platform, sum(toFloat64(spend)) AS spend
  FROM gold.fct_google_ads_daily
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:String})
    AND report_date <= toDate({endDate:String})
  GROUP BY report_date
  UNION ALL
  SELECT report_date, 'amazon' AS platform, sum(toFloat64(cost)) AS spend
  FROM gold.fct_amazon_ads_campaigns_daily
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:String})
    AND report_date <= toDate({endDate:String})
  GROUP BY report_date
)
WHERE spend > 0
ORDER BY report_date, platform
"""


def _densify_order_date_cohort_with_spend(
    df,
    brand_id: int,
    start: str,
    end: str,
):
    """
    Attach calendar-day ad spend even when a paid platform has 0 attributed
    orders that day (otherwise spend/ROAS charts silently drop those days).
    """
    import pandas as pd
    from amazon_entity_report import get_clickhouse_client

    empty_cols = [
        "report_date",
        "platform",
        "orders",
        "gross_sales",
        "returns_amount",
        "cancels_amount",
        "returned_orders",
        "cancelled_orders",
        "net_sales",
        "net_cogs",
        "ad_spend",
        "net_profit",
    ]
    if df is None:
        df = pd.DataFrame(columns=empty_cols)

    client = get_clickhouse_client()
    spend_res = client.query(
        _PAID_SPEND_BY_DAY_SQL,
        parameters={"brandId": brand_id, "startDate": start, "endDate": end},
    )
    spend_df = pd.DataFrame(spend_res.result_rows, columns=spend_res.column_names)
    if spend_df.empty:
        return df

    spend_df["report_date"] = pd.to_datetime(spend_df["report_date"])
    spend_df["spend"] = pd.to_numeric(spend_df["spend"], errors="coerce").fillna(0)

    if df.empty:
        out = pd.DataFrame(
            {
                "report_date": spend_df["report_date"],
                "platform": spend_df["platform"],
                "orders": 0,
                "gross_sales": 0.0,
                "discounts": 0.0,
                "returns_amount": 0.0,
                "cancels_amount": 0.0,
                "returned_orders": 0,
                "cancelled_orders": 0,
                "net_sales": 0.0,
                "net_cogs": 0.0,
                "active_cogs": 0.0,
                "gross_cogs": 0.0,
                "return_cogs": 0.0,
                "cancel_cogs": 0.0,
                "ad_spend": spend_df["spend"],
                "net_profit": -spend_df["spend"],
            }
        )
        return out.sort_values(["report_date", "platform"]).reset_index(drop=True)

    key = ["report_date", "platform"]
    existing = set(
        zip(
            pd.to_datetime(df["report_date"]).dt.normalize(),
            df["platform"].astype(str),
        )
    )
    extras = []
    for _, row in spend_df.iterrows():
        k = (pd.Timestamp(row["report_date"]).normalize(), str(row["platform"]))
        if k in existing:
            continue
        spend = float(row["spend"])
        extras.append(
            {
                "report_date": row["report_date"],
                "platform": row["platform"],
                "orders": 0,
                "gross_sales": 0.0,
                "discounts": 0.0,
                "returns_amount": 0.0,
                "cancels_amount": 0.0,
                "returned_orders": 0,
                "cancelled_orders": 0,
                "net_sales": 0.0,
                "net_cogs": 0.0,
                "active_cogs": 0.0,
                "gross_cogs": 0.0,
                "return_cogs": 0.0,
                "cancel_cogs": 0.0,
                "ad_spend": spend,
                "net_profit": -spend,
            }
        )
    if extras:
        df = pd.concat([df, pd.DataFrame(extras)], ignore_index=True)
    return df.sort_values(["report_date", "platform"]).reset_index(drop=True)


def fetch_order_date_cohort_rows(
    brand_id: int,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
):
    """
    True order-date cohort rows by placement channel.

    For orders placed on day D: gross sales, discounts, returns/cancels of *those*
    orders (regardless of when the return/cancel event happened), and net COGS of
    those orders on the same order-date axis — using the SAME unit formulas as
    dashboard_master.sql (gross/discount tax logic, return/cancel revenue, ACTIVE
    product+ship+pack+gateway, return RTO/fees, cancel gateway, Amazon item P&L).

    Only the date axis differs from the Historical dashboard (event dates).
    Amazon marketplace stays on purchase_date (dashboard already does).

    That day's ad spend is attached to the matching paid channel. Paid platforms
    with spend but no attributed orders that day are densified so spend / ROAS
    charts stay complete.
    """
    import pandas as pd
    from amazon_entity_report import get_clickhouse_client

    start, end = _to_date_str(start_date), _to_date_str(end_date)
    sql = _ORDER_DATE_COHORT_SQL.replace("{channelExpr}", _CHANNEL_MAP)
    client = get_clickhouse_client()
    res = client.query(
        sql,
        parameters={"brandId": brand_id, "startDate": start, "endDate": end},
    )
    df = pd.DataFrame(res.result_rows, columns=res.column_names)
    numeric_cols = (
        "orders",
        "gross_sales",
        "discounts",
        "returns_amount",
        "cancels_amount",
        "returned_orders",
        "cancelled_orders",
        "net_sales",
        "net_cogs",
        "active_cogs",
        "gross_cogs",
        "return_cogs",
        "cancel_cogs",
        "ad_spend",
        "net_profit",
    )
    if df.empty:
        df = pd.DataFrame(
            columns=["report_date", "platform", *numeric_cols]
        )
    else:
        df["report_date"] = pd.to_datetime(df["report_date"])
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return _densify_order_date_cohort_with_spend(df, brand_id, start, end)


def fetch_order_date_cohort_pnl_series(
    brand_id: int,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
):
    """Daily all-placement order-date cohort P&L for the NP chart."""
    import pandas as pd

    rows = fetch_order_date_cohort_rows(brand_id, start_date, end_date)
    if rows.empty:
        return pd.DataFrame(
            columns=[
                "sale_date",
                "orders",
                "gross_sales",
                "returns_amount",
                "cancels_amount",
                "revenue",
                "cogs",
                "gross_cogs",
                "total_ad_spend",
                "net_profit",
            ]
        )
    g = (
        rows.groupby("report_date", as_index=False)
        .agg(
            {
                "orders": "sum",
                "gross_sales": "sum",
                "returns_amount": "sum",
                "cancels_amount": "sum",
                "net_sales": "sum",
                "net_cogs": "sum",
                "gross_cogs": "sum",
                "ad_spend": "sum",
                "net_profit": "sum",
            }
        )
        .sort_values("report_date")
    )
    return pd.DataFrame(
        {
            "sale_date": pd.to_datetime(g["report_date"]),
            "orders": g["orders"],
            "gross_sales": g["gross_sales"],
            "returns_amount": g["returns_amount"],
            "cancels_amount": g["cancels_amount"],
            "revenue": g["net_sales"],
            "cogs": g["net_cogs"],
            "gross_cogs": g["gross_cogs"],
            "total_ad_spend": g["ad_spend"],
            "net_profit": g["net_profit"],
        }
    ).reset_index(drop=True)


def fetch_gross_aov(
    brand_id: int,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
) -> float:
    """Average actual order value (incl GST) over the order-date cohort.

    Uses the SAME order set as the Orders KPI (gold.fct_orders, brand-filtered,
    order_date axis, excluding test / voided / revenue-adjustment orders) so AOV
    reconciles with Orders and Gross Revenue. Numerator is gross_revenue (incl
    GST) — the actual charged order value, not the GST-excluded gross.
    """
    from amazon_entity_report import get_clickhouse_client

    start, end = _to_date_str(start_date), _to_date_str(end_date)
    sql = """
    WITH orders_dedup AS (
      SELECT
        order_id,
        argMax(order_date, _loaded_at) AS order_date,
        argMax(order_status, _loaded_at) AS order_status,
        argMax(is_test, _loaded_at) AS is_test,
        argMax(is_revenue_adjustment, _loaded_at) AS is_revenue_adjustment,
        toFloat64(argMax(gross_revenue, _loaded_at)) AS gross_revenue
      FROM gold.fct_orders
      WHERE brand_id = {brandId:Int64}
      GROUP BY order_id
    )
    SELECT count() AS orders, sum(gross_revenue) AS gross_value
    FROM orders_dedup
    WHERE order_date >= toDate({startDate:String})
      AND order_date <= toDate({endDate:String})
      AND coalesce(is_test, 0) = 0
      AND lowerUTF8(trimBoth(coalesce(order_status, ''))) != 'voided'
      AND coalesce(is_revenue_adjustment, 0) = 0
    """
    try:
        client = get_clickhouse_client()
        res = client.query(
            sql, parameters={"brandId": brand_id, "startDate": start, "endDate": end}
        )
        if not res.result_rows:
            return 0.0
        orders, gross_value = res.result_rows[0]
        orders = float(orders or 0)
        return round(float(gross_value or 0) / orders, 2) if orders else 0.0
    except Exception as exc:  # noqa: BLE001 - AOV is non-fatal decoration
        logger.warning("fetch_gross_aov failed: %s", exc)
        return 0.0


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

    # --- Amazon net-sales reconciliation override ---------------------------------
    # dashboard_master.sql derives Amazon net revenue by prorating the ORDER-level
    # effective_gross_revenue across items, which overstates net sales vs the
    # dashboard (e.g. 07-26: 50,862 vs 43,323). The backend/dashboard source Amazon
    # net sales at the ITEM level (cancelled orders zeroed, joined on order
    # purchase_date). Reuse the already-reconciled item fetcher and fold the delta
    # back through net_sales / gross_sales / net_profit. COGS is left untouched
    # because the master query's Amazon net COGS already matches the dashboard.
    try:
        from amazon_entity_report import fetch_amazon_sp_items_gold
        items_df = fetch_amazon_sp_items_gold(start, end, brand_id=brand_id)
        if not items_df.empty and "item_price_amount" in items_df.columns:
            amz_item_rev = float(
                pd.to_numeric(items_df["item_price_amount"], errors="coerce").fillna(0).sum()
            )
            delta = totals["amazon_net_revenue"] - amz_item_rev
            if abs(delta) > 0.01:
                totals["amazon_net_revenue"] = round(amz_item_rev, 2)
                totals["net_sales"] = round(totals["net_sales"] - delta, 2)
                totals["gross_sales"] = round(totals["gross_sales"] - delta, 2)
                totals["net_profit"] = round(
                    totals["net_sales"] - totals["total_cogs"] - totals["total_ad_spend"], 2
                )
                logger.info("[dashboard] Amazon item-level net-sales override: delta=%.2f", delta)
    except Exception as ex:
        logger.warning("[dashboard] Amazon item-level override failed (%s); keeping master value", ex)

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


# ------------------------------------------------------------ real-time (live) source

def _realtime_stats(brand_id: int, company_id: int, start: str, end: str) -> dict:
    """
    General Statistics computed from the REAL-TIME "Reports Analytics" APIs, which query
    the source platforms directly (Shopify Admin / Meta Graph / Google Ads) and therefore
    survive a warehouse/ClickHouse outage. Combines:
        * /shopify/analytics/live-dashboard-bundle  (Shopify revenue + live Meta/Google
          spend + Amazon SP sales + returns/cancels)
        * /shopify/analytics/cogs                   (live Shopify COGS + per-channel split)

    Totals (net sales, gross sales, COGS, ad spend, orders, net profit, returns/cancels)
    are fully real-time. Per-channel net sales / order counts are NOT exposed by the live
    platform APIs (they need warehouse order-attribution), so channel rows carry real-time
    ad spend + COGS only; the PDF residual row reconciles the channel sum back to totals.
    Raises on missing data so the caller can fall through to the next source.
    """
    from api_data_fetcher import (
        fetch_live_dashboard_bundle_cached,
        fetch_shopify_cogs_cached,
    )

    def _f(x) -> float:
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    bundle = fetch_live_dashboard_bundle_cached(start, end)
    cur = (bundle or {}).get("current") or {}
    dash = cur.get("dashboard") or {}
    if not dash:
        raise RuntimeError("live-dashboard-bundle returned no dashboard data")

    sm = ((dash.get("revenue") or {}).get("sales_metrics")) or {}
    stat = dash.get("statistics") or {}
    amazon = cur.get("amazonSales") or {}
    adspend = cur.get("adSpend") or {}
    ad_bd = adspend.get("breakdown") or {}
    rc = (bundle.get("returnsCancels") or {}).get("current") or {}

    cogs_data = fetch_shopify_cogs_cached(start, end) or {}
    shop_cogs = _f(cogs_data.get("cost_of_goods_sold"))
    cogs_bd = cogs_data.get("cogs_breakdown") or {}

    shop_net, shop_gross = _f(sm.get("net_sales")), _f(sm.get("gross_sales"))
    shop_orders = int(_f(stat.get("total_orders")))
    amz_net, amz_gross = _f(amazon.get("net_sales")), _f(amazon.get("gross_sales"))
    amz_cogs, amz_orders = _f(amazon.get("cogs")), int(_f(amazon.get("orders")))

    def _amz_spend(v):
        return _f(v.get("total")) if isinstance(v, dict) else _f(v)

    meta_spend, google_spend = _f(ad_bd.get("meta")), _f(ad_bd.get("google"))
    amz_spend = _amz_spend(ad_bd.get("amazon"))
    total_ad = _f(adspend.get("total")) or (meta_spend + google_spend + amz_spend)

    net_sales = round(shop_net + amz_net, 2)
    gross_sales = round(shop_gross + amz_gross, 2)
    total_cogs = round(shop_cogs + amz_cogs, 2)
    total_orders = shop_orders + amz_orders

    totals = {
        "net_sales": net_sales,
        "gross_sales": gross_sales,
        "total_cogs": total_cogs,
        "total_ad_spend": round(total_ad, 2),
        "total_orders": total_orders,
        "net_profit": round(net_sales - total_cogs - total_ad, 2),
        "returns_cancels": int(_f(rc.get("total_count"))),
        "cancelled_orders": int(_f(rc.get("cancelled_count"))),
        "returned_orders": int(_f(rc.get("returned_count"))),
        "cancelled_amount": _f(rc.get("cancelled_amount")),
        "returned_amount": _f(rc.get("returned_amount")),
        "returns_cancels_amount": _f(rc.get("total_amount")),
        "amazon_net_revenue": round(amz_net, 2),
        "amazon_net_cogs": round(amz_cogs, 2),
        "amazon_spend": round(amz_spend, 2),
        "amazon_orders": amz_orders,
    }

    # Per-channel: real-time ad spend + COGS; net sales / order counts unavailable live.
    channels = {}
    for ch, spend in (("meta", meta_spend), ("google", google_spend), ("organic", 0.0)):
        channels[ch] = {
            "sales": 0.0,
            "cogs": round(_f(cogs_bd.get(ch)), 2),
            "ad_spend": round(spend, 2) if ch != "organic" else 0.0,
            "order_count": 0,
        }
    return {"totals": totals, "channels": channels, "source": "realtime"}


# --------------------------------------------------------------------------- API

def _source_order() -> list[str]:
    """
    Ordered list of data sources to try for the General Statistics payload.

    Override with env DASHBOARD_SOURCE_ORDER (comma-separated, e.g. "clickhouse,realtime").
    Sources: 'api' = historical/dashboard (warehouse-backed), 'clickhouse' = direct gold
    query (warehouse), 'realtime' = live source-platform APIs (survive a warehouse outage).

    Default order always ends with 'realtime' so net profit + totals still render when the
    warehouse ('api' and 'clickhouse') is unavailable — the point of this fallback.
    """
    raw = os.getenv("DASHBOARD_SOURCE_ORDER", "").strip()
    if raw:
        return [s.strip().lower() for s in raw.split(",") if s.strip()]
    api_only = os.getenv("USE_API_ONLY", "false").lower() in ("1", "true", "yes")
    api_fallback = os.getenv("USE_API_FALLBACK", "true").lower() in ("1", "true", "yes")
    if api_only:
        return ["api", "realtime"]
    if api_fallback:
        return ["api", "clickhouse", "realtime"]
    return ["clickhouse", "realtime"]


def _run_source(name: str, brand_id: int, company_id: int, start: str, end: str) -> Optional[dict]:
    if name == "api":
        data = _fetch_from_api(brand_id, company_id, start, end)
        return _api_to_stats(data) if data is not None else None
    if name == "clickhouse":
        return _clickhouse_stats(brand_id, start, end)
    if name in ("realtime", "live"):
        return _realtime_stats(brand_id, company_id, start, end)
    raise ValueError(f"unknown dashboard source: {name}")


def fetch_general_statistics(
    brand_id: int,
    company_id: int,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    prefer_api: bool = True,  # retained for back-compat; ordering now via _source_order()
) -> dict:
    """
    Return the General Statistics payload, trying each source in _source_order() until one
    yields non-empty totals. The chain always ends with the real-time source so net profit
    and totals still render when the warehouse (API + ClickHouse) is down.
    """
    start, end = _to_date_str(start_date), _to_date_str(end_date)
    last_err: Optional[Exception] = None
    for name in _source_order():
        try:
            stats = _run_source(name, brand_id, company_id, start, end)
        except Exception as ex:
            last_err = ex
            logger.warning("[dashboard] source '%s' failed (%s); trying next", name, ex)
            continue
        if stats and stats.get("totals"):
            if name != _source_order()[0]:
                logger.info("[dashboard] served from fallback source '%s'", name)
            return stats
        logger.warning("[dashboard] source '%s' returned no data; trying next", name)
    if last_err is not None:
        logger.error("[dashboard] all sources failed; last error: %s", last_err)
    return {"totals": {}, "channels": {}, "source": "unavailable"}


def build_pdf_api_metrics(stats: dict) -> dict:
    """
    Convert fetch_general_statistics() output into the {meta, google, organic, amazon, total}
    structure that report_renderer.build_daily_pdf_context() expects.

    Channel rows are the attributed split (Meta/Google/Organic + Amazon marketplace).
    Total is the all-up General Statistics headline (same cards as the dashboard).

    Channel money columns will not always equal Total on their own — build_daily_pdf_context
    adds an Other/Timing residual when needed so:

        Meta + Google + Organic + Amazon + Residual  ==  Total

    Event-date returns/cancels are shown as an informational row (not in the money sum);
    dashboard net_sales / channel breakdowns already use placement-lifecycle netting.
    """
    def _sd(n, d):
        return (n / d) if d else 0.0

    def _enrich(ch):
        s, ad, co, oc = ch["sales"], ch["ad_spend"], ch["cogs"], ch["order_count"]
        margin = s - co
        return {
            **ch,
            "net_profit": round(s - co - ad, 2),
            "gross_roas": round(_sd(s, ad), 2),
            "net_roas": round(_sd(s - co, ad), 2),
            # Dashboard BE ROAS = net_sales / (net_sales - net_cogs)
            "be_roas": round(_sd(s, margin), 2) if margin > 0 else 0.0,
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
    #   BE ROAS = net_sales / (net_sales - cogs)
    # Dashboard "Gross ROAS" (gross_sales / ad_spend) is kept as dashboard_gross_roas.
    _margin = t["net_sales"] - t["total_cogs"]
    total = {
        "sales": round(t["net_sales"], 2),
        "gross_sales": round(t.get("gross_sales", 0.0), 2),
        "ad_spend": round(t["total_ad_spend"], 2),
        "cogs": round(t["total_cogs"], 2),
        "net_profit": round(t["net_profit"], 2),
        "gross_roas": round(_sd(t["net_sales"], t["total_ad_spend"]), 2),
        "dashboard_gross_roas": round(_sd(t.get("gross_sales", 0.0), t["total_ad_spend"]), 2),
        "net_roas": round(_sd(t["net_sales"] - t["total_cogs"], t["total_ad_spend"]), 2),
        "be_roas": round(_sd(t["net_sales"], _margin), 2) if _margin > 0 else 0.0,
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


def build_cohort_pdf_metrics(brand_id: int, start: str, end: str) -> dict:
    """
    Build the {meta, google, organic, amazon, total} PDF structure from the
    order-date cohort rows — the SAME source that feeds every daily graph.

    Because channels and Total share one query, the money columns reconcile
    exactly (channels sum to Total; no event-date returns row, no timing
    residual). Each channel carries the full gross->net bridge:

        gross_sales - returned - cancelled (- discounts)  ==  net_sales   ("sales")
        active_cogs + retcnl_cost                          ==  net_cogs    ("cogs")
        net_profit = net_sales - net_cogs - ad_spend

    ``retcnl_cost`` = net_cogs - active_cogs. For Shopify platforms that equals
    return_cogs + cancel_cogs (reverse-logistics penalties). For Amazon net_cogs
    is a marketplace-fee construct, so retcnl_cost captures the fee/refund delta;
    the identity active + retcnl_cost == net_cogs still holds by construction.
    """
    import pandas as pd

    rows = fetch_order_date_cohort_rows(int(brand_id), start, end)

    def _sd(n, d):
        return (n / d) if d else 0.0

    def _blank_channel() -> dict:
        return {
            "gross_sales": 0.0, "discounts": 0.0,
            "returned_amount": 0.0, "cancelled_amount": 0.0,
            "revenue_adjustment": 0.0,
            "sales": 0.0, "ad_spend": 0.0,
            "gross_cogs": 0.0, "active_cogs": 0.0,
            "return_cogs": 0.0, "cancel_cogs": 0.0,
            "retcnl_cost": 0.0, "cogs_adjustment": 0.0, "cogs": 0.0,
            "order_count": 0, "returned_orders": 0, "cancelled_orders": 0,
        }

    agg = {p: _blank_channel() for p in ("meta", "google", "organic", "amazon")}
    if rows is not None and not rows.empty:
        for _, r in rows.iterrows():
            p = str(r.get("platform") or "").lower()
            if p not in agg:
                continue
            c = agg[p]
            c["gross_sales"] += float(r.get("gross_sales") or 0)
            c["discounts"] += float(r.get("discounts") or 0)
            c["returned_amount"] += float(r.get("returns_amount") or 0)
            c["cancelled_amount"] += float(r.get("cancels_amount") or 0)
            c["sales"] += float(r.get("net_sales") or 0)
            c["ad_spend"] += float(r.get("ad_spend") or 0)
            c["gross_cogs"] += float(r.get("gross_cogs") or 0)
            c["active_cogs"] += float(r.get("active_cogs") or 0)
            c["return_cogs"] += float(r.get("return_cogs") or 0)
            c["cancel_cogs"] += float(r.get("cancel_cogs") or 0)
            c["cogs"] += float(r.get("net_cogs") or 0)
            c["order_count"] += int(r.get("orders") or 0)
            c["returned_orders"] += int(r.get("returned_orders") or 0)
            c["cancelled_orders"] += int(r.get("cancelled_orders") or 0)

    def _finish(c: dict) -> dict:
        for k in ("gross_sales", "discounts", "returned_amount", "cancelled_amount",
                  "sales", "ad_spend", "gross_cogs", "active_cogs",
                  "return_cogs", "cancel_cogs", "cogs"):
            c[k] = round(c[k], 2)
        # Single revenue adjustment: everything that takes Gross Sales → Net Sales
        # (returns + cancels + discounts) on the order-date cohort.
        c["revenue_adjustment"] = round(
            c["returned_amount"] + c["cancelled_amount"] + c["discounts"], 2
        )
        # COGS bridge: gross_cogs + cogs_adjustment == net_cogs (single signed line).
        # adjustment = reverse-logistics/fee cost added  MINUS  returned/cancelled
        # product cost removed; usually small and often negative (net COGS < gross).
        c["cogs_adjustment"] = round(c["cogs"] - c["gross_cogs"], 2)
        # Kept for continuity (active + retcnl == net_cogs).
        c["retcnl_cost"] = round(c["cogs"] - c["active_cogs"], 2)
        gs, s, ad, gco, co, oc = (c["gross_sales"], c["sales"], c["ad_spend"],
                                  c["gross_cogs"], c["cogs"], c["order_count"])
        margin = s - co
        # Dashboard definitions:
        #   Gross Profit = Gross Sales - Ad Spend - Gross COGS
        #   Net Profit   = Net Sales   - Ad Spend - Net COGS
        c["gross_profit"] = round(gs - ad - gco, 2)
        c["net_profit"] = round(s - ad - co, 2)
        c["gross_roas"] = round(_sd(gs, ad), 2)
        c["net_roas"] = round(_sd(s - co, ad), 2)
        c["be_roas"] = round(_sd(s, margin), 2) if margin > 0 else 0.0
        c["cpp"] = round(_sd(ad, oc), 2)
        c["quantity"] = oc
        return c

    meta = _finish(agg["meta"])
    google = _finish(agg["google"])
    organic = _finish(agg["organic"])
    amazon = _finish(agg["amazon"])

    # Total = exact sum of channels (single source => reconciles by construction).
    parts = [meta, google, organic, amazon]
    total = {
        k: round(sum(float(p[k]) for p in parts), 2)
        for k in ("gross_sales", "discounts", "returned_amount", "cancelled_amount",
                  "sales", "ad_spend", "gross_cogs", "active_cogs",
                  "return_cogs", "cancel_cogs", "cogs")
    }
    total["revenue_adjustment"] = round(
        total["returned_amount"] + total["cancelled_amount"] + total["discounts"], 2
    )
    total["cogs_adjustment"] = round(total["cogs"] - total["gross_cogs"], 2)
    total["retcnl_cost"] = round(total["cogs"] - total["active_cogs"], 2)
    total["order_count"] = sum(int(p["order_count"]) for p in parts)
    total["quantity"] = total["order_count"]
    total["returned_orders"] = sum(int(p["returned_orders"]) for p in parts)
    total["cancelled_orders"] = sum(int(p["cancelled_orders"]) for p in parts)
    total["returns_cancels"] = total["returned_orders"] + total["cancelled_orders"]
    total["returned_amount"] = round(total["returned_amount"], 2)
    total["cancelled_amount"] = round(total["cancelled_amount"], 2)
    total["returns_cancels_amount"] = round(total["returned_amount"] + total["cancelled_amount"], 2)
    tgs, ts, tad, tgco, tco = (total["gross_sales"], total["sales"], total["ad_spend"],
                               total["gross_cogs"], total["cogs"])
    tmargin = ts - tco
    total["net_profit"] = round(ts - tad - tco, 2)
    total["gross_profit"] = round(tgs - tad - tgco, 2)
    total["gross_roas"] = round(_sd(tgs, tad), 2)
    total["dashboard_gross_roas"] = round(_sd(total["gross_sales"], tad), 2)
    total["net_roas"] = round(_sd(ts - tco, tad), 2)
    total["be_roas"] = round(_sd(ts, tmargin), 2) if tmargin > 0 else 0.0
    total["cpp"] = round(_sd(tad, total["order_count"]), 2)

    return {"meta": meta, "google": google, "organic": organic,
            "amazon": amazon, "total": total, "source": "order_date_cohort"}


def get_dashboard_pdf_metrics(
    timeframe_start=None,
    timeframe_end=None,
    brand_id: Optional[int] = None,
    company_id: Optional[int] = None,
) -> dict:
    """
    Drop-in replacement for api_data_fetcher.get_organized_metrics_for_pdf(): returns the
    {meta, google, organic, amazon, total} dict for the marketing PDF top section.

    Sourced from the ORDER-DATE COHORT (build_cohort_pdf_metrics) so the channel
    table's gross->net bridge reconciles exactly with the daily graphs. Falls back
    to the General Statistics builder (build_pdf_api_metrics) if the cohort query
    yields nothing.

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

    try:
        metrics = build_cohort_pdf_metrics(brand_id, start, end)
        t = metrics.get("total", {})
        if float(t.get("gross_sales") or 0) or float(t.get("ad_spend") or 0):
            logger.info("[dashboard] PDF metrics %s..%s brand=%s (source=order_date_cohort)",
                        start, end, brand_id)
            return metrics
        logger.warning("[dashboard] cohort metrics empty for %s..%s; falling back to general statistics",
                       start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dashboard] cohort metrics failed (%s); falling back to general statistics", exc)

    stats = fetch_general_statistics(brand_id, company_id, start, end)
    logger.info("[dashboard] PDF metrics %s..%s brand=%s (source=%s)",
                start, end, brand_id, stats.get("source"))
    return build_pdf_api_metrics(stats)
