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


def _has_delivery_metrics(metrics: Optional[dict]) -> bool:
    m = metrics or {}
    return (
        float(m.get("spend", 0) or 0) > 0
        or float(m.get("clicks", 0) or 0) > 0
        or float(m.get("impressions", 0) or 0) > 0
    )


def _strip_delivery_metrics(metrics: dict) -> dict:
    """Keep attribution fields; zero spend/clicks/impressions (Google hoisted spend)."""
    out = dict(metrics or {})
    for key in ("spend", "impressions", "clicks", "cpc", "cpm", "ctr"):
        out[key] = 0
    return out


def _strip_attribution_metrics(metrics: dict) -> dict:
    """Keep delivery fields; zero order attribution (avoid double-count with ad rows)."""
    out = dict(metrics or {})
    for key in (
        "attributed_orders_count",
        "attributed_orders_revenue",
        "attributed_orders_cogs",
        "attributed_orders_quantity",
        "total_sales",
        "returns_cancels",
        "gross_cogs",
        "net_cogs",
    ):
        out[key] = 0
    return out


def _has_attribution_metrics(metrics: Optional[dict]) -> bool:
    m = metrics or {}
    return (
        int(m.get("attributed_orders_count", 0) or 0) > 0
        or float(m.get("attributed_orders_revenue", 0) or 0) > 0
    )


def _ad_tree_has_attribution(campaign: dict, time_key: str) -> bool:
    for adset in campaign.get("adsets") or []:
        for ad in adset.get("ads") or []:
            if _has_attribution_metrics(ad.get("metrics")):
                return True
            if any(_has_attribution_metrics(b) for b in (ad.get(time_key) or [])):
                return True
    return False


def _flatten_google_attribution_rows(data: dict) -> list[dict]:
    """
    Google hourly spend is campaign-level (hoisted to campaign.hourly_data).
    - Brand Search: delivery on campaign.hourly_data, orders on ad nodes.
    - PMax: delivery + orders co-located on campaign.hourly_data (no ad tree).
    - sag_organic: attribution-only buckets on campaign.hourly_data (no ad tree).
    """
    if not data:
        return []
    time_agg = data.get("time_aggregation") or "daily"
    time_key = "hourly_data" if time_agg == "hourly" else "daily_data"
    period = data.get("period") or {}
    period_start = period.get("start") or period.get("end")
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

        campaign_buckets = campaign.get(time_key) or []
        has_ad_tree = bool(campaign.get("adsets"))
        campaign_has_hoisted_delivery = bool(campaign_buckets) and any(
            _has_delivery_metrics(b) for b in campaign_buckets
        )
        ad_has_delivery = any(
            _has_delivery_metrics(ad.get("metrics"))
            or any(_has_delivery_metrics(b) for b in (ad.get(time_key) or []))
            for adset in campaign.get("adsets") or []
            for ad in adset.get("ads") or []
        )

        for bucket in campaign_buckets:
            has_delivery = _has_delivery_metrics(bucket)
            has_attr = _has_attribution_metrics(bucket)
            if not has_delivery and not (has_attr and not has_ad_tree):
                continue
            hour = bucket.get("hour")
            if hour is None and time_agg == "hourly":
                hour = 0
            rows.append(_attribution_row(
                "Google Ads", "Google", cname, None, None,
                cid, None, None, bucket.get("date") or period_start, hour,
                bucket, utm, {},
            ))

        if (
            time_agg == "daily"
            and not campaign_buckets
            and not ad_has_delivery
            and _has_delivery_metrics(campaign.get("metrics"))
        ):
            metrics = campaign.get("metrics") or {}
            if _ad_tree_has_attribution(campaign, time_key):
                metrics = _strip_attribution_metrics(metrics)
            rows.append(_attribution_row(
                "Google Ads", "Google", cname, None, None,
                cid, None, None, period_start, 0,
                metrics, utm, {},
            ))

        strip_ad_delivery = campaign_has_hoisted_delivery or (
            time_agg == "daily"
            and not ad_has_delivery
            and _has_delivery_metrics(campaign.get("metrics"))
        )

        for adset in campaign.get("adsets") or []:
            asid = adset.get("adset_id")
            asname = adset.get("adset_name")
            for ad in adset.get("ads") or []:
                aid = ad.get("ad_id")
                aname = ad.get("ad_name")
                buckets = ad.get(time_key) or []
                if not buckets:
                    m = ad.get("metrics") or {}
                    if strip_ad_delivery:
                        m = _strip_delivery_metrics(m)
                    rows.append(_attribution_row(
                        "Google Ads", "Google", cname, asname, aname,
                        cid, asid, aid, period_start, 0, m, utm, ad,
                    ))
                    continue
                for bucket in buckets:
                    hour = bucket.get("hour")
                    if hour is None and time_agg == "hourly":
                        hour = 0
                    metrics = _strip_delivery_metrics(bucket) if strip_ad_delivery else bucket
                    rows.append(_attribution_row(
                        "Google Ads", "Google", cname, asname, aname,
                        cid, asid, aid, bucket.get("date"), hour, metrics, utm, ad,
                    ))

        if not has_ad_tree and not _ad_tree_has_attribution(campaign, time_key):
            camp_metrics = campaign.get("metrics") or {}
            hourly_attr_orders = sum(
                int(b.get("attributed_orders_count", 0) or 0)
                for b in campaign_buckets
                if _has_attribution_metrics(b)
            )
            camp_orders = int(camp_metrics.get("attributed_orders_count", 0) or 0)
            if camp_orders > hourly_attr_orders and _has_attribution_metrics(camp_metrics):
                rows.append(_attribution_row(
                    "Google Ads", "Google", cname, None, None,
                    cid, None, None, period_start, 0,
                    _strip_delivery_metrics(camp_metrics), utm, {},
                ))
    return rows


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
        if not product_details and orders:
            product_details = orders
    # Google PMax / sag_organic: orders may live on the hourly bucket, not ad_node
    if not product_details:
        bucket_orders = m.get("attributed_orders") or m.get("orders")
        if bucket_orders:
            product_details = bucket_orders

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


def _google_campaign_key(campaign_id, campaign_name=None, utm_campaign=None) -> str:
    cid = str(campaign_id) if campaign_id not in (None, "") else "0"
    name = str(campaign_name or utm_campaign or "").strip()
    return f"{cid}|{name}"


def _order_revenue(order: dict) -> float:
    for key in ("revenue", "total", "total_price", "net_revenue_excl_tax", "gross_revenue_excl_tax"):
        val = order.get(key)
        if val not in (None, ""):
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def _attach_google_orders_to_rows(rows: list[dict], top_orders: list[dict]) -> None:
    """Attach payload.orders to flattened rows missing product_details (PMax, sag_organic)."""
    by_campaign: dict[str, list[dict]] = {}
    orphan_orders: list[dict] = []

    for order in top_orders:
        if not isinstance(order, dict):
            continue
        cid = order.get("campaign_id")
        cname = order.get("campaign_name")
        if cid in (None, "", 0, "0") and not cname:
            orphan_orders.append(order)
            continue
        key = _google_campaign_key(cid, cname, order.get("utm_campaign"))
        by_campaign.setdefault(key, []).append(order)

    for row in rows:
        if int(row.get("attributed_orders_count") or 0) <= 0:
            continue
        if row.get("product_details"):
            continue

        key = _google_campaign_key(
            row.get("campaign_id"),
            row.get("campaign_name"),
            row.get("utm_campaign"),
        )
        candidates = list(by_campaign.get(key, []))

        # sag_organic / unattributed Google: campaign_id=0, name from utm_campaign
        if not candidates and orphan_orders:
            if str(row.get("campaign_name") or "").strip().lower() in ("sag_organic", ""):
                row_rev = float(row.get("attributed_orders_revenue") or 0)
                candidates = [
                    o for o in orphan_orders
                    if abs(_order_revenue(o) - row_rev) < 0.05
                ] or list(orphan_orders)

        if candidates:
            row["product_details"] = candidates
            row["attributed_orders"] = candidates


def flatten_google_attribution(data: dict) -> pd.DataFrame:
    rows = _flatten_google_attribution_rows(data)
    if rows and data.get("orders"):
        _attach_google_orders_to_rows(rows, data.get("orders") or [])
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flatten_organic_attribution(data: dict) -> pd.DataFrame:
    rows = []
    for order in data.get("orders") or []:
        line_items = order.get("line_items") or order.get("items") or order.get("product_details") or []
        item_qty = sum(int(i.get("quantity") or i.get("quantity_ordered") or 1) for i in line_items if isinstance(i, dict))
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
            "attributed_orders_revenue": float(
                order.get("net_revenue_excl_tax")
                or order.get("gross_revenue_excl_tax")
                or order.get("revenue")
                or order.get("net_revenue")
                or order.get("gross_revenue")
                or 0
            ),
            "attributed_orders_cogs": float(
                order.get("gross_cogs")
                or order.get("net_cogs")
                or order.get("cogs")
                or order.get("total_cogs")
                or 0
            ),
            "attributed_orders_quantity": int(order.get("total_quantity") or item_qty or 1),
            "attributed_orders": [order],
            "product_details": line_items,
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
        fees = order.get("fees") if isinstance(order.get("fees"), dict) else {}
        commission = float(fees.get("fee_commission") or order.get("fee_commission") or order.get("commission") or 0)
        closing = float(
            fees.get("fee_fixed_closing")
            or fees.get("fee_variable_closing")
            or order.get("fee_fixed_closing")
            or order.get("closing")
            or 0
        )
        shipping = float(fees.get("fee_shipping_hb") or order.get("fee_shipping_hb") or order.get("shipping") or 0)
        tax_withheld = float(
            fees.get("total_tax_withheld")
            or order.get("total_tax_withheld")
            or order.get("tax_withheld")
            or 0
        )
        if tax_withheld == 0 and fees:
            tax_withheld = sum(
                float(fees.get(k) or 0)
                for k in ("tcs_igst", "tcs_cgst", "tcs_sgst", "item_tds")
            )
        amazon_fees = float(order.get("amazon_fees") or fees.get("total_amazon_fees") or 0)
        if amazon_fees > 0 and (commission + closing + shipping + tax_withheld) == 0:
            commission = amazon_fees
        rows.append({
            "amazon_order_id": order.get("amazon_order_id") or order.get("order_id"),
            "purchase_date": str(order.get("purchase_date") or order.get("order_date") or "")[:10],
            "pnl_status": order.get("pnl_status") or order.get("order_status"),
            "payout_basis": order.get("payout_basis"),
            "gross": float(order.get("gross_revenue") or order.get("gross") or order.get("gross_sales") or 0),
            "cogs": float(order.get("product_cost") or order.get("total_cogs") or order.get("cogs") or 0),
            "gross_profit": float(order.get("gross_profit") or 0),
            "net_payout": float(order.get("net_payout") or 0),
            "commission": commission,
            "closing": closing,
            "shipping": shipping,
            "tax_withheld": tax_withheld,
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
            qty_ordered = int(item.get("quantity_ordered") or item.get("quantity") or 0)
            qty_shipped = int(item.get("quantity_shipped") or 0)
            if qty_shipped == 0 and qty_ordered > 0:
                qty_shipped = qty_ordered
            rows.append({
                "amazon_order_id": oid,
                "purchase_date": purchase,
                "order_status": status,
                "order_item_id": item.get("order_item_id") or item.get("id"),
                "seller_sku": item.get("seller_sku") or item.get("sku"),
                "asin": item.get("asin"),
                "title": item.get("title"),
                "quantity_ordered": qty_ordered,
                "quantity_shipped": qty_shipped,
                "item_price_amount": float(item.get("item_price") or item.get("item_price_amount") or item.get("item_revenue") or item.get("gross_revenue") or 0) * cancel_mult,
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
    regions = (
        data.get("regions")
        or data.get("data")
        or data.get("sales_by_region")
        or data.get("sales_by_province")
        or []
    )
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
    buckets = (
        data.get("daily_breakdown")
        or data.get("daily")
        or data.get("buckets")
        or data.get("daily_data")
        or []
    )
    if not buckets and isinstance(data.get("data"), dict):
        buckets = data["data"].get("daily") or data["data"].get("buckets") or []
    if not buckets:
        hourly = data.get("hourly_breakdown") or []
        if hourly:
            period = data.get("period") or {}
            sale_date = str(period.get("end") or period.get("start") or "")[:10]
            rev = sum(float(h.get("net_sales") or h.get("revenue") or 0) for h in hourly)
            cogs = sum(float(h.get("net_cogs") or h.get("cogs") or h.get("total_cogs") or 0) for h in hourly)
            spend = sum(float(h.get("total_ad_spend") or h.get("ad_spend") or 0) for h in hourly)
            np = sum(float(h.get("net_profit") or 0) for h in hourly)
            if sale_date:
                return pd.DataFrame([{
                    "sale_date": sale_date,
                    "revenue": rev,
                    "cogs": cogs,
                    "total_ad_spend": spend,
                    "net_profit": np if np else rev - cogs - spend,
                    "gross_sales": sum(float(h.get("gross_sales") or 0) for h in hourly),
                }])
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
