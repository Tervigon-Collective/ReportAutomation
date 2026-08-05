"""Reconcile order-date cohort unit formulas against dashboard_master.sql."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amazon_entity_report import get_clickhouse_client
from dashboard_stats import _prefixed_master_sql, fetch_order_date_cohort_rows

BRAND = 20
START = "2026-07-07"
END = "2026-08-05"

UNIT_SQL = r"""
WITH
orders_dedup AS (
  SELECT brand_id, order_id,
    argMax(order_date,_loaded_at) order_date, argMax(order_status,_loaded_at) order_status,
    argMax(is_test,_loaded_at) is_test, argMax(is_revenue_adjustment,_loaded_at) ira,
    toFloat64(argMax(net_revenue,_loaded_at)) nr, toFloat64(argMax(net_revenue_excl_tax,_loaded_at)) nret,
    toFloat64(argMax(gross_revenue,_loaded_at)) gr, toFloat64(argMax(gross_revenue_excl_tax,_loaded_at)) gret,
    toFloat64(argMax(total_discounts,_loaded_at)) td, toFloat64(argMax(total_tax,_loaded_at)) tt
  FROM gold.fct_orders WHERE brand_id={brandId:Int64} GROUP BY brand_id, order_id
),
order_day AS (
  SELECT * FROM orders_dedup
  WHERE order_date>=toDate({startDate:String}) AND order_date<=toDate({endDate:String})
    AND coalesce(is_test,0)=0 AND lowerUTF8(trimBoth(coalesce(order_status,'')))!='voided' AND coalesce(ira,0)=0
),
gross AS (
  SELECT round(sum(
    (if(nr>0, gr-(gr*(nr-nret)/nr), gret))
    + (if(nr>0 AND gret>nret, gret-nret, if(tt>0 AND gr>0, td*((gr-tt)/gr), td)))
  ),2) AS gross_sales,
  round(sum(if(nr>0 AND gret>nret, gret-nret, if(tt>0 AND gr>0, td*((gr-tt)/gr), td))),2) AS discounts,
  count() AS orders
  FROM order_day
),
g AS (
  SELECT brand_id, order_id,
    maxIf(1, upperUTF8(trimBoth(coalesce(pnl_refund_class,'')))='RETURN') has_return,
    maxIf(1, upperUTF8(trimBoth(coalesce(pnl_refund_class,'')))='CANCELLATION') has_cancel
  FROM gold.fct_order_items WHERE brand_id={brandId:Int64} AND coalesce(is_gift_card,0)=0
  GROUP BY brand_id, order_id
),
ret AS (
  SELECT round(sumIf(
    if(toFloat64(coalesce(oi.returned_revenue_excl_gst,0))>0, toFloat64(oi.returned_revenue_excl_gst),
       toFloat64(coalesce(oi.net_pre_refund_excl_gst,0))+toFloat64(coalesce(oi.discount_excl_gst,0))),
    g.has_return=1),2) AS returns_amount
  FROM gold.fct_order_items oi
  INNER JOIN order_day od ON od.brand_id=oi.brand_id AND od.order_id=oi.order_id
  INNER JOIN g ON g.brand_id=oi.brand_id AND g.order_id=oi.order_id
  WHERE oi.brand_id={brandId:Int64} AND coalesce(oi.is_gift_card,0)=0 AND g.has_return=1
    AND coalesce(oi.returned_at, if(oi.order_status IN ('refunded','partially_refunded') AND oi.return_status='NO_RETURN' AND oi.refunded_quantity>0, oi.refunded_at, NULL)) IS NOT NULL
),
can AS (
  SELECT round(sumIf(
    if(toFloat64(coalesce(oi.cancelled_revenue_excl_gst,0))>0, toFloat64(oi.cancelled_revenue_excl_gst),
       toFloat64(coalesce(oi.net_pre_refund_excl_gst,0))+toFloat64(coalesce(oi.discount_excl_gst,0))),
    g.has_return=0 AND g.has_cancel=1),2) AS cancels_amount
  FROM gold.fct_order_items oi
  INNER JOIN order_day od ON od.brand_id=oi.brand_id AND od.order_id=oi.order_id
  INNER JOIN g ON g.brand_id=oi.brand_id AND g.order_id=oi.order_id
  WHERE oi.brand_id={brandId:Int64} AND oi.is_cancelled_line=1 AND coalesce(oi.is_gift_card,0)=0
    AND coalesce(oi.cancelled_at, if(oi.order_status='voided', oi.voided_at, NULL)) IS NOT NULL
),
cogs AS (
  SELECT round(
    sumIf(toFloat64(oi.total_cost), oi.pnl_refund_class='ACTIVE')
    + sumIf(toFloat64(coalesce(oi.placed_shipping_cost,0)), oi.pnl_refund_class='ACTIVE')
    + sumIf(toFloat64(coalesce(oi.placed_packaging_cost,0)), oi.pnl_refund_class='ACTIVE')
    + sumIf(toFloat64(coalesce(oi.placed_gateway_fee,0)), oi.pnl_refund_class='ACTIVE' AND coalesce(oi.is_cod,0)=0 AND coalesce(oi.is_online_payment,0)=1)
  ,2) AS active_cogs
  FROM gold.fct_order_items oi
  INNER JOIN order_day od ON od.brand_id=oi.brand_id AND od.order_id=oi.order_id
  WHERE oi.brand_id={brandId:Int64} AND coalesce(oi.is_placement_gross_eligible,0)=1 AND coalesce(oi.is_gift_card,0)=0
)
SELECT gross.orders, gross.gross_sales, gross.discounts, ret.returns_amount, can.cancels_amount, cogs.active_cogs,
  round(gross.gross_sales - ret.returns_amount - can.cancels_amount - gross.discounts, 2) AS net_sales
FROM gross, ret, can, cogs
"""


def main() -> None:
    c = get_clickhouse_client()
    rows = fetch_order_date_cohort_rows(BRAND, START, END)
    print("cohort rows", len(rows), "platforms", sorted(rows.platform.unique()))
    print("30d cohort totals:")
    for col in [
        "orders",
        "gross_sales",
        "discounts",
        "returns_amount",
        "cancels_amount",
        "net_sales",
        "net_cogs",
        "active_cogs",
        "return_cogs",
        "cancel_cogs",
        "ad_spend",
        "net_profit",
    ]:
        print(f"  {col}", round(float(rows[col].sum()), 2) if col in rows.columns else "MISSING")

    r = c.query(
        UNIT_SQL,
        parameters={"brandId": BRAND, "startDate": START, "endDate": END},
    )
    unit = dict(zip(r.column_names, r.result_rows[0]))
    print("unit shopify (dashboard formulas on order set):", unit)

    shop = rows[rows.platform != "amazon"]
    cohort_shop = {
        "orders": int(shop.orders.sum()),
        "gross_sales": round(float(shop.gross_sales.sum()), 2),
        "discounts": round(float(shop.discounts.sum()), 2),
        "returns_amount": round(float(shop.returns_amount.sum()), 2),
        "cancels_amount": round(float(shop.cancels_amount.sum()), 2),
        "active_cogs": round(float(shop.active_cogs.sum()), 2),
        "net_sales": round(float(shop.net_sales.sum()), 2),
        "net_cogs": round(float(shop.net_cogs.sum()), 2),
        "return_cogs": round(float(shop.return_cogs.sum()), 2),
        "cancel_cogs": round(float(shop.cancel_cogs.sum()), 2),
    }
    print("cohort shopify sum:", cohort_shop)

    diffs = []
    for k in ("orders", "gross_sales", "discounts", "returns_amount", "cancels_amount", "active_cogs", "net_sales"):
        a = float(unit[k])
        b = float(cohort_shop[k])
        if abs(a - b) > 0.05:
            diffs.append((k, a, b, round(b - a, 2)))
    print("shopify unit diffs (cohort - unit):", diffs or "NONE")

    master = _prefixed_master_sql()
    rm = c.query(master, parameters={"brandId": BRAND, "startDate": START, "endDate": END})
    import pandas as pd

    mdf = pd.DataFrame(rm.result_rows, columns=rm.column_names)
    print(
        "dashboard master 30d amazon_gross",
        round(float(mdf.amazon_gross_revenue.sum()), 2),
        "amazon_net",
        round(float(mdf.amazon_net_revenue.sum()), 2),
        "amazon_cogs",
        round(float(mdf.amazon_net_cogs.sum()), 2),
        "amazon_orders",
        int(mdf.amazon_orders.sum()),
    )
    amz = rows[rows.platform == "amazon"]
    print(
        "cohort amazon",
        {
            "orders": int(amz.orders.sum()),
            "gross": round(float(amz.gross_sales.sum()), 2),
            "net": round(float(amz.net_sales.sum()), 2),
            "cogs": round(float(amz.net_cogs.sum()), 2),
        },
    )
    print(
        "spend master",
        round(float(mdf.total_ad_spend.sum()), 2),
        "cohort",
        round(float(rows.ad_spend.sum()), 2),
    )


if __name__ == "__main__":
    main()
