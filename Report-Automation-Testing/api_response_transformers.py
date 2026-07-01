"""
Flatten Node-Backend v1 JSON responses into pandas DataFrames consumed by dailyrollup
and amazon_entity_report builders.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import pandas as pd


def _time_agg_for_range(start_date: str, end_date: str) -> str:
    return "hourly" if start_date == end_date else "daily"


def _parse_json_field(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _flatten_attribution_rows(
    data: dict,
    source_label: str,
    channel_default: str,
) -> list[dict]:
    """Flatten meta/google attribution hierarchy to marketing-hourly-like rows."""
    if not data:
        return []
    time_agg = data.get("time_aggregation") or "daily"
    time_key = "hourly_data" if time_agg == "hourly" else "daily_data"
    rows: list[dict] = []

    for campaign in data.get("campaigns") or []:
        cid = campaign.get("campaign_id")
        cname = campaign.get("campaign_name") or campaign.get("utm_campaign")
        utm = {
            "utm_source": campaign.get("utm_source"),
            "utm_medium": campaign.get("utm_medium"),
            "utm_campaign": campaign.get("utm_campaign"),
            "utm_content": campaign.get("utm_content"),
            "utm_term": campaign.get("utm_term"),
        }
        for adset in campaign.get("adsets") or []:
            asid = adset.get("adset_id")
            asname = adset.get("adset_name")
            for ad in adset.get("ads") or []:
                aid = ad.get("ad_id")
                aname = ad.get("ad_name")
                buckets = ad.get(time_key) or []
                if not buckets:
                    m = ad.get("metrics") or {}
                    rows.append(_attribution_row(
                        source_label, channel_default, cname, asname, aname,
                        cid, asid, aid, None, 0, m, utm, ad,
                    ))
                    continue
                for bucket in buckets:
                    hour = bucket.get("hour")
                    if hour is None and time_agg == "hourly":
                        hour = 0
                    rows.append(_attribution_row(
                        source_label, channel_default, cname, asname, aname,
                        cid, asid, aid, bucket.get("date"), hour, bucket, utm, ad,
                    ))
    return rows


def _attribution_row(
    source: str,
    channel: str,
    campaign_name,
    adset_name,
    ad_name,
    campaign_id,
    adset_id,
    ad_id,
    date_start,
    hour,
    metrics: dict,
    utm: dict,
    ad_node: dict,
) -> dict:
    m = metrics or {}
    spend = float(m.get("spend", 0) or 0)
    impressions = float(m.get("impressions", 0) or 0)
    clicks = float(m.get("clicks", 0) or 0)
    cpc = float(m.get("cpc", 0) or 0) if m.get("cpc") is not None else (spend / clicks if clicks else 0.0)
    cpm = float(m.get("cpm", 0) or 0) if m.get("cpm") is not None else ((spend / impressions) * 1000 if impressions else 0.0)
    ctr = float(m.get("ctr", 0) or 0) if m.get("ctr") is not None else ((clicks / impressions) * 100 if impressions else 0.0)

    orders = ad_node.get("orders") if isinstance(ad_node, dict) else None
    product_details = None
    if isinstance(ad_node, dict):
        product_details = ad_node.get("product_details")

    return {
        "source": source,
        "channel": channel,
        "attribution_source": source,
        "date_start": str(date_start)[:10] if date_start else None,
        "hour": int(hour) if hour is not None else 0,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "adset_id": adset_id,
        "adset_name": adset_name,
        "ad_id": ad_id,
        "ad_name": ad_name,
        "impressions": impressions,
        "clicks": clicks,
        "spend_cost": spend,
        "cpm": cpm,
        "cpc": cpc,
        "ctr": ctr,
        "action_onsite_web_view_content": float(m.get("view_content", m.get("action_onsite_web_view_content", 0)) or 0),
        "action_onsite_web_add_to_cart": float(m.get("add_to_cart", m.get("action_onsite_web_add_to_cart", 0)) or 0),
        "action_onsite_web_initiate_checkout": float(m.get("initiate_checkout", m.get("action_onsite_web_initiate_checkout", 0)) or 0),
        "action_offsite_pixel_view_content": 0.0,
        "action_offsite_pixel_add_to_cart": 0.0,
        "action_offsite_pixel_initiate_checkout": 0.0,
        "action_landing_page_view": float(m.get("landing_page_views", m.get("action_landing_page_view", 0)) or 0),
        "attributed_orders_count": int(m.get("attributed_orders_count", 0) or 0),
        "attributed_orders_revenue": float(m.get("attributed_orders_revenue", m.get("total_sales", 0)) or 0),
        "attributed_orders_cogs": float(m.get("attributed_orders_cogs", m.get("gross_cogs", 0)) or 0),
        "attributed_orders_quantity": int(m.get("attributed_orders_quantity", 0) or 0),
        "attributed_orders": _parse_json_field(orders),
        "product_details": _parse_json_field(product_details),
        **utm,
    }


def flatten_meta_attribution(data: dict) -> pd.DataFrame:
    rows = _flatten_attribution_rows(data, "Meta Ads", "Meta")
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flatten_google_attribution(data: dict) -> pd.DataFrame:
    rows = _flatten_attribution_rows(data, "Google Ads", "Google")
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flatten_organic_attribution(data: dict) -> pd.DataFrame:
    rows = []
    for order in data.get("orders") or []:
        rows.append({
            "source": "Organic",
            "channel": "Organic",
            "attribution_source": "Organic",
            "date_start": str(order.get("order_date") or order.get("date") or "")[:10],
            "hour": 0,
            "campaign_id": None,
            "campaign_name": order.get("utm_campaign"),
            "adset_id": None,
            "adset_name": order.get("utm_content"),
            "ad_id": None,
            "ad_name": order.get("utm_term"),
            "impressions": 0,
            "clicks": 0,
            "spend_cost": 0.0,
            "cpm": 0.0,
            "cpc": 0.0,
            "ctr": 0.0,
            "action_onsite_web_view_content": 0,
            "action_onsite_web_add_to_cart": 0,
            "action_onsite_web_initiate_checkout": 0,
            "action_offsite_pixel_view_content": 0,
            "action_offsite_pixel_add_to_cart": 0,
            "action_offsite_pixel_initiate_checkout": 0,
            "action_landing_page_view": 0,
            "attributed_orders_count": 1,
            "attributed_orders_revenue": float(order.get("net_revenue_excl_tax") or order.get("gross_revenue_excl_tax") or 0),
            "attributed_orders_cogs": float(order.get("gross_cogs") or order.get("net_cogs") or 0),
            "attributed_orders_quantity": int(order.get("total_quantity") or 0),
            "attributed_orders": [order],
            "product_details": order.get("line_items") or order.get("product_details"),
            "utm_source": order.get("utm_source"),
            "utm_medium": order.get("utm_medium"),
            "utm_campaign": order.get("utm_campaign"),
            "utm_content": order.get("utm_content"),
            "utm_term": order.get("utm_term"),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flatten_all_attribution_to_hourly_df(
    start_date: str,
    end_date: str,
    *,
    fetch_meta,
    fetch_google,
    fetch_organic,
) -> pd.DataFrame:
    """Combine meta + google + organic API payloads into one marketing DataFrame."""
    time_agg = _time_agg_for_range(start_date, end_date)
    params = {"time_aggregation": time_agg}
    frames = []
    meta = fetch_meta(start_date, end_date, **params)
    if meta:
        frames.append(flatten_meta_attribution(meta))
    google = fetch_google(start_date, end_date, **params)
    if google:
        frames.append(flatten_google_attribution(google))
    organic = fetch_organic(start_date, end_date, channel="organic")
    if organic:
        frames.append(flatten_organic_attribution(organic))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    sort_cols = [c for c in ("date_start", "hour", "source", "channel") if c in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols).reset_index(drop=True)
    return out


def amazon_ads_daily_from_attribution(data: dict) -> pd.DataFrame:
    """Build fct_amazon_ads_campaigns_daily-like DataFrame from amazon-attribution campaigns."""
    rows = []
    for campaign in data.get("campaigns") or []:
        cname = campaign.get("campaign_name")
        cid = campaign.get("campaign_id")
        for point in campaign.get("daily_data") or []:
            m = point if "spend" in point else (point.get("metrics") or point)
            rows.append({
                "campaign_id": cid,
                "campaign_name": cname,
                "report_date": point.get("date") or point.get("report_date"),
                "impressions": float(m.get("impressions", 0) or 0),
                "clicks": float(m.get("clicks", 0) or 0),
                "spend": float(m.get("spend", 0) or 0),
                "orders": int(m.get("purchases_7d", m.get("orders", 0)) or 0),
                "sales": float(m.get("sales_7d", m.get("sales", 0)) or 0),
            })
    if rows:
        return pd.DataFrame(rows)
    # fallback: campaign-level totals without daily split
    for campaign in data.get("campaigns") or []:
        m = campaign.get("metrics") or {}
        rows.append({
            "campaign_id": campaign.get("campaign_id"),
            "campaign_name": campaign.get("campaign_name"),
            "report_date": None,
            "impressions": float(m.get("impressions", 0) or 0),
            "clicks": float(m.get("clicks", 0) or 0),
            "spend": float(m.get("spend", 0) or 0),
            "orders": int(m.get("orders", 0) or 0),
            "sales": float(m.get("sales", 0) or 0),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def amazon_ads_from_historical(data: dict) -> pd.DataFrame:
    """Map GET /v1/historical/amazon/ads to ads gold-like DataFrame (no per-day rows)."""
    rows = []
    for c in data.get("campaigns") or []:
        rows.append({
            "campaign_id": c.get("campaign_id"),
            "campaign_name": c.get("campaign_name"),
            "report_date": None,
            "impressions": float(c.get("impressions", 0) or 0),
            "clicks": float(c.get("clicks", 0) or 0),
            "spend": float(c.get("spend", 0) or 0),
            "orders": 0,
            "sales": 0.0,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def amazon_orders_from_attribution(data: dict) -> pd.DataFrame:
    rows = []
    for order in data.get("orders") or []:
        status = str(order.get("order_status") or "").lower()
        cancel_mult = 0 if status in ("canceled", "cancelled") else 1
        rows.append({
            "amazon_order_id": order.get("amazon_order_id") or order.get("order_id"),
            "purchase_date": str(order.get("purchase_date") or order.get("order_date") or "")[:10],
            "order_status": order.get("order_status"),
            "fulfillment_channel": order.get("fulfillment_channel"),
            "order_total": float(order.get("order_total") or order.get("gross_revenue") or 0) * cancel_mult,
            "items_shipped": int(order.get("items_shipped") or order.get("number_of_items_shipped") or 0),
            "currency_code": order.get("currency_code") or "INR",
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def amazon_pnl_from_attribution(data: dict) -> pd.DataFrame:
    rows = []
    for order in data.get("orders") or []:
        status = str(order.get("order_status") or "").lower()
        if status in ("canceled", "cancelled"):
            continue
        rows.append({
            "amazon_order_id": order.get("amazon_order_id") or order.get("order_id"),
            "gross": float(order.get("gross_revenue") or order.get("gross") or 0),
            "cogs": float(order.get("product_cost") or order.get("cogs") or 0),
            "gross_profit": float(order.get("gross_profit") or 0),
            "net_payout": float(order.get("net_payout") or 0),
            "commission": float(order.get("fee_commission") or order.get("commission") or 0),
            "closing": float(order.get("fee_fixed_closing") or order.get("closing") or 0),
            "shipping": float(order.get("fee_shipping_hb") or order.get("shipping") or 0),
            "tax_withheld": float(order.get("total_tax_withheld") or order.get("tax_withheld") or 0),
            "items": int(order.get("items") or order.get("items_shipped") or 0),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def amazon_line_items_from_attribution(data: dict) -> pd.DataFrame:
    rows = []
    for order in data.get("orders") or []:
        oid = order.get("amazon_order_id") or order.get("order_id")
        status = order.get("order_status")
        purchase = str(order.get("purchase_date") or order.get("order_date") or "")[:10]
        cancel_mult = 0 if str(status or "").lower() in ("canceled", "cancelled") else 1
        for item in order.get("line_items") or order.get("items") or []:
            rows.append({
                "amazon_order_id": oid,
                "purchase_date": purchase,
                "order_status": status,
                "order_item_id": item.get("order_item_id") or item.get("id"),
                "seller_sku": item.get("seller_sku") or item.get("sku"),
                "asin": item.get("asin"),
                "title": item.get("title"),
                "quantity_ordered": int(item.get("quantity_ordered") or item.get("quantity") or 0),
                "quantity_shipped": int(item.get("quantity_shipped") or 0),
                "item_price_amount": float(item.get("item_price") or item.get("item_price_amount") or 0) * cancel_mult,
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def meta_campaign_pdf_df(data: dict) -> pd.DataFrame:
    """Campaign-level rows for PDF from meta-attribution summary + campaigns."""
    rows = []
    for campaign in data.get("campaigns") or []:
        m = campaign.get("metrics") or {}
        spend = float(m.get("spend", 0) or 0)
        revenue = float(m.get("attributed_orders_revenue", m.get("total_sales", 0)) or 0)
        cogs = float(m.get("attributed_orders_cogs", m.get("gross_cogs", 0)) or 0)
        orders = int(m.get("attributed_orders_count", m.get("orders", 0)) or 0)
        clicks = float(m.get("clicks", 0) or 0)
        impressions = float(m.get("impressions", 0) or 0)
        rows.append({
            "date_start": data.get("period", {}).get("start"),
            "channel": "Meta",
            "campaign_name": campaign.get("campaign_name"),
            "spend": spend,
            "sales": revenue,
            "shopify_revenue": revenue,
            "cogs": cogs,
            "shopify_cogs": cogs,
            "purchases": orders,
            "shopify_orders": orders,
            "clicks": clicks,
            "impressions": impressions,
            "gross_roas": revenue / spend if spend else 0.0,
            "net_roas": (revenue - cogs) / spend if spend else 0.0,
            "be_roas": (cogs + spend) / spend if spend else 0.0,
            "net_profit": revenue - cogs - spend,
            "roas": revenue / spend if spend else 0.0,
            "breakeven_roas": (cogs + spend) / spend if spend else 0.0,
            "conversion_rate": (orders / clicks * 100) if clicks else 0.0,
            "ctr": (clicks / impressions * 100) if impressions else 0.0,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def sales_by_region_to_state_df(data: dict) -> pd.DataFrame:
    regions = data.get("regions") or data.get("data") or data.get("sales_by_region") or []
    if isinstance(data, list):
        regions = data
    rows = []
    for r in regions:
        if not isinstance(r, dict):
            continue
        rows.append({
            "state": r.get("region") or r.get("shipping_city") or r.get("province") or r.get("state"),
            "total_sales": float(r.get("net_sales") or r.get("total_sales") or r.get("sales") or 0),
            "order_count": int(r.get("orders") or r.get("order_count") or 0),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def time_patterns_daily_df(data: dict) -> pd.DataFrame:
    buckets = data.get("daily") or data.get("buckets") or data.get("daily_data") or []
    if not buckets and isinstance(data.get("data"), dict):
        buckets = data["data"].get("daily") or data["data"].get("buckets") or []
    rows = []
    for b in buckets:
        if not isinstance(b, dict):
            continue
        rows.append({
            "sale_date": b.get("date") or b.get("report_date") or b.get("bucket"),
            "revenue": float(b.get("net_sales") or b.get("revenue") or 0),
            "cogs": float(b.get("cogs") or b.get("total_cogs") or 0),
            "total_ad_spend": float(b.get("ad_spend") or b.get("total_ad_spend") or 0),
            "net_profit": float(b.get("net_profit") if b.get("net_profit") is not None else 0),
            "gross_sales": float(b.get("gross_sales") or 0),
        })
    if rows:
        df = pd.DataFrame(rows)
        if "net_profit" in df.columns and df["net_profit"].eq(0).all():
            df["net_profit"] = df["revenue"] - df["cogs"] - df["total_ad_spend"]
        return df
    return pd.DataFrame()
