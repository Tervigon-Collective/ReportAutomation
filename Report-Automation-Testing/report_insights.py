"""
Generates narrative insight bullets from report data.
Called by email/PDF generation to produce actionable text.
"""
from __future__ import annotations
import pandas as pd


def _fmt_inr(val: float) -> str:
    """Format a number as ₹ with K/L suffix."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "₹0"
    if abs(v) >= 1_00_000:
        return f"₹{v/1_00_000:.1f}L"
    if abs(v) >= 1_000:
        return f"₹{v/1_000:.1f}K"
    return f"₹{v:.0f}"


def _pct_change(now: float, prev: float) -> str | None:
    """Return '+X%' or '-X%' string, None if prev is zero."""
    try:
        if not prev:
            return None
        pct = (now - prev) / abs(prev) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1f}%"
    except Exception:
        return None


def generate_daily_insights(
    api_metrics: dict,
    campaign_df: pd.DataFrame | None = None,
    funnel_metrics: dict | None = None,
    google_funnel: dict | None = None,
    prev_day_metrics: dict | None = None,
) -> list[str]:
    """
    Build a list of narrative insight strings from today's data.

    Args:
        api_metrics:       Return value of get_organized_metrics_for_pdf()
        campaign_df:       DataFrame from get_campaign_data() / build_meta_campaigns_rollup()
        funnel_metrics:    Return value of get_meta_funnel_metrics()
        google_funnel:     Return value of get_google_funnel_metrics()
        prev_day_metrics:  Same structure as api_metrics but for the previous day (optional)

    Returns:
        List of insight strings (each is one sentence / bullet).
    """
    insights: list[str] = []
    meta = api_metrics.get("meta", {})
    google = api_metrics.get("google", {})
    organic = api_metrics.get("organic", {})
    total = api_metrics.get("total", {})

    total_revenue = float(total.get("sales", 0) or 0)
    total_spend = float(total.get("ad_spend", 0) or 0)
    total_net_profit = float(total.get("net_profit", 0) or 0)
    total_orders = int(total.get("order_count", 0) or 0)

    meta_revenue = float(meta.get("sales", 0) or 0)
    meta_spend = float(meta.get("ad_spend", 0) or 0)
    meta_net_roas = float(meta.get("net_roas", 0) or 0)
    meta_orders = int(meta.get("order_count", 0) or 0)

    google_revenue = float(google.get("sales", 0) or 0)
    google_spend = float(google.get("ad_spend", 0) or 0)
    google_net_roas = float(google.get("net_roas", 0) or 0)
    google_orders = int(google.get("order_count", 0) or 0)

    organic_revenue = float(organic.get("sales", 0) or 0)
    organic_orders = int(organic.get("order_count", 0) or 0)

    # --- P&L headline ---
    if total_revenue > 0:
        blended_roas = total_revenue / total_spend if total_spend else 0
        profit_sign = "profit" if total_net_profit >= 0 else "loss"
        insights.append(
            f"Today's blended ROAS is {blended_roas:.2f}x on {_fmt_inr(total_spend)} spend "
            f"generating {_fmt_inr(total_revenue)} revenue — net {profit_sign} of {_fmt_inr(abs(total_net_profit))}."
        )
    elif total_spend > 0:
        insights.append(
            f"Ad spend of {_fmt_inr(total_spend)} recorded so far today — revenue data may still be processing."
        )

    # --- Day-over-day comparison ---
    if prev_day_metrics:
        prev_total = prev_day_metrics.get("total", {})
        prev_revenue = float(prev_total.get("sales", 0) or 0)
        prev_spend = float(prev_total.get("ad_spend", 0) or 0)
        rev_chg = _pct_change(total_revenue, prev_revenue)
        spend_chg = _pct_change(total_spend, prev_spend)
        if rev_chg and spend_chg:
            insights.append(
                f"Compared to yesterday: revenue {rev_chg}, ad spend {spend_chg}."
            )

    # --- Channel split ---
    if total_revenue > 0:
        channels = []
        if meta_revenue > 0:
            meta_pct = meta_revenue / total_revenue * 100
            channels.append(f"Meta {meta_pct:.0f}%")
        if google_revenue > 0:
            google_pct = google_revenue / total_revenue * 100
            channels.append(f"Google {google_pct:.0f}%")
        if organic_revenue > 0:
            organic_pct = organic_revenue / total_revenue * 100
            channels.append(f"Organic {organic_pct:.0f}%")
        if channels:
            insights.append(f"Revenue split: {', '.join(channels)}.")

    # --- Google efficiency ---
    if google_spend > 0:
        if google_net_roas < 1:
            insights.append(
                f"Google is below breakeven — net ROAS {google_net_roas:.2f}x on {_fmt_inr(google_spend)} spend. "
                f"Review targeting or bid strategy."
            )
        elif google_net_roas > 3:
            insights.append(
                f"Google performing strongly at {google_net_roas:.2f}x net ROAS — consider scaling budget."
            )

    # --- Campaign-level insights ---
    if campaign_df is not None and not campaign_df.empty:
        df = campaign_df.copy()
        # Normalise column names: get_campaign_data() may rename shopify_orders→purchases
        # and shopify_revenue→sales; add back original names as aliases.
        if "purchases" in df.columns and "shopify_orders" not in df.columns:
            df["shopify_orders"] = df["purchases"]
        if "sales" in df.columns and "shopify_revenue" not in df.columns:
            df["shopify_revenue"] = df["sales"]

        # Ensure numeric cols
        for col in ["spend", "net_roas", "shopify_revenue", "net_profit", "shopify_orders"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        # Campaigns burning with zero ROAS
        spend_col = "spend" if "spend" in df.columns else None
        roas_col = "net_roas" if "net_roas" in df.columns else None

        if spend_col and roas_col:
            burning = df[(df[spend_col] > 500) & (df[roas_col] <= 0)]
            if not burning.empty:
                names = burning["campaign_name"].tolist() if "campaign_name" in burning.columns else []
                total_burnt = burning[spend_col].sum()
                if names:
                    truncated = names[:3]
                    extra = len(names) - 3
                    name_str = ", ".join(truncated) + (f" +{extra} more" if extra > 0 else "")
                    insights.append(
                        f"{len(names)} campaign{'s' if len(names) > 1 else ''} spent {_fmt_inr(total_burnt)} "
                        f"with 0 ROAS: {name_str}. Consider pausing or reviewing creatives."
                    )

            # Top performing campaign
            if roas_col in df.columns:
                top = df[df[spend_col] > 200].nlargest(1, roas_col)
                if not top.empty:
                    row = top.iloc[0]
                    cname = row.get("campaign_name", "Top campaign")
                    rev_col = "shopify_revenue" if "shopify_revenue" in row.index else None
                    rev_str = f" driving {_fmt_inr(row[rev_col])}" if rev_col else ""
                    insights.append(
                        f"Best campaign: {cname} at {row[roas_col]:.2f}x net ROAS{rev_str}."
                    )

            # Checkout drop-off detection (IC > 0 but purchases = 0)
            if "campaign_name" in df.columns and "_initiate_checkout" in df.columns and "shopify_orders" in df.columns:
                dropoff = df[
                    (df["_initiate_checkout"] > 0) & (df["shopify_orders"] == 0) & (df[spend_col] > 200)
                ]
                if not dropoff.empty:
                    dc_names = dropoff["campaign_name"].tolist()[:2]
                    insights.append(
                        f"Checkout drop-off detected — {', '.join(dc_names)} had "
                        f"{int(dropoff['_initiate_checkout'].sum())} checkout initiations but 0 purchases. "
                        f"Check landing page or payment flow."
                    )

    # --- Meta funnel ---
    if funnel_metrics:
        impressions = int(funnel_metrics.get("impressions", 0) or 0)
        clicks = int(funnel_metrics.get("clicks", 0) or 0)
        ctr = float(funnel_metrics.get("ctr", 0) or 0)
        atc = int(funnel_metrics.get("add_to_cart", 0) or 0)
        orders_f = int(funnel_metrics.get("orders", 0) or 0)

        if impressions > 0 and clicks > 0:
            # Flag low CTR
            if ctr < 0.8:
                insights.append(
                    f"Meta CTR is low at {ctr:.2f}% across {impressions:,} impressions — "
                    f"creatives may need refresh."
                )
            # Cart-to-purchase rate
            if atc > 0 and orders_f > 0:
                c2p = orders_f / atc * 100
                if c2p < 20:
                    insights.append(
                        f"Cart-to-purchase rate is {c2p:.0f}% ({atc} carts → {orders_f} orders). "
                        f"Checkout friction may be causing drop-off."
                    )

    # --- Organic signal ---
    if organic_orders > 0 and total_orders > 0:
        organic_share = organic_orders / total_orders * 100
        if organic_share > 30:
            insights.append(
                f"Organic channel contributed {organic_share:.0f}% of orders ({organic_orders}) "
                f"— strong brand pull independent of paid media."
            )

    return insights


def generate_wtd_insights(
    wtd_metrics: dict,
    mtd_metrics: dict,
    campaign_df: pd.DataFrame | None = None,
) -> list[str]:
    """
    Build narrative insights for the WTD/MTD report email.
    """
    insights: list[str] = []

    def _extract(m: dict) -> tuple:
        rev = float(m.get("total_sales", 0) or m.get("sales", 0) or 0)
        spend = float(m.get("total_ad_spend", 0) or m.get("ad_spend", 0) or 0)
        profit = float(m.get("net_profit", 0) or 0)
        orders = int(m.get("order_count", 0) or m.get("total_orders", 0) or 0)
        return rev, spend, profit, orders

    wtd_rev, wtd_spend, wtd_profit, wtd_orders = _extract(wtd_metrics)
    mtd_rev, mtd_spend, mtd_profit, mtd_orders = _extract(mtd_metrics)

    # WTD headline
    if wtd_rev > 0 and wtd_spend > 0:
        wtd_roas = wtd_rev / wtd_spend
        profit_sign = "profit" if wtd_profit >= 0 else "loss"
        insights.append(
            f"Week-to-date: {_fmt_inr(wtd_rev)} revenue on {_fmt_inr(wtd_spend)} spend "
            f"({wtd_roas:.2f}x blended ROAS) — net {profit_sign} of {_fmt_inr(abs(wtd_profit))}."
        )

    # MTD headline
    if mtd_rev > 0 and mtd_spend > 0:
        mtd_roas = mtd_rev / mtd_spend
        profit_sign = "profit" if mtd_profit >= 0 else "loss"
        insights.append(
            f"Month-to-date: {_fmt_inr(mtd_rev)} revenue on {_fmt_inr(mtd_spend)} spend "
            f"({mtd_roas:.2f}x blended ROAS) — net {profit_sign} of {_fmt_inr(abs(mtd_profit))}."
        )

    # CPO comparison
    if wtd_orders > 0 and wtd_spend > 0:
        cpo = wtd_spend / wtd_orders
        insights.append(f"WTD cost-per-order: {_fmt_inr(cpo)} across {wtd_orders} orders.")

    # Campaign-level insights (same logic as daily)
    if campaign_df is not None and not campaign_df.empty:
        df = campaign_df.copy()
        for col in ["spend", "net_roas"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        if "spend" in df.columns and "net_roas" in df.columns:
            burning = df[(df["spend"] > 1000) & (df["net_roas"] <= 0)]
            if not burning.empty:
                total_burnt = burning["spend"].sum()
                insights.append(
                    f"{len(burning)} campaign{'s' if len(burning) > 1 else ''} spent {_fmt_inr(total_burnt)} "
                    f"with 0 net ROAS this week — review or pause."
                )

    return insights
