"""
Amazon entity reports sourced from ClickHouse gold tables.
Used by WTD/MTD report for per-timeframe Amazon sheets (Ads + SP).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from revenue_gst import apply_net_revenue_column

load_dotenv()

try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None

# Entity report Amazon data: ClickHouse gold is primary; API is optional fallback.
_AMAZON_ENTITY_CH_PRIMARY = os.getenv(
    "AMAZON_ENTITY_CLICKHOUSE_PRIMARY", "true"
).lower() in ("1", "true", "yes")
_AMAZON_ENTITY_API_FALLBACK = os.getenv(
    "AMAZON_ENTITY_API_FALLBACK", "true"
).lower() in ("1", "true", "yes")


def _entity_clickhouse_primary() -> bool:
    return _AMAZON_ENTITY_CH_PRIMARY


def _entity_api_fallback() -> bool:
    return _AMAZON_ENTITY_API_FALLBACK


def _default_brand_id() -> Optional[int]:
    raw = os.getenv("CLICKHOUSE_BRAND_ID")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_brand_id(brand_id: Optional[int]) -> Optional[int]:
    return brand_id if brand_id is not None else _default_brand_id()


def _fetch_amazon_api_bundle(start_str: str, end_str: str, brand_id: Optional[int]):
    """Load ads, orders, items, pnl from v1 Amazon APIs."""
    try:
        from api_data_fetcher import (
            fetch_amazon_attribution,
            fetch_historical_amazon_sp_sales,
            fetch_historical_amazon_dashboard,
        )
        from api_response_transformers import (
            amazon_ads_daily_from_attribution,
            amazon_ads_from_historical,
            amazon_orders_from_attribution,
            amazon_pnl_from_attribution,
            amazon_line_items_from_attribution,
        )
    except ImportError:
        return None

    attr = fetch_amazon_attribution(start_str, end_str)
    ads_df = amazon_ads_daily_from_attribution(attr) if attr else pd.DataFrame()
    if ads_df.empty and attr is None:
        from api_data_fetcher import fetch_historical_amazon_ads
        hist = fetch_historical_amazon_ads(start_str, end_str)
        if hist:
            ads_df = amazon_ads_from_historical(hist)

    orders_df = amazon_orders_from_attribution(attr) if attr else pd.DataFrame()
    pnl_df = amazon_pnl_from_attribution(attr) if attr else pd.DataFrame()
    items_df = amazon_line_items_from_attribution(attr) if attr else pd.DataFrame()

    if orders_df.empty:
        sp = fetch_historical_amazon_sp_sales(start_str, end_str)
        if sp and isinstance(sp.get("amazon"), dict):
            amz = sp["amazon"]
            orders_df = pd.DataFrame([{
                "amazon_order_id": o.get("amazon_order_id"),
                "purchase_date": o.get("purchase_date"),
                "order_status": o.get("order_status"),
                "order_total": o.get("order_total") or o.get("gross_sales"),
                "items_shipped": o.get("items_shipped", 0),
            } for o in (amz.get("orders") or [])])

    dashboard = fetch_historical_amazon_dashboard(start_str, end_str)
    return {
        "ads_df": ads_df,
        "orders_df": orders_df,
        "items_df": items_df,
        "pnl_df": pnl_df,
        "attr": attr,
        "dashboard": dashboard,
    }


def get_clickhouse_client():
    if clickhouse_connect is None:
        raise ImportError("clickhouse-connect is required. Install with: pip install clickhouse-connect")

    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER"),
        password=os.getenv("CLICKHOUSE_PASSWORD"),
        database=os.getenv("CLICKHOUSE_DATABASE", "gold"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
        connect_timeout=int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "60")),
    )


def _to_date_str(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return value


def fetch_amazon_ads_gold(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
) -> pd.DataFrame:
    """Fetch Amazon Ads campaign daily metrics (ClickHouse primary, API fallback)."""
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)

    def _from_clickhouse() -> pd.DataFrame:
        query = """
            SELECT
                company_id,
                brand_id,
                shop_domain,
                campaign_id,
                campaign_name,
                campaign_type,
                campaign_status,
                report_date,
                COALESCE(impressions, 0) AS impressions,
                COALESCE(clicks, 0) AS clicks,
                COALESCE(cost, 0) AS spend,
                COALESCE(purchases_7d, 0) AS orders,
                COALESCE(sales_7d, 0) AS sales
            FROM gold.fct_amazon_ads_campaigns_daily
            WHERE report_date BETWEEN %(start_date)s AND %(end_date)s
            ORDER BY report_date, campaign_name
        """
        client = get_clickhouse_client()
        result = client.query(
            query, parameters={"start_date": start_str, "end_date": end_str}
        )
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        print(
            f"[ClickHouse Ads] {len(df)} rows for {start_str} to {end_str}"
            + (f", spend={df['spend'].sum():.2f}" if not df.empty else "")
        )
        return df

    def _from_api() -> pd.DataFrame:
        bundle = _fetch_amazon_api_bundle(start_str, end_str, None)
        if bundle and bundle.get("ads_df") is not None and not bundle["ads_df"].empty:
            df = bundle["ads_df"].copy()
            if "report_date" not in df.columns:
                df["report_date"] = None
            print(f"[API Ads] {len(df)} rows for {start_str} to {end_str}")
            return df
        return pd.DataFrame()

    if _entity_clickhouse_primary():
        try:
            df = _from_clickhouse()
            if not df.empty:
                return df
        except Exception as e:
            print(f"[ClickHouse Ads] failed ({e})")
        if not _entity_api_fallback():
            return pd.DataFrame()

    if _entity_api_fallback():
        try:
            return _from_api()
        except Exception as e:
            print(f"[API Ads] failed ({e})")
    return pd.DataFrame()


def _brand_filter_clause(brand_id: Optional[int], prefix: str = "") -> str:
    """Returns ' AND {prefix}brand_id = %(brand_id)s' when brand_id is set, else ''."""
    return f" AND {prefix}brand_id = %(brand_id)s" if brand_id is not None else ""


def _maybe_add_brand_param(params: dict, brand_id: Optional[int]) -> dict:
    if brand_id is not None:
        params["brand_id"] = int(brand_id)
    return params


def fetch_amazon_sp_orders_gold(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch Amazon SP orders (ClickHouse primary, API fallback)."""
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    brand_id = _resolve_brand_id(brand_id)

    def _from_clickhouse() -> pd.DataFrame:
        query = f"""
            SELECT
                company_id,
                brand_id,
                shop_domain,
                amazon_order_id,
                purchase_date,
                order_status,
                fulfillment_channel,
                sales_channel,
                COALESCE(order_total, 0)
                    * if(lower(order_status) IN ('canceled', 'cancelled'), 0, 1) AS order_total,
                currency_code,
                COALESCE(number_of_items_shipped, 0) AS items_shipped,
                COALESCE(number_of_items_unshipped, 0) AS items_unshipped
            FROM gold.fct_amazon_sp_orders
            WHERE purchase_date BETWEEN %(start_date)s AND %(end_date)s
                {_brand_filter_clause(brand_id)}
            ORDER BY purchase_date, amazon_order_id
        """
        client = get_clickhouse_client()
        params = _maybe_add_brand_param(
            {"start_date": start_str, "end_date": end_str}, brand_id
        )
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        print(
            f"[ClickHouse SP Orders] {len(df)} rows for {start_str} to {end_str}"
            + (f" (brand_id={brand_id})" if brand_id is not None else "")
            + (f", revenue={df['order_total'].sum():.2f}" if not df.empty else "")
        )
        return df

    def _from_api() -> pd.DataFrame:
        bundle = _fetch_amazon_api_bundle(start_str, end_str, brand_id)
        if bundle and bundle.get("orders_df") is not None and not bundle["orders_df"].empty:
            print(f"[API SP Orders] {len(bundle['orders_df'])} rows for {start_str} to {end_str}")
            return bundle["orders_df"]
        return pd.DataFrame()

    if _entity_clickhouse_primary():
        try:
            df = _from_clickhouse()
            if not df.empty:
                return df
        except Exception as e:
            print(f"[ClickHouse SP Orders] failed ({e})")
        if not _entity_api_fallback():
            return pd.DataFrame()

    if _entity_api_fallback():
        try:
            return _from_api()
        except Exception as e:
            print(f"[API SP Orders] failed ({e})")
    return pd.DataFrame()


def fetch_amazon_sp_items_gold(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch Amazon SP order line items (ClickHouse primary, API fallback)."""
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    brand_id = _resolve_brand_id(brand_id)

    def _from_clickhouse() -> pd.DataFrame:
        # Filter by order purchase_date (not item purchase_date) so line items stay
        # aligned with orders/P&L even when item rows carry a different timestamp.
        query = f"""
            SELECT
                i.company_id,
                i.brand_id,
                i.shop_domain,
                i.amazon_order_id,
                i.order_item_id,
                i.seller_sku,
                i.asin,
                i.title,
                o.purchase_date AS purchase_date,
                i.order_status,
                COALESCE(i.quantity_ordered, 0) AS quantity_ordered,
                COALESCE(i.quantity_shipped, 0) AS quantity_shipped,
                COALESCE(i.item_price_amount, 0)
                    * if(lower(i.order_status) IN ('canceled', 'cancelled'), 0, 1) AS item_price_amount,
                i.item_price_currency
            FROM gold.fct_amazon_order_items i
            INNER JOIN gold.fct_amazon_sp_orders o
                ON i.amazon_order_id = o.amazon_order_id
            WHERE o.purchase_date BETWEEN %(start_date)s AND %(end_date)s
                AND i.amazon_order_id != ''
                {_brand_filter_clause(brand_id, prefix="o.")}
            ORDER BY purchase_date, amazon_order_id, order_item_id
        """
        client = get_clickhouse_client()
        params = _maybe_add_brand_param(
            {"start_date": start_str, "end_date": end_str}, brand_id
        )
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        print(
            f"[ClickHouse SP Items] {len(df)} rows for {start_str} to {end_str}"
            + (f" (brand_id={brand_id})" if brand_id is not None else "")
            + (f", item revenue={df['item_price_amount'].sum():.2f}" if not df.empty else "")
        )
        return df

    def _from_api() -> pd.DataFrame:
        bundle = _fetch_amazon_api_bundle(start_str, end_str, brand_id)
        if bundle and bundle.get("items_df") is not None and not bundle["items_df"].empty:
            print(f"[API SP Items] {len(bundle['items_df'])} rows for {start_str} to {end_str}")
            return bundle["items_df"]
        return pd.DataFrame()

    if _entity_clickhouse_primary():
        try:
            df = _from_clickhouse()
            if not df.empty:
                return df
        except Exception as e:
            print(f"[ClickHouse SP Items] failed ({e})")
        if not _entity_api_fallback():
            return pd.DataFrame()

    if _entity_api_fallback():
        try:
            return _from_api()
        except Exception as e:
            print(f"[API SP Items] failed ({e})")
    return pd.DataFrame()


def fetch_amazon_sp_order_pnl_gold(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch order-level P&L (ClickHouse primary, API fallback)."""
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    brand_id = _resolve_brand_id(brand_id)

    def _from_clickhouse() -> pd.DataFrame:
        query = f"""
            SELECT
                purchase_date,
                amazon_order_id,
                pnl_status,
                payout_basis,
                coalesce(items_settled, number_of_items_shipped, 0)        AS items,
                order_currency_code                                        AS currency,
                round(order_total_header,            2)                    AS order_total,
                round(effective_gross_revenue,       2)                    AS gross,
                round(effective_refunds,             2)                    AS refunds,
                round(effective_commission,          2)                    AS commission,
                round(effective_closing,             2)                    AS closing,
                round(effective_shipping,            2)                    AS shipping,
                round(effective_tax_withheld,        2)                    AS tax_withheld,
                round(effective_net_payout,          2)                    AS net_payout,
                round(total_cogs,                    2)                    AS cogs,
                round(effective_gross_profit,        2)                    AS gross_profit,
                round(
                    effective_gross_profit
                        / nullIf(toFloat64(effective_gross_revenue), 0) * 100,
                    2
                ) AS gross_margin_pct
            FROM gold.fct_amazon_sp_order_pnl
            WHERE purchase_date BETWEEN %(start_date)s AND %(end_date)s
                {_brand_filter_clause(brand_id)}
            ORDER BY purchase_date DESC, amazon_order_id
        """
        client = get_clickhouse_client()
        params = _maybe_add_brand_param(
            {"start_date": start_str, "end_date": end_str}, brand_id
        )
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        print(
            f"[ClickHouse SP P&L] {len(df)} rows for {start_str} to {end_str}"
            + (f" (brand_id={brand_id})" if brand_id is not None else "")
            + (
                f", gross={pd.to_numeric(df['gross'], errors='coerce').fillna(0).sum():.2f}"
                f", net_payout={pd.to_numeric(df['net_payout'], errors='coerce').fillna(0).sum():.2f}"
                if not df.empty else ""
            )
        )
        return df

    def _from_api() -> pd.DataFrame:
        bundle = _fetch_amazon_api_bundle(start_str, end_str, brand_id)
        if bundle and bundle.get("pnl_df") is not None and not bundle["pnl_df"].empty:
            print(f"[API SP PnL] {len(bundle['pnl_df'])} rows for {start_str} to {end_str}")
            return bundle["pnl_df"]
        dash = bundle.get("dashboard") if bundle else None
        if dash and dash.get("summary"):
            s = dash["summary"]
            if float(s.get("total_net_payout", 0) or 0) > 0:
                print(
                    f"[API SP PnL] using historical/amazon/dashboard summary "
                    f"for {start_str} to {end_str}"
                )
                return pd.DataFrame([{
                    "gross": float(s.get("total_revenue", 0) or 0),
                    "cogs": float(s.get("total_product_cost", 0) or 0),
                    "gross_profit": float(s.get("profit", 0) or 0) + float(s.get("total_spend", 0) or 0),
                    "net_payout": float(s.get("total_net_payout", 0) or 0),
                    "commission": 0.0,
                    "closing": 0.0,
                    "shipping": 0.0,
                    "tax_withheld": 0.0,
                }])
        return pd.DataFrame()

    if _entity_clickhouse_primary():
        try:
            df = _from_clickhouse()
            if not df.empty:
                return df
        except Exception as e:
            print(f"[ClickHouse SP P&L] failed ({e})")
        if not _entity_api_fallback():
            return pd.DataFrame()

    if _entity_api_fallback():
        try:
            return _from_api()
        except Exception as e:
            print(f"[API SP PnL] failed ({e})")
    return pd.DataFrame()


def fetch_amazon_returns_by_delivery_gold(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Approved Returns Report lines keyed by ``return_delivery_date``.

    Headline Amazon refund axis: Seller Central ``Refunded Amount`` for returns
    delivered in the window (Approved only; Rejected omitted), converted to
    **ex-GST** via ``apply_net_revenue_column`` (÷ ``1 + GST_RATE``) so refunds
    match other report revenue that is reported excluding GST.

    Columns:
      - ``refunded_amount_incl_gst`` — raw Seller Central amount
      - ``refunded_amount`` — ex-GST amount used in summary / net sales
    """
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    brand_id = _resolve_brand_id(brand_id)

    query = f"""
        SELECT
            return_delivery_date,
            amazon_order_id,
            amazon_rma_id,
            order_item_id,
            merchant_sku,
            asin,
            item_name,
            order_date,
            return_request_date,
            return_request_status,
            return_type,
            resolution,
            return_reason,
            return_quantity,
            order_quantity,
            round(toFloat64(ifNull(order_amount, 0)), 2) AS order_amount,
            round(toFloat64(ifNull(label_cost, 0)), 2) AS label_cost,
            round(toFloat64(ifNull(refunded_amount, 0)), 2) AS refunded_amount_incl_gst,
            round(toFloat64(ifNull(report_return_label_cost, 0)), 2)
                AS report_return_label_cost
        FROM gold.fct_amazon_return_items
        WHERE return_delivery_date BETWEEN %(start_date)s AND %(end_date)s
            AND is_return_approved = 1
            AND is_return_rejected = 0
            {_brand_filter_clause(brand_id)}
        ORDER BY return_delivery_date, amazon_order_id, merchant_sku
    """
    try:
        client = get_clickhouse_client()
        params = _maybe_add_brand_param(
            {"start_date": start_str, "end_date": end_str}, brand_id
        )
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        if not df.empty:
            incl = pd.to_numeric(df["refunded_amount_incl_gst"], errors="coerce").fillna(0.0)
            df["refunded_amount_incl_gst"] = incl.round(2)
            df["refunded_amount"] = apply_net_revenue_column(incl).round(2)
        refunded = (
            float(pd.to_numeric(df["refunded_amount"], errors="coerce").fillna(0).sum())
            if not df.empty else 0.0
        )
        refunded_incl = (
            float(pd.to_numeric(df["refunded_amount_incl_gst"], errors="coerce").fillna(0).sum())
            if not df.empty else 0.0
        )
        print(
            f"[ClickHouse Returns@delivery] {len(df)} approved lines for "
            f"{start_str} to {end_str}"
            + (f" (brand_id={brand_id})" if brand_id is not None else "")
            + f", refunded_ex_gst={refunded:.2f} (incl_gst={refunded_incl:.2f})"
        )
        return df
    except Exception as e:
        print(f"[ClickHouse Returns@delivery] failed ({e})")
        return pd.DataFrame()


def fetch_amazon_refunds_by_settlement_gold(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Finance refund lines keyed by settlement ``last_posted_date`` (IST date).

    Ops / recon helper only. Headline refunds use return delivery date via
    ``fetch_amazon_returns_by_delivery_gold``.

    Amounts are stored negative in gold; we expose positive magnitudes.
    ``refunded_amount`` = |total_refund_amount| (principal + tax/shipping refunds).
    """
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    brand_id = _resolve_brand_id(brand_id)

    query = f"""
        SELECT
            toDate(last_posted_date) AS settlement_date,
            amazon_order_id,
            order_item_id,
            seller_sku AS merchant_sku,
            asin,
            title AS item_name,
            purchase_date,
            refund_event_count,
            round(abs(toFloat64(ifNull(refund_principal, 0))), 2) AS refund_principal,
            round(abs(toFloat64(ifNull(refund_tax, 0))), 2) AS refund_tax,
            round(abs(toFloat64(ifNull(refund_shipping, 0))), 2) AS refund_shipping,
            round(abs(toFloat64(ifNull(total_refund_amount, 0))), 2) AS refunded_amount,
            round(toFloat64(ifNull(quantity_shipped, 0)), 0) AS return_quantity
        FROM gold.fct_amazon_sp_finance_items
        WHERE toDate(last_posted_date) BETWEEN %(start_date)s AND %(end_date)s
            AND coalesce(total_refund_amount, 0) < -0.01
            {_brand_filter_clause(brand_id)}
        ORDER BY settlement_date, amazon_order_id, merchant_sku
    """
    try:
        client = get_clickhouse_client()
        params = _maybe_add_brand_param(
            {"start_date": start_str, "end_date": end_str}, brand_id
        )
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        refunded = (
            float(pd.to_numeric(df["refunded_amount"], errors="coerce").fillna(0).sum())
            if not df.empty else 0.0
        )
        print(
            f"[ClickHouse Refunds@settlement] {len(df)} settled lines for "
            f"{start_str} to {end_str}"
            + (f" (brand_id={brand_id})" if brand_id is not None else "")
            + f", refunded={refunded:.2f}"
        )
        return df
    except Exception as e:
        print(f"[ClickHouse Refunds@settlement] failed ({e})")
        return pd.DataFrame()


def summarize_amazon_delivery_refunds(returns_df: pd.DataFrame) -> dict:
    """Aggregate approved delivery-dated returns (ex-GST ``refunded_amount``)."""
    empty = {
        "refunds": 0.0,
        "refunds_incl_gst": 0.0,
        "return_lines": 0,
        "return_orders": 0,
        "return_units": 0,
        "return_lines_with_amount": 0,
    }
    if returns_df is None or returns_df.empty:
        return empty

    amt = pd.to_numeric(returns_df.get("refunded_amount"), errors="coerce").fillna(0.0)
    incl = pd.to_numeric(
        returns_df.get("refunded_amount_incl_gst", amt), errors="coerce"
    ).fillna(0.0)
    qty = pd.to_numeric(returns_df.get("return_quantity"), errors="coerce").fillna(0.0)
    orders = (
        int(returns_df["amazon_order_id"].nunique())
        if "amazon_order_id" in returns_df.columns else 0
    )
    return {
        "refunds": float(amt.sum()),
        "refunds_incl_gst": float(incl.sum()),
        "return_lines": int(len(returns_df)),
        "return_orders": orders,
        "return_units": int(qty.sum()),
        "return_lines_with_amount": int((amt > 0.01).sum()),
    }


def summarize_amazon_settlement_refunds(refunds_df: pd.DataFrame) -> dict:
    """Aggregate finance-posted refunds for summary / reconciliation."""
    empty = {
        "refunds": 0.0,
        "return_lines": 0,
        "return_orders": 0,
        "return_units": 0,
        "return_lines_with_amount": 0,
        "refund_principal": 0.0,
    }
    if refunds_df is None or refunds_df.empty:
        return empty

    amt = pd.to_numeric(refunds_df.get("refunded_amount"), errors="coerce").fillna(0.0)
    principal = pd.to_numeric(refunds_df.get("refund_principal"), errors="coerce").fillna(0.0)
    qty = pd.to_numeric(refunds_df.get("return_quantity"), errors="coerce").fillna(0.0)
    orders = (
        int(refunds_df["amazon_order_id"].nunique())
        if "amazon_order_id" in refunds_df.columns else 0
    )
    return {
        "refunds": float(amt.sum()),
        "refund_principal": float(principal.sum()),
        "return_lines": int(len(refunds_df)),
        "return_orders": orders,
        "return_units": int(qty.sum()),
        "return_lines_with_amount": int((amt > 0.01).sum()),
    }


def build_amazon_ads_campaign_rollup(amazon_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ads data at DAILY grain (report_date x campaign) with derived
    metrics and a Grand Total row.

    Columns: report_date, campaign_name, spend, orders, sales, ctr, roas.
    Sort   : report_date ASC, then sales DESC within each day.
    """
    if amazon_df.empty:
        return amazon_df

    numeric_cols = ["spend", "orders", "sales", "impressions", "clicks"]
    for col in numeric_cols:
        if col in amazon_df.columns:
            amazon_df[col] = pd.to_numeric(amazon_df[col], errors="coerce").fillna(0)

    group_cols = [c for c in ["report_date", "campaign_name"] if c in amazon_df.columns]
    if not group_cols:
        # Defensive fallback: nothing to group by.
        return amazon_df

    campaign_rollup = (
        amazon_df.groupby(group_cols, dropna=False)
        .agg({c: "sum" for c in numeric_cols if c in amazon_df.columns})
        .reset_index()
    )

    with np.errstate(divide="ignore", invalid="ignore"):
        campaign_rollup["ctr"] = (
            (campaign_rollup["clicks"] / campaign_rollup["impressions"] * 100)
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )
        campaign_rollup["roas"] = (
            (campaign_rollup["sales"] / campaign_rollup["spend"])
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )

    required_cols = ["report_date", "campaign_name", "spend", "orders", "sales", "ctr", "roas"]
    campaign_rollup = campaign_rollup[[c for c in required_cols if c in campaign_rollup.columns]]

    sort_cols = [c for c in ["report_date"] if c in campaign_rollup.columns]
    if "sales" in campaign_rollup.columns:
        sort_cols.append("sales")
        ascending = [True] * (len(sort_cols) - 1) + [False]
        campaign_rollup = campaign_rollup.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)
    elif sort_cols:
        campaign_rollup = campaign_rollup.sort_values(sort_cols).reset_index(drop=True)

    # Grand Total row spans all dates and all campaigns.
    total_row = {"campaign_name": "Grand Total"}
    if "report_date" in campaign_rollup.columns:
        total_row["report_date"] = ""
    for col in ["spend", "orders", "sales"]:
        if col in campaign_rollup.columns:
            total_row[col] = float(campaign_rollup[col].sum())

    total_row["roas"] = (
        total_row["sales"] / total_row["spend"] if total_row.get("spend", 0) > 0 else 0.0
    )
    total_clicks = amazon_df["clicks"].sum() if "clicks" in amazon_df.columns else 0
    total_impressions = amazon_df["impressions"].sum() if "impressions" in amazon_df.columns else 0
    total_row["ctr"] = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0.0

    return pd.concat([campaign_rollup, pd.DataFrame([total_row])], ignore_index=True)


def get_amazon_clickhouse_summary(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> dict:
    """Summary metrics for email/channel table.

    Returns Amazon revenue + ads spend + order counts plus the P&L-derived
    fields (cogs, units, net_profit, net_roas, gross_profit_after_fees,
    net_payout).

    Amazon Net Profit uses an Amazon-specific COGS definition that bundles
    the marketplace fees together with product cost so the bottom line
    reflects the actual economics of the channel:

        cogs       = product_cost + |commission| + |shipping|
                                  + |closing|    + |tax_withheld|
        net_profit = revenue - cogs - ad_spend
        net_roas   = (revenue - cogs) / ad_spend   (0 if no spend)

    Fees are stored as negative values in the P&L view (deductions from
    gross), so we subtract their signed sum to add their magnitudes onto
    the product cost.

    `pnl_available` is True only when the P&L view returned rows for the
    range, so callers can decide whether to render real values or "N/A".
    """
    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    start_display = datetime.strptime(start_str, "%Y-%m-%d").strftime("%d-%m-%Y")
    end_display = datetime.strptime(end_str, "%Y-%m-%d").strftime("%d-%m-%Y")
    brand_id = _resolve_brand_id(brand_id)

    def _empty_summary() -> dict:
        return {
            "revenue": 0.0,
            "gross_sales": 0.0,
            "refunds": 0.0,
            "refund_principal": 0.0,
            "net_sales": 0.0,
            "spend": 0.0,
            "orders": 0,
            "order_item_lines": 0,
            "cogs": 0.0,
            "product_cost": 0.0,
            "fees_total": 0.0,
            "units": 0,
            "return_lines": 0,
            "return_orders": 0,
            "return_units": 0,
            "net_profit": 0.0,
            "net_roas": 0.0,
            "gross_profit_after_fees": 0.0,
            "net_payout": 0.0,
            "pnl_available": False,
            "refunds_axis": "return_delivery_date",
            "start_date": start_display,
            "end_date": end_display,
            "date_range": f"{start_display} to {end_display}",
            "available": False,
        }

    def _summary_from_clickhouse() -> dict:
        ads_df = fetch_amazon_ads_gold(start_str, end_str)
        sp_df = fetch_amazon_sp_orders_gold(start_str, end_str, brand_id=brand_id)

        try:
            pnl_df = fetch_amazon_sp_order_pnl_gold(
                start_str, end_str, brand_id=brand_id
            )
        except Exception as e:
            print(f"[ClickHouse Amazon] P&L fetch error in summary: {e}")
            pnl_df = pd.DataFrame()

        try:
            items_df = fetch_amazon_sp_items_gold(
                start_str, end_str, brand_id=brand_id
            )
        except Exception as e:
            print(f"[ClickHouse Amazon] items fetch error in summary: {e}")
            items_df = pd.DataFrame()

        returns_df = fetch_amazon_returns_by_delivery_gold(
            start_str, end_str, brand_id=brand_id
        )
        returns_summary = summarize_amazon_delivery_refunds(returns_df)

        ads_spend = float(ads_df["spend"].sum()) if not ads_df.empty else 0.0
        ads_sales = float(ads_df["sales"].sum()) if not ads_df.empty else 0.0
        ads_orders = int(ads_df["orders"].sum()) if not ads_df.empty else 0

        sp_revenue = float(sp_df["order_total"].sum()) if not sp_df.empty else 0.0
        sp_orders = len(sp_df) if not sp_df.empty else 0
        order_item_lines = len(items_df) if not items_df.empty else 0

        def _col_sum(df: pd.DataFrame, col: str) -> float:
            return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum()) \
                if (not df.empty and col in df.columns) else 0.0

        pnl_available = not pnl_df.empty
        pnl_gross = _col_sum(pnl_df, "gross")
        pnl_product_cost = _col_sum(pnl_df, "cogs")
        pnl_gross_profit = _col_sum(pnl_df, "gross_profit")
        pnl_net_payout = _col_sum(pnl_df, "net_payout")

        pnl_commission = _col_sum(pnl_df, "commission")
        pnl_closing = _col_sum(pnl_df, "closing")
        pnl_shipping = _col_sum(pnl_df, "shipping")
        pnl_tax_withheld = _col_sum(pnl_df, "tax_withheld")
        pnl_fees_total = -(pnl_commission + pnl_closing + pnl_shipping + pnl_tax_withheld)
        pnl_cogs = pnl_product_cost + pnl_fees_total

        pnl_units = int(_col_sum(sp_df, "items_shipped"))
        if pnl_units == 0:
            pnl_units = int(_col_sum(pnl_df, "items"))
        if pnl_units == 0 and not items_df.empty:
            qty_col = "quantity_shipped" if "quantity_shipped" in items_df.columns else "quantity_ordered"
            if qty_col in items_df.columns:
                pnl_units = int(_col_sum(items_df, qty_col))

        # Gross = placement-axis SP revenue; refunds = delivery-dated Refunded
        # Amount converted ex-GST (same GST_RATE divisor as other channels).
        gross_sales = (
            pnl_gross if pnl_available and pnl_gross > 0
            else (sp_revenue if sp_revenue > 0 else ads_sales)
        )
        refunds = float(returns_summary["refunds"])
        refunds_incl = float(returns_summary.get("refunds_incl_gst", refunds) or refunds)
        net_sales = gross_sales - refunds
        orders = sp_orders if sp_orders > 0 else ads_orders

        # Channel net profit uses net_sales (gross − delivery-dated ex-GST refunds).
        net_profit = net_sales - pnl_cogs - ads_spend if pnl_available else 0.0
        net_roas = ((net_sales - pnl_cogs) / ads_spend) if pnl_available and ads_spend > 0 else 0.0

        available = (
            not ads_df.empty or not sp_df.empty or pnl_available or not returns_df.empty
        )

        print(
            f"[Amazon recon] {start_str}→{end_str}: "
            f"orders={orders} item_lines={order_item_lines} units={pnl_units} | "
            f"gross={gross_sales:.2f} delivery_refunds_ex_gst={refunds:.2f} "
            f"(incl_gst={refunds_incl:.2f}) net={net_sales:.2f} | "
            f"return_lines={returns_summary['return_lines']} "
            f"return_orders={returns_summary['return_orders']} "
            f"return_units={returns_summary['return_units']}"
        )

        return {
            "revenue": net_sales,
            "gross_sales": gross_sales,
            "refunds": refunds,
            "refunds_incl_gst": refunds_incl,
            "net_sales": net_sales,
            "spend": ads_spend,
            "orders": orders,
            "order_item_lines": order_item_lines,
            "ads_sales": ads_sales,
            "sp_revenue": sp_revenue,
            "cogs": pnl_cogs,
            "product_cost": pnl_product_cost,
            "fees_total": pnl_fees_total,
            "units": pnl_units,
            "return_lines": returns_summary["return_lines"],
            "return_orders": returns_summary["return_orders"],
            "return_units": returns_summary["return_units"],
            "net_profit": net_profit,
            "net_roas": net_roas,
            "gross_profit_after_fees": pnl_gross_profit,
            "net_payout": pnl_net_payout,
            "pnl_available": pnl_available,
            "refunds_axis": "return_delivery_date",
            "start_date": start_display,
            "end_date": end_display,
            "date_range": f"{start_display} to {end_display}",
            "available": available,
        }

    def _summary_from_api() -> Optional[dict]:
        from metric_calculators import compute_amazon_net_profit

        bundle = _fetch_amazon_api_bundle(start_str, end_str, brand_id)
        dash = bundle.get("dashboard") if bundle else None
        attr = bundle.get("attr") if bundle else None
        summary_src = (attr or {}).get("summary") or (dash or {}).get("summary") or {}
        if not summary_src:
            return None

        ads_spend = float(summary_src.get("total_spend", summary_src.get("total_spend", 0)) or 0)
        if not ads_spend and dash:
            ads_spend = float((dash.get("summary") or {}).get("total_spend", 0) or 0)
        revenue = float(
            summary_src.get("total_order_total")
            or summary_src.get("total_revenue")
            or summary_src.get("net_sales")
            or 0
        )
        product_cost = float(summary_src.get("total_product_cost", 0) or 0)
        net_payout = float(summary_src.get("total_net_payout", 0) or 0)
        amazon_fees = float(summary_src.get("total_amazon_fees", 0) or 0)
        orders = int(summary_src.get("total_orders", summary_src.get("orders", 0)) or 0)
        pnl_cogs, net_profit = compute_amazon_net_profit(
            net_payout, product_cost, ads_spend, amazon_fees=amazon_fees
        )
        pnl_available = net_payout > 0 or product_cost > 0
        delivery_refunds = float(
            summary_src.get("delivery_refunds", 0)
            or abs(float(summary_src.get("total_refunds", 0) or 0))
        )
        net_sales = revenue - delivery_refunds
        return {
            "revenue": net_sales,
            "gross_sales": revenue,
            "refunds": delivery_refunds,
            "net_sales": net_sales,
            "spend": ads_spend,
            "orders": orders,
            "order_item_lines": orders,
            "ads_sales": float(summary_src.get("attributed_sales", 0) or 0),
            "sp_revenue": revenue,
            "cogs": pnl_cogs,
            "product_cost": product_cost,
            "fees_total": amazon_fees,
            "units": orders,
            "return_lines": 0,
            "return_orders": 0,
            "return_units": 0,
            "net_profit": (net_sales - pnl_cogs - ads_spend) if pnl_available else 0.0,
            "net_roas": ((net_sales - pnl_cogs) / ads_spend) if pnl_available and ads_spend > 0 else 0.0,
            "gross_profit_after_fees": net_payout - product_cost if pnl_available else 0.0,
            "net_payout": net_payout,
            "pnl_available": pnl_available,
            "start_date": start_display,
            "end_date": end_display,
            "date_range": f"{start_display} to {end_display}",
            "available": True,
        }

    if _entity_clickhouse_primary():
        try:
            result = _summary_from_clickhouse()
            if result.get("available"):
                return result
        except Exception as e:
            print(f"[ClickHouse Amazon] Summary error: {e}")
        if not _entity_api_fallback():
            return _empty_summary()

    if _entity_api_fallback():
        try:
            result = _summary_from_api()
            if result:
                return result
        except Exception as e:
            print(f"[API Amazon summary] failed ({e})")

    return _empty_summary()


def _apply_amazon_ads_formatting(writer, sheet_name: str, campaign_rollup: pd.DataFrame) -> None:
    """Apply WTD/MTD-style formatting to an Amazon ads sheet."""
    workbook = writer.book
    center_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})
    header_fmt = workbook.add_format({
        "bold": True,
        "align": "center",
        "valign": "vcenter",
        "bg_color": "#F2F2F2",
        "border": 1,
    })
    total_fmt = workbook.add_format({"bold": True, "bg_color": "#E6F3FF"})

    worksheet = writer.sheets[sheet_name]
    worksheet.set_column(0, len(campaign_rollup.columns) - 1, None, center_fmt)
    worksheet.freeze_panes(1, 0)
    worksheet.set_row(0, None, header_fmt)
    worksheet.set_row(len(campaign_rollup), None, total_fmt)

    if "roas" in campaign_rollup.columns:
        roas_col = campaign_rollup.columns.get_loc("roas")
        green_fmt = workbook.add_format({"font_color": "#006100", "bg_color": "#C6EFCE"})
        red_fmt = workbook.add_format({"font_color": "#9C0006", "bg_color": "#FFC7CE"})
        worksheet.conditional_format(1, roas_col, len(campaign_rollup), roas_col, {
            "type": "cell", "criteria": ">=", "value": 1, "format": green_fmt,
        })
        worksheet.conditional_format(1, roas_col, len(campaign_rollup), roas_col, {
            "type": "cell", "criteria": "<", "value": 1, "format": red_fmt,
        })

    if "ctr" in campaign_rollup.columns:
        ctr_col = campaign_rollup.columns.get_loc("ctr")
        worksheet.conditional_format(1, ctr_col, len(campaign_rollup), ctr_col, {
            "type": "2_color_scale",
            "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
            "max_type": "num", "max_value": 5, "max_color": "#90EE90",
        })

    if "spend" in campaign_rollup.columns:
        spend_col = campaign_rollup.columns.get_loc("spend")
        spend_values = pd.to_numeric(campaign_rollup["spend"], errors="coerce").fillna(0)
        max_spend = spend_values.max() if len(spend_values) > 0 else 1000
        worksheet.conditional_format(1, spend_col, len(campaign_rollup), spend_col, {
            "type": "2_color_scale",
            "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
            "max_type": "num", "max_value": max_spend, "max_color": "#FFFF00",
        })

    if "sales" in campaign_rollup.columns:
        sales_col = campaign_rollup.columns.get_loc("sales")
        sales_values = pd.to_numeric(campaign_rollup["sales"], errors="coerce").fillna(0)
        max_sales = sales_values.max() if len(sales_values) > 0 else 1000
        worksheet.conditional_format(1, sales_col, len(campaign_rollup), sales_col, {
            "type": "2_color_scale",
            "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
            "max_type": "num", "max_value": max_sales, "max_color": "#90EE90",
        })


# Order-level P&L money columns allocated to line items.
# Finance settlement refunds on the SP sheet stay as ``finance_refunds``
# (purchase-date order P&L). Headline refunds use return delivery date via
# ``fetch_amazon_returns_by_delivery_gold`` — a separate sheet/axis.
_PNL_MONEY_COLS = (
    "gross", "finance_refunds", "commission", "closing", "shipping",
    "tax_withheld", "net_payout", "cogs", "gross_profit",
)


def _allocate_pnl_to_line_items(
    items_df: pd.DataFrame,
    pnl_df: pd.DataFrame,
) -> pd.DataFrame:
    """Join order-level P&L onto line items and allocate money columns.

    Each line's allocation weight is its share of the order's total item
    revenue: ``item_price_amount / sum(item_price_amount per order)``.

    Why this works:
      - Sum-preserving: allocated values sum back to the order-level total.
      - Margin-preserving: gross_profit/gross is constant per order, so the
        per-line gross_margin_pct equals the order-level margin.
      - Canceled-safe: with canceled lines zeroed at SQL, weights become 0/0;
        we fall back to uniform 1/n so each canceled line still gets 0 of the
        (already-0) money columns and we don't produce NaN rows.
    """
    if pnl_df is None or pnl_df.empty:
        return items_df
    if items_df is None or items_df.empty:
        return items_df

    items = items_df.copy()
    pnl = pnl_df.copy()
    # Map finance settlement refunds off the ambiguous ``refunds`` name.
    if "refunds" in pnl.columns and "finance_refunds" not in pnl.columns:
        pnl = pnl.rename(columns={"refunds": "finance_refunds"})

    order_line_rev = (
        items.groupby("amazon_order_id")["item_price_amount"]
        .sum()
        .rename("_order_line_revenue")
        .reset_index()
    )
    order_line_count = (
        items.groupby("amazon_order_id")
        .size()
        .rename("_order_line_count")
        .reset_index()
    )

    items = items.merge(order_line_rev, on="amazon_order_id", how="left")
    items = items.merge(order_line_count, on="amazon_order_id", how="left")

    pnl_cols = [
        c for c in (
            "amazon_order_id", "pnl_status", "payout_basis",
            "order_total", *_PNL_MONEY_COLS,
        )
        if c in pnl.columns
    ]
    items = items.merge(
        pnl[pnl_cols].drop_duplicates(subset=["amazon_order_id"]),
        on="amazon_order_id",
        how="left",
    )

    line_rev = pd.to_numeric(items["item_price_amount"], errors="coerce").fillna(0)
    order_rev = pd.to_numeric(items["_order_line_revenue"], errors="coerce").fillna(0)
    line_count = pd.to_numeric(items["_order_line_count"], errors="coerce").fillna(1).replace(0, 1)

    weight = np.where(
        order_rev > 0,
        line_rev / order_rev.replace(0, np.nan),
        1.0 / line_count,
    )
    weight = pd.Series(weight, index=items.index).fillna(0.0)

    for col in _PNL_MONEY_COLS:
        if col in items.columns:
            order_val = pd.to_numeric(items[col], errors="coerce").fillna(0.0)
            items[col] = (order_val * weight).round(4)

    if "gross" in items.columns and "gross_profit" in items.columns:
        gross = pd.to_numeric(items["gross"], errors="coerce")
        profit = pd.to_numeric(items["gross_profit"], errors="coerce")
        items["gross_margin_pct"] = (
            (profit / gross.replace(0, np.nan) * 100).round(2).fillna(0.0)
        )

    return items.drop(columns=["_order_line_revenue", "_order_line_count"])


_SP_DISPLAY_COLS = [
    "purchase_date", "amazon_order_id", "order_item_id",
    "order_status", "pnl_status", "payout_basis",
    "fulfillment_channel", "sku", "asin", "title",
    "quantity_ordered", "quantity_shipped",
    "gross", "finance_refunds", "commission", "closing", "shipping",
    "tax_withheld", "net_payout", "product_cost", "gross_profit",
    "gross_margin_pct",
]


def _apply_sp_gross_profit_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Recompute gross_profit as net_payout - product_cost/cogs for display."""
    if df is None or df.empty:
        return df
    out = df.copy()
    cogs_col = "product_cost" if "product_cost" in out.columns else (
        "cogs" if "cogs" in out.columns else None
    )
    if "net_payout" in out.columns and cogs_col:
        np_col = pd.to_numeric(out["net_payout"], errors="coerce").fillna(0.0)
        co_col = pd.to_numeric(out[cogs_col], errors="coerce").fillna(0.0)
        out["gross_profit"] = (np_col - co_col).round(4)
        if "gross" in out.columns:
            gross = pd.to_numeric(out["gross"], errors="coerce")
            out["gross_margin_pct"] = (
                (out["gross_profit"] / gross.replace(0, np.nan) * 100)
                .round(2)
                .fillna(0.0)
            )
    return out


def _format_sp_display_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = _apply_sp_gross_profit_rules(df)
    out = out.rename(columns={
        "seller_sku": "sku",
        "item_price_amount": "item_price",
        "cogs": "product_cost",
    })
    return out[[c for c in _SP_DISPLAY_COLS if c in out.columns]]


def _build_orders_without_line_items(
    sp_items_df: pd.DataFrame,
    sp_orders_df: pd.DataFrame,
    sp_pnl_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """One row per order that has orders/P&L data but no line items in gold."""
    if sp_orders_df is None or sp_orders_df.empty:
        return pd.DataFrame()

    item_order_ids: set[str] = set()
    if sp_items_df is not None and not sp_items_df.empty and "amazon_order_id" in sp_items_df.columns:
        item_order_ids = {
            str(order_id).strip()
            for order_id in sp_items_df["amazon_order_id"].dropna().unique()
            if str(order_id).strip()
        }

    orders = sp_orders_df.drop_duplicates(subset=["amazon_order_id"]).copy()
    orders = orders[orders["amazon_order_id"].astype(str).str.strip() != ""]
    missing = orders[~orders["amazon_order_id"].astype(str).isin(item_order_ids)]
    if missing.empty:
        return pd.DataFrame()

    order_cols = [
        c for c in ["amazon_order_id", "purchase_date", "order_status", "fulfillment_channel"]
        if c in missing.columns
    ]
    rows = missing[order_cols].copy()

    if sp_pnl_df is not None and not sp_pnl_df.empty:
        pnl_cols = [
            c for c in (
                "amazon_order_id", "pnl_status", "payout_basis",
                "gross", "finance_refunds", "commission", "closing", "shipping",
                "tax_withheld", "net_payout", "cogs", "gross_profit", "gross_margin_pct",
            )
            if c in sp_pnl_df.columns or c == "finance_refunds"
        ]
        pnl = sp_pnl_df.copy()
        if "refunds" in pnl.columns and "finance_refunds" not in pnl.columns:
            pnl = pnl.rename(columns={"refunds": "finance_refunds"})
        pnl_cols = [c for c in pnl_cols if c in pnl.columns]
        pnl = pnl[pnl_cols].drop_duplicates(subset=["amazon_order_id"])
        rows = rows.merge(pnl, on="amazon_order_id", how="left")

    return _format_sp_display_df(rows)


def _build_sp_line_items_display(
    sp_items_df: pd.DataFrame,
    sp_orders_df: pd.DataFrame,
    sp_pnl_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Build a line-item-level SP display table.

    One row per (amazon_order_id, seller_sku). Joins fulfillment_channel from
    the orders table (not carried on order-items), and, when sp_pnl_df is
    provided, joins + proportionally allocates the order-level P&L money
    columns down to line items. If the items table is unavailable for the
    date range, falls back to a one-row-per-order view so the sheet still has
    something useful.
    """
    if sp_items_df is not None and not sp_items_df.empty:
        items = sp_items_df.copy()

        if sp_orders_df is not None and not sp_orders_df.empty \
                and "amazon_order_id" in sp_orders_df.columns:
            order_meta_cols = [
                c for c in ["amazon_order_id", "fulfillment_channel"]
                if c in sp_orders_df.columns
            ]
            if len(order_meta_cols) > 1:
                order_meta = (
                    sp_orders_df[order_meta_cols]
                    .drop_duplicates(subset=["amazon_order_id"])
                )
                items = items.merge(order_meta, on="amazon_order_id", how="left")

        items = _allocate_pnl_to_line_items(items, sp_pnl_df)
        items = _format_sp_display_df(items)

        missing_orders = _build_orders_without_line_items(
            sp_items_df, sp_orders_df, sp_pnl_df
        )
        if not missing_orders.empty:
            print(
                f"[Amazon SP] Adding {len(missing_orders)} order-level rows "
                f"with no line items in gold.fct_amazon_order_items"
            )
            items = pd.concat([items, missing_orders], ignore_index=True)
            sort_cols = [c for c in ["purchase_date", "amazon_order_id"] if c in items.columns]
            if sort_cols:
                items = items.sort_values(sort_cols).reset_index(drop=True)
        return items

    if sp_orders_df is None or sp_orders_df.empty:
        return pd.DataFrame()

    return _format_sp_display_df(
        _build_orders_without_line_items(pd.DataFrame(), sp_orders_df, sp_pnl_df)
    )


def _apply_sp_sheet_formatting(writer, sheet_name: str, sp_df: pd.DataFrame) -> None:
    """Basic formatting for Amazon SP orders sheet."""
    workbook = writer.book
    center_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})
    header_fmt = workbook.add_format({
        "bold": True,
        "align": "center",
        "valign": "vcenter",
        "bg_color": "#F2F2F2",
        "border": 1,
    })
    worksheet = writer.sheets[sheet_name]
    worksheet.set_column(0, len(sp_df.columns) - 1, None, center_fmt)
    worksheet.freeze_panes(1, 0)
    worksheet.set_row(0, None, header_fmt)


def add_amazon_sheets_for_timeframe(
    writer,
    timeframe_key: str,
    start_date: datetime,
    end_date: datetime,
    round_for_output_fn: Optional[Callable] = None,
    days_lag: int = 0,
    brand_id: Optional[int] = None,
) -> None:
    """
    Add separate Amazon Ads and SP sheets for a WTD/MTD timeframe.

    Uses the same ``start_date`` / ``end_date`` as Meta/Google/Organic sheets.
    ``days_lag`` optionally shifts the end date back (default 0).

    brand_id (optional): if set, filters all Amazon SP fetches to this brand.
    Defaults to CLICKHOUSE_BRAND_ID from the environment when unset.
    """
    brand_id = _resolve_brand_id(brand_id)
    amazon_start = start_date
    amazon_end = end_date - timedelta(days=days_lag)

    # Guard: lag can push end before start (e.g. run on Monday with Mon-start WTD + 1-day lag,
    # or run on the 1st of the month when MTD ends yesterday in the prior month).
    if amazon_end < amazon_start:
        if timeframe_key == 'mtd':
            print(
                f"[{timeframe_key}] Amazon MTD has no complete days yet "
                f"({amazon_start.strftime('%Y-%m-%d')} > {amazon_end.strftime('%Y-%m-%d')}); skipping sheets"
            )
            return
        # For WTD (and any other weekly window): anchor to Monday of amazon_end's week
        amazon_start = (amazon_end - timedelta(days=amazon_end.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    amazon_start_str = amazon_start.strftime("%Y-%m-%d")
    amazon_end_str = amazon_end.strftime("%Y-%m-%d")
    amazon_date_range_str = (
        f"{amazon_start.strftime('%d-%m')} to {amazon_end.strftime('%d-%m')}"
    )

    print(f"[{timeframe_key}] ClickHouse Amazon range: {amazon_start_str} to {amazon_end_str}")

    ads_df = fetch_amazon_ads_gold(amazon_start_str, amazon_end_str)
    sp_orders_df = fetch_amazon_sp_orders_gold(
        amazon_start_str, amazon_end_str, brand_id=brand_id
    )

    # Line items: one row per (amazon_order_id, seller_sku). The SP sheet is
    # written at line-item granularity so each SKU within a multi-item order
    # gets its own row with its own price/quantity. We still keep sp_orders_df
    # around for joining order-level columns (fulfillment_channel) and for the
    # email summary which counts at the order level.
    try:
        sp_items_df = fetch_amazon_sp_items_gold(
            amazon_start_str, amazon_end_str, brand_id=brand_id
        )
    except Exception as e:
        print(f"[{timeframe_key}] Amazon SP items fetch error: {e}")
        sp_items_df = pd.DataFrame()

    # Order-level P&L view -> allocated down to line items in the SP sheet.
    try:
        sp_pnl_df = fetch_amazon_sp_order_pnl_gold(
            amazon_start_str, amazon_end_str, brand_id=brand_id
        )
    except Exception as e:
        print(f"[{timeframe_key}] Amazon SP P&L fetch error: {e}")
        sp_pnl_df = pd.DataFrame()

    # --- Amazon Ads sheet (campaign rollup) ---
    amazon_sheet_name = f"{timeframe_key}_amazon ({amazon_date_range_str})"[:31]
    if not ads_df.empty:
        campaign_rollup = build_amazon_ads_campaign_rollup(ads_df)
        if round_for_output_fn:
            campaign_rollup = round_for_output_fn(campaign_rollup)
        print(
            f"[{timeframe_key}] Writing Amazon Ads sheet '{amazon_sheet_name}': "
            f"{len(campaign_rollup) - 1} campaigns + Grand Total"
        )
        campaign_rollup.to_excel(writer, sheet_name=amazon_sheet_name, index=False)
        try:
            _apply_amazon_ads_formatting(writer, amazon_sheet_name, campaign_rollup)
        except Exception as e:
            print(f"[{timeframe_key}] Amazon Ads formatting error: {e}")
    else:
        print(f"[{timeframe_key}] No Amazon Ads data, creating empty sheet")
        pd.DataFrame().to_excel(writer, sheet_name=amazon_sheet_name, index=False)

    # --- Amazon SP line-item sheet ---
    # One row per (amazon_order_id, seller_sku). Orders with multiple SKUs
    # become multiple rows; canceled orders still appear but their per-line
    # money columns (gross, etc.) are 0 (zeroed in the SQL fetch).
    sp_sheet_name = f"{timeframe_key}_amazon_sp ({amazon_date_range_str})"[:31]
    sp_display = _build_sp_line_items_display(sp_items_df, sp_orders_df, sp_pnl_df)
    if not sp_display.empty:
        if round_for_output_fn:
            sp_display = round_for_output_fn(sp_display)
        print(
            f"[{timeframe_key}] Writing Amazon SP line-item sheet '{sp_sheet_name}': "
            f"{len(sp_display)} line items"
        )
        sp_display.to_excel(writer, sheet_name=sp_sheet_name, index=False)
        try:
            _apply_sp_sheet_formatting(writer, sp_sheet_name, sp_display)
        except Exception as e:
            print(f"[{timeframe_key}] Amazon SP formatting error: {e}")
    else:
        print(f"[{timeframe_key}] No Amazon SP data, creating empty sheet")
        pd.DataFrame().to_excel(writer, sheet_name=sp_sheet_name, index=False)

    # --- Amazon Returns sheet (return_delivery_date, actual Refunded Amount) ---
    returns_sheet_name = f"{timeframe_key}_amazon_returns ({amazon_date_range_str})"[:31]
    try:
        returns_df = fetch_amazon_returns_by_delivery_gold(
            amazon_start_str, amazon_end_str, brand_id=brand_id
        )
    except Exception as e:
        print(f"[{timeframe_key}] Amazon returns fetch error: {e}")
        returns_df = pd.DataFrame()

    if not returns_df.empty:
        returns_out = returns_df.copy()
        if round_for_output_fn:
            returns_out = round_for_output_fn(returns_out)
        totals = summarize_amazon_delivery_refunds(returns_df)
        total_row = {
            "return_delivery_date": "",
            "amazon_order_id": "Grand Total",
            "merchant_sku": "",
            "return_quantity": totals["return_units"],
            "refunded_amount_incl_gst": totals.get("refunds_incl_gst", totals["refunds"]),
            "refunded_amount": totals["refunds"],
        }
        returns_out = pd.concat([returns_out, pd.DataFrame([total_row])], ignore_index=True)
        print(
            f"[{timeframe_key}] Writing Amazon Returns sheet '{returns_sheet_name}': "
            f"{totals['return_lines']} lines / {totals['return_orders']} orders / "
            f"refunded_ex_gst={totals['refunds']:.2f} "
            f"(incl_gst={totals.get('refunds_incl_gst', totals['refunds']):.2f})"
        )
        returns_out.to_excel(writer, sheet_name=returns_sheet_name, index=False)
        try:
            _apply_sp_sheet_formatting(writer, returns_sheet_name, returns_out)
        except Exception as e:
            print(f"[{timeframe_key}] Amazon Returns formatting error: {e}")
    else:
        print(f"[{timeframe_key}] No Amazon returns, creating empty sheet")
        pd.DataFrame().to_excel(writer, sheet_name=returns_sheet_name, index=False)


def add_amazon_sheets_for_previous_day(writer, days_back: int = 1) -> str:
    """
    Backward-compatible helper: add T-1 Amazon sheets using daily timeframe key.
    Prefer add_amazon_sheets_for_timeframe for WTD/MTD integration.
    """
    target = date.today() - timedelta(days=days_back)
    target_dt = datetime.combine(target, datetime.min.time())
    add_amazon_sheets_for_timeframe(
        writer,
        timeframe_key="daily",
        start_date=target_dt,
        end_date=target_dt.replace(hour=23, minute=59, second=59),
        days_lag=0,
    )
    return target.strftime("%Y-%m-%d")
