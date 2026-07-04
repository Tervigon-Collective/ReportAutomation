"""
Dashboard-aligned metric formulas for report automation.

Source of truth: Seleric_Dashboard Node-Backend FORMULA_REFERENCE.md and
fe-dashboard/src/lib/metrics/buildDashboardStats.js / rollupAttributionPnL.js

Prefer pre-computed API fields when present; only calculate when missing.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    try:
        n, d = float(numerator), float(denominator)
        return (n / d) if d else default
    except (TypeError, ValueError):
        return default


def compute_gross_roas(sales: float, ad_spend: float) -> float:
    """Gross ROAS = gross_sales / ad_spend (dashboard cards use gross_sales for gross ROAS)."""
    return safe_div(sales, ad_spend)


def compute_net_roas(net_sales: float, cogs: float, ad_spend: float) -> float:
    """Net ROAS = (net_sales - cogs) / ad_spend."""
    return safe_div(net_sales - cogs, ad_spend)


def compute_be_roas(cogs: float, ad_spend: float) -> float:
    """Break-even ROAS = (cogs + ad_spend) / ad_spend (main dashboard definition)."""
    return safe_div(cogs + ad_spend, ad_spend)


def compute_net_profit(net_sales: float, cogs: float, ad_spend: float) -> float:
    """Net profit = net_sales - cogs - ad_spend."""
    return net_sales - cogs - ad_spend


def compute_cpp(ad_spend: float, order_count: int) -> float:
    return safe_div(ad_spend, order_count)


def compute_ctr(clicks: float, impressions: float) -> float:
    return safe_div(clicks, impressions) * 100.0


def compute_roas(sales: float, spend: float) -> float:
    return safe_div(sales, spend)


def compute_acos(spend: float, sales: float) -> float:
    return safe_div(spend, sales) * 100.0


def compute_amazon_net_profit(
    net_payout: float,
    product_cost: float,
    ad_spend: float,
    *,
    amazon_fees: float = 0.0,
    include_fees_in_cogs: bool = True,
) -> tuple[float, float]:
    """
    Amazon channel net profit aligned with buildAmazonAttributionPayload / amazonOrderPnl.

    Returns (cogs, net_profit) where cogs bundles product_cost + marketplace fees when
    include_fees_in_cogs is True.
    """
    fees = abs(_f(amazon_fees))
    cogs = _f(product_cost) + (fees if include_fees_in_cogs else 0.0)
    net_profit = _f(net_payout) - cogs - _f(ad_spend)
    return cogs, net_profit


def enrich_channel_bucket(
    sales: float,
    ad_spend: float,
    cogs: float,
    order_count: int,
    *,
    net_profit: Optional[float] = None,
    gross_roas: Optional[float] = None,
    net_roas: Optional[float] = None,
    be_roas: Optional[float] = None,
) -> dict:
    """Build a channel metrics dict matching get_organized_metrics_for_pdf shape."""
    s, ad, co, oc = _f(sales), _f(ad_spend), _f(cogs), int(order_count or 0)
    np = _f(net_profit) if net_profit is not None else compute_net_profit(s, co, ad)
    return {
        "sales": round(s, 2),
        "ad_spend": round(ad, 2),
        "cogs": round(co, 2),
        "net_profit": round(np, 2),
        "gross_roas": round(_f(gross_roas) if gross_roas is not None else compute_gross_roas(s, ad), 2),
        "net_roas": round(_f(net_roas) if net_roas is not None else compute_net_roas(s, co, ad), 2),
        "be_roas": round(_f(be_roas) if be_roas is not None else compute_be_roas(co, ad), 2),
        "quantity": oc,
        "cpp": round(compute_cpp(ad, oc), 2),
        "order_count": oc,
    }


def channel_metrics_from_historical_dashboard(data: Mapping[str, Any]) -> dict:
    """
    Extract {meta, google, organic, total} buckets from GET /v1/historical/dashboard data.
    """
    def chan_value(breakdown: Mapping, ch: str, default: float = 0.0) -> float:
        raw = breakdown.get(ch, default) if breakdown else default
        if isinstance(raw, dict):
            return _f(raw.get("total", raw.get("net_sales", 0)))
        return _f(raw, default)

    ad = data.get("ad_spend_breakdown") or {}
    ns = data.get("net_sales_breakdown") or {}
    cogs_bd = data.get("cogs_breakdown") or {}
    orders_bd = data.get("orders_breakdown") or {}
    amazon = data.get("amazon") or {}

    channels = {}
    for ch in ("meta", "google", "organic"):
        spend = chan_value(ad, ch) if ch != "organic" else 0.0
        channels[ch] = enrich_channel_bucket(
            sales=chan_value(ns, ch),
            ad_spend=spend,
            cogs=chan_value(cogs_bd, ch),
            order_count=int(chan_value(orders_bd, ch)),
        )

    amz_ad = ad.get("amazon", {})
    amz_spend = _f(amz_ad.get("total") if isinstance(amz_ad, dict) else amz_ad)
    amazon_bucket = enrich_channel_bucket(
        sales=_f(amazon.get("net_sales")),
        ad_spend=amz_spend,
        cogs=_f(amazon.get("cogs")),
        order_count=int(_f(amazon.get("orders"))),
    )

    totals = enrich_channel_bucket(
        sales=_f(data.get("net_sales")),
        ad_spend=_f(data.get("total_ad_spend")),
        cogs=_f(data.get("total_cogs")),
        order_count=int(_f(data.get("total_orders"))),
        net_profit=_f(data.get("net_profit")),
        gross_roas=compute_gross_roas(_f(data.get("gross_sales")), _f(data.get("total_ad_spend"))),
        net_roas=compute_net_roas(
            _f(data.get("net_sales")), _f(data.get("total_cogs")), _f(data.get("total_ad_spend"))
        ),
        be_roas=compute_be_roas(_f(data.get("total_cogs")), _f(data.get("total_ad_spend"))),
    )
    rc = data.get("returns_cancels") or {}
    totals.update({
        "gross_sales": round(_f(data.get("gross_sales")), 2),
        "returns_cancels": int(rc.get("total_count", 0) or 0),
        "cancelled_orders": int(rc.get("cancelled_count", 0) or 0),
        "returned_orders": int(rc.get("returned_count", 0) or 0),
        "cancelled_amount": round(_f(rc.get("cancelled_amount")), 2),
        "returned_amount": round(_f(rc.get("returned_amount")), 2),
        "returns_cancels_amount": round(_f(rc.get("total_amount")), 2),
    })

    return {
        "meta": channels["meta"],
        "google": channels["google"],
        "organic": channels["organic"],
        "amazon": amazon_bucket,
        "total": totals,
    }


def meta_funnel_from_api(summary: Mapping[str, Any]) -> dict:
    """Map GET /v1/meta-funnel summary to dailyrollup funnel dict shape."""
    impressions = _f(summary.get("total_impressions"))
    clicks = _f(summary.get("total_clicks"))
    landing = _f(summary.get("total_sessions") or summary.get("total_landing_page_views"))
    atc = _f(summary.get("total_add_to_cart"))
    checkout = _f(summary.get("total_initiate_checkout") or summary.get("total_checkout_start"))
    orders = _f(summary.get("total_orders"))
    revenue = _f(summary.get("total_revenue"))
    cogs = _f(summary.get("total_cogs"))
    spend = _f(summary.get("total_spend"))
    net_profit = revenue - cogs - spend

    def rate(n, d):
        return round(safe_div(n, d) * 100.0, 2)

    return {
        "impressions": int(round(impressions)),
        "clicks": int(round(clicks)),
        "landing_page_views": int(round(landing)),
        "add_to_cart": int(round(atc)),
        "checkout": int(round(checkout)),
        "orders": int(round(orders)),
        "net_profit": round(net_profit, 2),
        "ctr": rate(clicks, impressions),
        "landing_page_rate": rate(landing, clicks),
        "add_to_cart_rate": rate(atc, landing),
        "checkout_rate": rate(checkout, atc),
        "conversion_rate": rate(orders, clicks),
        "profit_per_order": round(safe_div(net_profit, orders), 2),
        "drop_off_impressions_to_clicks": rate(impressions - clicks, impressions),
        "drop_off_clicks_to_landing": rate(clicks - landing, clicks),
        "drop_off_landing_to_cart": rate(landing - atc, landing),
        "drop_off_cart_to_checkout": rate(atc - checkout, atc),
        "drop_off_checkout_to_orders": rate(checkout - orders, checkout),
        "drop_off_cart_to_orders": rate(atc - orders, atc),
    }


def channel_funnel_from_api(data: Mapping[str, Any]) -> dict:
    """Map GET /v1/funnel (unified per-channel funnel) to the funnel dict shape.

    Combines ad-delivery metrics from `performance` (impressions, clicks, spend,
    attributed orders/revenue) with the on-site session funnel from `funnel`
    (sessions -> product_view -> add_to_cart -> checkout). Landing-page views map
    to sessions, matching the Meta funnel card semantics (meta_funnel_from_api).
    """
    perf = data.get("performance") or {}
    fn = data.get("funnel") or {}

    impressions = _f(perf.get("impressions"))
    clicks = _f(perf.get("clicks"))
    landing = _f(fn.get("sessions"))
    atc = _f(fn.get("atc_sessions"))
    checkout = _f(fn.get("checkout_sessions"))
    orders = _f(perf.get("attributed_orders") or fn.get("session_purchases"))
    revenue = _f(perf.get("attributed_revenue") or fn.get("session_revenue"))
    spend = _f(perf.get("spend"))
    net_profit = revenue - spend  # no COGS on this endpoint; contribution before COGS

    def rate(n, d):
        return round(safe_div(n, d) * 100.0, 2)

    return {
        "impressions": int(round(impressions)),
        "clicks": int(round(clicks)),
        "landing_page_views": int(round(landing)),
        "add_to_cart": int(round(atc)),
        "checkout": int(round(checkout)),
        "orders": int(round(orders)),
        "net_profit": round(net_profit, 2),
        "ctr": _f(perf.get("ctr")) or rate(clicks, impressions),
        "interaction_rate": _f(perf.get("ctr")) or rate(clicks, impressions),
        "landing_page_rate": rate(landing, clicks),
        "add_to_cart_rate": rate(atc, landing),
        "checkout_rate": rate(checkout, atc),
        "conversion_rate": rate(orders, clicks),
        "drop_off_impressions_to_clicks": rate(impressions - clicks, impressions),
        "drop_off_clicks_to_landing": rate(clicks - landing, clicks),
        "drop_off_landing_to_cart": rate(landing - atc, landing),
        "drop_off_cart_to_checkout": rate(atc - checkout, atc),
        "drop_off_checkout_to_orders": rate(checkout - orders, checkout),
        "drop_off_cart_to_orders": rate(atc - orders, atc),
        "drop_off_clicks_to_orders": rate(clicks - orders, clicks),
    }
