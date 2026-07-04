"""
Jinja2 environment with custom filters for rendering email and PDF templates.
"""
from __future__ import annotations
import os
from pathlib import Path

import jinja2

TEMPLATES_DIR = Path(__file__).parent / "templates"

_env: jinja2.Environment | None = None


def _indian_group(v: float) -> str:
    """Format a non-negative number with Indian digit grouping, no decimals (e.g. 1,40,289)."""
    n = int(round(v))
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    # group the head in pairs from the right
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_inr(val) -> str:
    """Module-level INR formatter (Rs. + Indian grouping) for insight text."""
    try:
        v = float(val)
    except Exception:
        return "Rs.0"
    neg = v < 0
    return f"Rs.{'-' if neg else ''}{_indian_group(abs(v))}"


def _get_env() -> jinja2.Environment:
    global _env
    if _env is not None:
        return _env

    loader = jinja2.FileSystemLoader(str(TEMPLATES_DIR))
    _env = jinja2.Environment(
        loader=loader,
        autoescape=jinja2.select_autoescape(["html"]),
        undefined=jinja2.Undefined,
    )

    # --- Custom filters ---

    def fmt_inr(val) -> str:
        """Format INR for PDF/email. Uses 'Rs.' — xhtml2pdf fonts lack the rupee glyph.

        Shows full comma-grouped numbers by default (e.g. Rs.1,40,289). Set
        PDF_ABBREVIATE_INR=true to use the compact L/K form instead.
        """
        try:
            v = float(val)
        except Exception:
            return "Rs.0"
        neg = v < 0
        v = abs(v)
        if os.getenv("PDF_ABBREVIATE_INR", "false").lower() in ("1", "true", "yes"):
            if v >= 1_00_000:
                s = f"Rs.{v/1_00_000:.1f}L"
            elif v >= 1_000:
                s = f"Rs.{v/1_000:.1f}K"
            else:
                s = f"Rs.{v:.0f}"
        else:
            # Full (untrimmed) number, Indian grouping
            s = f"Rs.{_indian_group(v)}"
        if neg:
            suffix = s[3:] if s.startswith("Rs.") else s
            return f"Rs.-{suffix}"
        return s

    def fmt_inr_compact(val) -> str:
        """Compact INR (Rs.20.4L / Rs.2.9L / Rs.4.2Cr / Rs.590) for dense tables."""
        try:
            v = float(val)
        except Exception:
            return "Rs.0"
        neg = v < 0
        v = abs(v)
        if v >= 1_00_00_000:
            s = f"Rs.{v/1_00_00_000:.2f}Cr"
        elif v >= 1_00_000:
            s = f"Rs.{v/1_00_000:.1f}L"
        elif v >= 1_000:
            s = f"Rs.{v/1_000:.1f}K"
        else:
            s = f"Rs.{v:.0f}"
        return f"Rs.-{s[3:]}" if neg else s

    def fmt_roas(val) -> str:
        try:
            v = float(val)
        except Exception:
            return "-"
        return f"{v:.2f}x"

    def fmt_pct(val) -> str:
        try:
            v = float(val)
        except Exception:
            return "-"
        return f"{v:.1f}%"

    def fmt_pct2(val) -> str:
        try:
            v = float(val)
        except Exception:
            return "-"
        return f"{v:.2f}%"

    def fmt_inr_detail(val) -> str:
        """Compact INR with decimals; no space after Rs. to avoid PDF line-wrap."""
        try:
            v = float(val)
        except Exception:
            return "Rs.0.00"
        neg = v < 0
        v = abs(v)
        s = f"Rs.{v:,.2f}"
        if neg:
            return f"Rs.-{v:,.2f}"
        return s

    def fmt_metric(val) -> str:
        try:
            v = float(val)
        except Exception:
            return "-"
        return f"{v:.2f}"

    def fmt_num(val) -> str:
        try:
            v = int(float(val))
        except Exception:
            return "0"
        return f"{v:,}"

    def truncate_filter(s, length=38, end="...") -> str:
        s = str(s)
        if len(s) <= length:
            return s
        return s[: length - len(end)] + end

    _env.filters["fmt_inr"] = fmt_inr
    _env.filters["fmt_inr_compact"] = fmt_inr_compact
    _env.filters["fmt_inr_detail"] = fmt_inr_detail
    _env.filters["fmt_roas"] = fmt_roas
    _env.filters["fmt_metric"] = fmt_metric
    _env.filters["fmt_pct"] = fmt_pct
    _env.filters["fmt_pct2"] = fmt_pct2
    _env.filters["fmt_num"] = fmt_num
    _env.filters["truncate"] = truncate_filter
    _env.filters["roas_grad"] = _roas_grad_class

    return _env


def render_email_daily(context: dict) -> str:
    """Render the daily email HTML from templates/email_daily.html."""
    tmpl = _get_env().get_template("email_daily.html")
    return tmpl.render(**context)


def render_email_wtd_mtd(context: dict) -> str:
    """Render the WTD/MTD email HTML from templates/email_wtd_mtd.html."""
    tmpl = _get_env().get_template("email_wtd_mtd.html")
    return tmpl.render(**context)


def render_pdf_html(context: dict) -> str:
    """Render the PDF HTML from templates/pdf_report.html."""
    tmpl = _get_env().get_template("pdf_report.html")
    return tmpl.render(**context)


def _strip_page_css_for_xhtml2pdf(html: str) -> str:
    """Adapt @page CSS for xhtml2pdf.

    xhtml2pdf understands ``@page`` and its own ``@frame`` static frames (used here
    for the repeating footer + page numbers), but chokes on the CSS Paged Media
    margin-box at-rules that weasyprint uses (``@top-*`` / ``@bottom-*`` / ``@left-*``
    / ``@right-*``). Strip only those margin boxes and leave ``@page``/``@frame`` intact.
    """
    import re
    return re.sub(
        r'@(?:top|bottom|left|right)-\w+\s*\{[^{}]*\}',
        '',
        html,
        flags=re.DOTALL,
    )


def _sanitize_for_xhtml2pdf(html: str) -> str:
    """Replace glyphs missing from Helvetica/ReportLab built-in fonts."""
    return (
        html.replace("\u20b9", "Rs.")
        .replace("\u2212", "-")
        .replace("\u2014", " - ")
        .replace("\u2013", "-")
        .replace("\u2026", "...")
        .replace("\u26a0", "")
        .replace("\u2713", "")
        .replace("\ufe0f", "")
    )


def html_to_pdf(html: str, output_path: str) -> str:
    """Convert rendered HTML to PDF. Tries weasyprint first, falls back to xhtml2pdf."""
    errors = []

    # Try weasyprint first (better CSS support)
    try:
        from weasyprint import HTML as WPHtml
        WPHtml(string=html).write_pdf(output_path)
        return output_path
    except ImportError as e:
        errors.append(f"weasyprint: ImportError - {e}")
    except OSError as e:
        errors.append(f"weasyprint: OSError (likely missing GTK) - {e}")
    except Exception as e:
        errors.append(f"weasyprint: {type(e).__name__} - {e}")

    # Try pdfkit (requires wkhtmltopdf installed)
    try:
        import pdfkit
        pdfkit.from_string(html, output_path)
        return output_path
    except ImportError as e:
        errors.append(f"pdfkit: ImportError - {e}")
    except OSError as e:
        errors.append(f"pdfkit: OSError (wkhtmltopdf not found) - {e}")
    except Exception as e:
        errors.append(f"pdfkit: {type(e).__name__} - {e}")

    # Fallback to xhtml2pdf (pure Python, no external dependencies)
    # Preprocess HTML to remove unsupported @page CSS rules
    try:
        from xhtml2pdf import pisa
        cleaned_html = _sanitize_for_xhtml2pdf(_strip_page_css_for_xhtml2pdf(html))
        with open(output_path, "wb") as pdf_file:
            pisa_status = pisa.CreatePDF(cleaned_html, dest=pdf_file)
            if pisa_status.err:
                raise RuntimeError(f"xhtml2pdf conversion error: {pisa_status.err}")
        return output_path
    except ImportError as e:
        errors.append(f"xhtml2pdf: ImportError - {e}")
    except Exception as e:
        errors.append(f"xhtml2pdf: {type(e).__name__} - {e}")

    # Last resort: write HTML fallback
    html_path = output_path.replace(".pdf", "_fallback.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    error_details = "\n  ".join(errors) if errors else "No libraries tried"
    raise RuntimeError(
        f"No PDF library available. HTML written to {html_path}.\n"
        f"Errors encountered:\n  {error_details}"
    )


def _normalize_funnel(f: dict | None) -> dict | None:
    """
    Normalise the funnel dict returned by get_meta_funnel_metrics() so template
    variable names match regardless of which key the source function uses.
    """
    if not f:
        return f
    out = dict(f)
    if "initiate_checkout" not in out:
        out["initiate_checkout"] = out.get("checkout", 0)
    if "checkout" not in out:
        out["checkout"] = out.get("initiate_checkout", 0)
    if "bounce_rate" not in out:
        out["bounce_rate"] = out.get("drop_off_clicks_to_landing", 0)

    impressions = float(out.get("impressions") or 0)
    clicks = float(out.get("clicks") or 0)
    landing = float(out.get("landing_page_views") or 0)
    atc = float(out.get("add_to_cart") or 0)
    checkout = float(out.get("checkout") or out.get("initiate_checkout") or 0)
    orders = float(out.get("orders") or 0)

    def _rate(n, d):
        return round((n / d) * 100.0, 2) if d > 0 else None

    if out.get("checkout_rate") is None:
        out["checkout_rate"] = _rate(checkout, atc)
    if out.get("drop_off_cart_to_checkout") is None and atc > 0:
        out["drop_off_cart_to_checkout"] = _rate(atc - checkout, atc)
    if out.get("drop_off_checkout_to_orders") is None and checkout > 0:
        out["drop_off_checkout_to_orders"] = _rate(checkout - orders, checkout)
    if out.get("landing_page_rate") is None:
        out["landing_page_rate"] = _rate(landing, clicks)
    if out.get("add_to_cart_rate") is None:
        out["add_to_cart_rate"] = _rate(atc, landing)
    if out.get("ctr") is None:
        out["ctr"] = _rate(clicks, impressions)
    if out.get("conversion_rate") is None:
        out["conversion_rate"] = _rate(orders, clicks)
    if out.get("drop_off_impressions_to_clicks") is None and impressions > 0:
        out["drop_off_impressions_to_clicks"] = _rate(impressions - clicks, impressions)
    if out.get("drop_off_clicks_to_landing") is None and clicks > 0:
        out["drop_off_clicks_to_landing"] = _rate(clicks - landing, clicks)
    if out.get("drop_off_landing_to_cart") is None and landing > 0:
        out["drop_off_landing_to_cart"] = _rate(landing - atc, landing)
    if out.get("drop_off_cart_to_orders") is None and atc > 0:
        out["drop_off_cart_to_orders"] = _rate(atc - orders, atc)
    return out


def _normalize_google_funnel(f: dict | None) -> dict | None:
    """Normalise Google funnel metrics for the PDF card."""
    if not f:
        return f
    out = dict(f)
    impressions = float(out.get("impressions") or 0)
    clicks = float(out.get("clicks") or 0)
    orders = float(out.get("orders") or 0)

    def _rate(n, d):
        return round((n / d) * 100.0, 2) if d > 0 else None

    if out.get("ctr") is None:
        out["ctr"] = _rate(clicks, impressions)
    if out.get("interaction_rate") is None:
        out["interaction_rate"] = out.get("ctr")
    if out.get("conversion_rate") is None:
        out["conversion_rate"] = _rate(orders, clicks)
    if out.get("drop_off_impressions_to_clicks") is None and impressions > 0:
        out["drop_off_impressions_to_clicks"] = _rate(impressions - clicks, impressions)
    if out.get("drop_off_clicks_to_orders") is None and clicks > 0:
        out["drop_off_clicks_to_orders"] = _rate(clicks - orders, clicks)
    return out


def _funnel_row(
    stage: str,
    count=None,
    *,
    rate=None,
    rate_prefix: str | None = None,
    drop_off=None,
) -> dict:
    """Build one funnel table row for Jinja (None/0 count still shown when explicitly 0)."""
    has_count = count is not None
    has_rate = rate is not None
    has_drop_off = drop_off is not None
    return {
        "stage": stage,
        "count": count,
        "has_count": has_count,
        "rate": rate,
        "rate_prefix": rate_prefix,
        "has_rate": has_rate,
        "drop_off": drop_off,
        "has_drop_off": has_drop_off,
    }


def build_meta_funnel_rows(funnel: dict | None) -> list[dict]:
    f = _normalize_funnel(funnel) or {}
    return [
        _funnel_row("Impressions", f.get("impressions")),
        _funnel_row(
            "Clicks",
            f.get("clicks"),
            rate=f.get("ctr"),
            rate_prefix="CTR",
            drop_off=f.get("drop_off_impressions_to_clicks"),
        ),
        _funnel_row(
            "LP Views",
            f.get("landing_page_views"),
            rate=f.get("landing_page_rate"),
            drop_off=f.get("drop_off_clicks_to_landing"),
        ),
        _funnel_row(
            "Add to Cart",
            f.get("add_to_cart"),
            rate=f.get("add_to_cart_rate"),
            drop_off=f.get("drop_off_landing_to_cart"),
        ),
        _funnel_row(
            "Checkout",
            f.get("initiate_checkout"),
            rate=f.get("checkout_rate"),
            drop_off=f.get("drop_off_cart_to_checkout"),
        ),
        _funnel_row(
            "Orders",
            f.get("orders"),
            rate=f.get("conversion_rate"),
            rate_prefix="CVR",
            drop_off=f.get("drop_off_cart_to_orders"),
        ),
    ]


def build_google_funnel_rows(funnel: dict | None) -> list[dict]:
    """Google Ads funnel; LP/ATC/checkout rows align with Meta card layout.

    When the on-site session funnel is available (GET /v1/funnel?channel=google via
    channel_funnel_from_api), the LP Views / Add to Cart / Checkout stages carry real
    counts. Otherwise (ad-delivery-only sources) those rows render blank as before.
    """
    g = _normalize_google_funnel(funnel) or {}
    has_onsite = g.get("landing_page_views") is not None or g.get("add_to_cart") is not None
    if has_onsite:
        lp_rate = g.get("landing_page_rate")
        lp_prefix = None
    else:
        lp_rate = g.get("interaction_rate")
        lp_prefix = "Int"
    return [
        _funnel_row("Impressions", g.get("impressions")),
        _funnel_row(
            "Clicks",
            g.get("clicks"),
            rate=g.get("ctr"),
            rate_prefix="CTR",
            drop_off=g.get("drop_off_impressions_to_clicks"),
        ),
        _funnel_row(
            "LP Views",
            g.get("landing_page_views"),
            rate=lp_rate,
            rate_prefix=lp_prefix,
            drop_off=g.get("drop_off_clicks_to_landing"),
        ),
        _funnel_row(
            "Add to Cart",
            g.get("add_to_cart"),
            rate=g.get("add_to_cart_rate"),
            drop_off=g.get("drop_off_landing_to_cart"),
        ),
        _funnel_row(
            "Checkout",
            g.get("checkout"),
            rate=g.get("checkout_rate"),
            drop_off=g.get("drop_off_cart_to_checkout"),
        ),
        _funnel_row(
            "Orders",
            g.get("orders"),
            rate=g.get("conversion_rate"),
            rate_prefix="CVR",
            drop_off=g.get("drop_off_cart_to_orders") or g.get("drop_off_clicks_to_orders"),
        ),
    ]


def _visible_funnel_rows(rows: list[dict]) -> list[dict]:
    """Hide funnel cards when every stage count is empty/zero."""
    if not rows:
        return []
    if any(r.get("has_count") and float(r.get("count") or 0) > 0 for r in rows):
        return rows
    return []


def _bounce_bg(bounce: float) -> str:
    """Light-blue heatmap cell background; 0% bounce stays white."""
    try:
        v = float(bounce)
    except Exception:
        return "#FFFFFF"
    if v <= 0:
        return "#FFFFFF"
    ratio = min(v / 100.0, 1.0)
    r = int(255 - (255 - 217) * ratio)
    g = int(255 - (255 - 234) * ratio)
    b = int(255 - (255 - 251) * ratio)
    return f"#{r:02x}{g:02x}{b:02x}"


def _campaign_revenue(row: dict) -> float:
    return float(row.get("shopify_revenue") or row.get("sales") or 0)


def _aggregate_campaign_rows(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    spend = sum(float(r.get("spend") or 0) for r in rows)
    revenue = sum(_campaign_revenue(r) for r in rows)
    cogs = sum(float(r.get("cogs") or 0) for r in rows)
    net_profit = sum(float(r.get("net_profit") or 0) for r in rows)
    clicks = sum(float(r.get("clicks") or 0) for r in rows)
    impressions = sum(float(r.get("impressions") or 0) for r in rows)
    orders = sum(float(r.get("shopify_orders") or r.get("purchases") or 0) for r in rows)

    if impressions > 0:
        ctr = clicks / impressions * 100.0
    else:
        ctr = sum(float(r.get("ctr") or 0) for r in rows) / len(rows)

    if clicks > 0:
        bounce_rate = sum(float(r.get("bounce_rate") or 0) * float(r.get("clicks") or 0) for r in rows) / clicks
        conversion_rate = orders / clicks * 100.0
    else:
        bounce_rate = sum(float(r.get("bounce_rate") or 0) for r in rows) / len(rows)
        conversion_rate = sum(float(r.get("conversion_rate") or 0) for r in rows) / len(rows)

    gross_roas = revenue / spend if spend else 0.0
    net_roas = (revenue - cogs) / spend if spend else 0.0

    return {
        "spend": spend,
        "shopify_revenue": revenue,
        "net_profit": net_profit,
        "ctr": ctr,
        "bounce_rate": bounce_rate,
        "conversion_rate": conversion_rate,
        "gross_roas": gross_roas,
        "net_roas": net_roas,
    }


def _decorate_campaign_row(row: dict) -> dict:
    out = dict(row)
    bounce = float(out.get("bounce_rate") or 0)
    net_roas = float(out.get("net_roas") or 0)
    out["bounce_bg"] = _bounce_bg(bounce)
    out["nr_low"] = net_roas < 1
    out["roas_grad"] = _roas_grad_class(net_roas)
    if net_roas >= 1.0:
        out["roas_tone"] = "pos"
    elif net_roas >= 0.8:
        out["roas_tone"] = "warn"
    else:
        out["roas_tone"] = "neg"
    out["orders"] = int(float(out.get("shopify_orders") or out.get("purchases") or 0))
    return out


def build_campaign_roas_segments(campaign_rows: list[dict]) -> tuple[list[dict], dict | None]:
    """
    Group campaigns into Net ROAS segments for the PDF marketing summary.

    Segments (only non-empty segments are returned):
      - Net ROAS > 1
      - 0.8 < Net ROAS <= 1
      - Net ROAS <= 0.8
    """
    if not campaign_rows:
        return [], None

    segment_defs = [
        # (internal label, tier, display label, sub, predicate, sort_ascending)
        ("Net ROAS > 1", "high", "High Performing", "Net ROAS > 1.0x",
         lambda nr: nr > 1, False),
        ("0.8 < Net ROAS <= 1", "average", "Average Performance", "Net ROAS 0.8x – 1.0x",
         lambda nr: 0.8 < nr <= 1, False),
        ("Net ROAS <= 0.8", "low", "Underperforming", "Net ROAS < 0.8x",
         lambda nr: nr <= 0.8, True),
    ]

    grand = _aggregate_campaign_rows(campaign_rows)
    grand_spend = grand["spend"] if grand else 0
    grand_revenue = grand["shopify_revenue"] if grand else 0

    segments: list[dict] = []
    for label, tier, display_label, sub, predicate, sort_asc in segment_defs:
        bucket = [r for r in campaign_rows if predicate(float(r.get("net_roas") or 0))]
        if not bucket:
            continue
        bucket.sort(key=lambda r: float(r.get("net_roas") or 0), reverse=not sort_asc)
        rows = [_decorate_campaign_row(r) for r in bucket]
        total = _aggregate_campaign_rows(bucket)
        if total:
            total["spend_pct"] = (total["spend"] / grand_spend * 100.0) if grand_spend else 0.0
            total["revenue_pct"] = (total["shopify_revenue"] / grand_revenue * 100.0) if grand_revenue else 0.0
        segments.append({
            "label": label,
            "tier": tier,
            "display_label": display_label,
            "sub": sub,
            "count": len(rows),
            "rows": rows,
            "total": total,
        })

    return segments, grand


def build_returns_cancels_summary(total: dict) -> dict:
    """Dashboard-aligned returns/cancels counts and amounts for the PDF summary."""
    count = int(total.get("returns_cancels") or 0)
    cancelled_orders = int(total.get("cancelled_orders") or 0)
    returned_orders = int(total.get("returned_orders") or 0)
    cancelled_amount = round(float(total.get("cancelled_amount") or 0), 2)
    returned_amount = round(float(total.get("returned_amount") or 0), 2)
    total_amount = round(float(total.get("returns_cancels_amount") or 0), 2)
    if total_amount <= 0 and (cancelled_amount > 0 or returned_amount > 0):
        total_amount = round(cancelled_amount + returned_amount, 2)
    return {
        "show": count > 0 or cancelled_orders > 0 or returned_orders > 0,
        "total_count": count or (cancelled_orders + returned_orders),
        "cancelled_orders": cancelled_orders,
        "returned_orders": returned_orders,
        "cancelled_amount": cancelled_amount,
        "returned_amount": returned_amount,
        "total_amount": total_amount or round(cancelled_amount + returned_amount, 2),
        "gross_sales": round(float(total.get("gross_sales") or 0), 2),
    }


def build_returns_row(total: dict, channels: list[dict] | None = None, returns_cancels_count: int = 0) -> dict:
    """
    Channel Performance "Returned / Cancelled" row — mirrors the General Statistics
    dashboard returns/cancels counts and ex-GST amounts (event-date axis).

    ``channels`` is accepted for backward compatibility but no longer used; the Total
    row remains the dashboard headline and is not derived from summing channel rows.
    """
    _ = channels  # unused; kept for call-site compatibility
    summary = build_returns_cancels_summary(total)
    amount = summary["total_amount"]
    row = {
        "sales": amount,
        "ad_spend": 0.0,
        "cogs": 0.0,
        "net_profit": amount,
        "order_count": summary["total_count"],
        "cancelled_orders": summary["cancelled_orders"],
        "returned_orders": summary["returned_orders"],
        "cancelled_amount": summary["cancelled_amount"],
        "returned_amount": summary["returned_amount"],
        "total_amount": amount,
        "show": summary["show"],
    }
    if not row["show"] and int(returns_cancels_count or 0) > 0:
        row["show"] = True
    return row


def _pct(n, d) -> int:
    """Integer percentage (0..100) used for bar-cell widths."""
    try:
        n = float(n or 0)
        d = float(d or 0)
    except Exception:
        return 0
    if d <= 0:
        return 0
    return int(round(min(max(n / d, 0.0), 1.0) * 100))


def _roas_grad_class(net_roas: float) -> str:
    """CSS class for Net ROAS cell background gradient tier."""
    try:
        nr = float(net_roas or 0)
    except Exception:
        return "roas-grad-low"
    if nr >= 1.0:
        return "roas-grad-high"
    if nr >= 0.8:
        return "roas-grad-mid"
    return "roas-grad-low"


def _augment_funnel_rows(rows: list[dict]) -> list[dict]:
    """Flag the biggest drop-off stage in each funnel table."""
    if not rows:
        return rows
    max_drop = None
    for r in rows:
        if r.get("has_drop_off") and r.get("drop_off") is not None:
            d = float(r.get("drop_off") or 0)
            if max_drop is None or d > max_drop:
                max_drop = d
    for r in rows:
        r["is_max_drop"] = (
            r.get("has_drop_off")
            and max_drop is not None
            and abs(float(r.get("drop_off") or 0) - max_drop) < 1e-9
            and max_drop > 0
        )
    return rows


def _build_channel_viz(channels: list[tuple[str, dict]]) -> dict:
    """Precompute bar widths for the Visual Analytics charts.

    Returns dicts of per-channel rows scaled to the max in each series, plus a
    signed 'half' width (0..50) for the centre-axis diverging Net Profit chart.
    """
    items = [(name, ch) for name, ch in channels if ch]
    if not items:
        return {}
    rev_max = max((float(c.get("sales") or 0) for _, c in items), default=0)
    spend_max = max((float(c.get("ad_spend") or 0) for _, c in items), default=0)
    profit_absmax = max((abs(float(c.get("net_profit") or 0)) for _, c in items), default=0)
    roas_items = [(n, c) for n, c in items if float(c.get("ad_spend") or 0) > 0]
    roas_max = max((float(c.get("net_roas") or 0) for _, c in roas_items), default=0)
    # cap the scale so a single high-ROAS channel doesn't crush the other bars;
    # values above the cap simply render as a full bar
    roas_scale = max(1.0, min(roas_max, 4.0))

    rev_spend = [
        {
            "name": n,
            "revenue": float(c.get("sales") or 0),
            "spend": float(c.get("ad_spend") or 0),
            "rev_w": _pct(c.get("sales") or 0, rev_max),
            "spend_w": _pct(c.get("ad_spend") or 0, spend_max),
        }
        for n, c in items
    ]
    profit = [
        {
            "name": n,
            "net_profit": float(c.get("net_profit") or 0),
            "w": _pct(abs(float(c.get("net_profit") or 0)), profit_absmax),
            "half": _pct(abs(float(c.get("net_profit") or 0)), profit_absmax) // 2,
            "neg": float(c.get("net_profit") or 0) < 0,
        }
        for n, c in items
    ]
    profit_has_neg = any(p["neg"] for p in profit)
    roas = [
        {
            "name": n,
            "net_roas": float(c.get("net_roas") or 0),
            "roas_w": _pct(c.get("net_roas") or 0, roas_scale),
            "pos": float(c.get("net_roas") or 0) >= 1.0,
        }
        for n, c in roas_items
    ]
    # Breakeven line position on the ROAS chart (1.0x as a share of the scale).
    be_w = _pct(1.0, roas_scale)
    return {
        "rev_spend": rev_spend,
        "profit": profit,
        "profit_has_neg": profit_has_neg,
        "roas": roas,
        "roas_breakeven_w": be_w,
    }


def _build_top_campaigns(campaign_rows: list[dict], n: int = 10) -> list[dict]:
    """Top campaigns by net profit with centre-axis diverging bar widths."""
    if not campaign_rows:
        return []
    rows = sorted(campaign_rows, key=lambda r: float(r.get("net_profit") or 0), reverse=True)[:n]
    absmax = max((abs(float(r.get("net_profit") or 0)) for r in rows), default=0)
    out = []
    for r in rows:
        np_ = float(r.get("net_profit") or 0)
        out.append({
            "campaign_name": r.get("campaign_name") or "",
            "net_profit": np_,
            "net_roas": float(r.get("net_roas") or 0),
            "w": _pct(abs(np_), absmax),
            "half": _pct(abs(np_), absmax) // 2,
            "neg": np_ < 0,
        })
    return out


def build_executive_insights(
    channels: list[tuple[str, dict]],
    campaign_rows: list[dict],
    meta_rows: list[dict],
    google_rows: list[dict],
    total: dict,
) -> list[dict]:
    """Auto-generate concise, executive-level takeaways from the report data.

    Each insight: {tone: good|bad|warn|info, title, text}.
    """
    insights: list[dict] = []

    real = [(n, c) for n, c in channels if c]
    if real:
        top_rev = max(real, key=lambda kv: float(kv[1].get("sales") or 0))
        rev = float(top_rev[1].get("sales") or 0)
        share = (rev / float(total.get("sales") or 1) * 100.0) if total.get("sales") else 0
        insights.append({
            "tone": "good",
            "title": "Top revenue channel",
            "text": f"{top_rev[0]} drove {fmt_inr(rev)} ({share:.0f}% of revenue).",
        })

    if campaign_rows:
        best = max(campaign_rows, key=lambda r: float(r.get("net_profit") or 0))
        if float(best.get("net_profit") or 0) > 0:
            insights.append({
                "tone": "good",
                "title": "Most profitable campaign",
                "text": f"“{_short(best.get('campaign_name'))}” netted "
                        f"{fmt_inr(best.get('net_profit'))} at "
                        f"{float(best.get('net_roas') or 0):.2f}x Net ROAS.",
            })
        worst = min(
            (r for r in campaign_rows if float(r.get("spend") or 0) > 0),
            key=lambda r: float(r.get("net_roas") or 0),
            default=None,
        )
        if worst is not None and float(worst.get("net_roas") or 0) < 0.8:
            insights.append({
                "tone": "bad",
                "title": "Lowest-performing campaign",
                "text": f"“{_short(worst.get('campaign_name'))}” returned only "
                        f"{float(worst.get('net_roas') or 0):.2f}x Net ROAS on "
                        f"{fmt_inr(worst.get('spend'))} spend.",
            })

    drop = _largest_dropoff(meta_rows, "Meta") or _largest_dropoff(google_rows, "Google")
    if drop:
        insights.append({
            "tone": "warn",
            "title": "Largest funnel drop-off",
            "text": f"{drop['channel']} loses {drop['drop']:.0f}% between "
                    f"{drop['from']} and {drop['to']} — the biggest leak.",
        })

    spenders = [(n, c) for n, c in real if float(c.get("ad_spend") or 0) > 0
                and float(c.get("order_count") or 0) > 0]
    if spenders:
        hi_cpo = max(spenders, key=lambda kv: float(kv[1].get("cpp") or 0))
        insights.append({
            "tone": "info",
            "title": "Highest cost per order",
            "text": f"{hi_cpo[0]} at {fmt_inr(hi_cpo[1].get('cpp'))} per order.",
        })

    # Optimization opportunity: budget tied up in sub-breakeven campaigns.
    under = [r for r in campaign_rows if float(r.get("net_roas") or 0) < 0.8
             and float(r.get("spend") or 0) > 0]
    if under:
        under_spend = sum(float(r.get("spend") or 0) for r in under)
        tot_spend = sum(float(r.get("spend") or 0) for r in campaign_rows) or 1
        insights.append({
            "tone": "warn",
            "title": "Optimization opportunity",
            "text": f"{fmt_inr(under_spend)} ({under_spend / tot_spend * 100:.0f}% of "
                    f"campaign spend) sits in {len(under)} sub-breakeven "
                    f"campaign{'s' if len(under) != 1 else ''} — reallocate or pause.",
        })

    return insights


def _short(name, length: int = 34) -> str:
    s = str(name or "")
    return s if len(s) <= length else s[: length - 1] + "…"


def _largest_dropoff(rows: list[dict], channel: str) -> dict | None:
    if not rows:
        return None
    best = None
    for i, r in enumerate(rows):
        if r.get("has_drop_off") and r.get("drop_off") is not None:
            d = float(r.get("drop_off") or 0)
            prev = rows[i - 1]["stage"] if i > 0 else r["stage"]
            if best is None or d > best["drop"]:
                best = {"channel": channel, "drop": d, "from": prev, "to": r["stage"]}
    return best


def build_daily_pdf_context(
    api_metrics: dict,
    campaign_df,
    funnel_metrics: dict | None,
    google_funnel: dict | None,
    report_date: str,
    report_time: str,
    roas_reconciliation: dict | None = None,
) -> dict:
    """
    Assemble the template context dict for the daily PDF.

    api_metrics structure: {meta:{...}, google:{...}, organic:{...}, total:{...}}
    campaign_df: pandas DataFrame from build_meta_campaigns_rollup()
    """
    import pandas as pd

    def _norm_channel(c: dict | None) -> dict:
        """Fill missing numeric keys with 0 so templates never hit Undefined math."""
        c = dict(c or {})
        for k in ("sales", "ad_spend", "cogs", "net_profit", "gross_roas",
                  "net_roas", "be_roas", "order_count", "cpp"):
            if c.get(k) is None:
                c[k] = 0
        return c

    total = api_metrics.get("total", {})
    meta = _norm_channel(api_metrics.get("meta"))
    google = _norm_channel(api_metrics.get("google"))
    organic = _norm_channel(api_metrics.get("organic"))
    amazon = api_metrics.get("amazon")

    channels = [
        ("Meta", meta),
        ("Google", google),
        ("Organic", organic),
    ]
    # Amazon is a separate marketplace; include it as a channel row only when the
    # source provides it (dashboard_stats). The legacy hourly source omits it.
    if amazon:
        channels.append(("Amazon", _norm_channel(amazon)))

    campaigns = []
    campaign_segments = []
    campaign_total = None
    if campaign_df is not None and not campaign_df.empty:
        df = campaign_df.copy()

        # Normalise column names: get_campaign_data() renames shopify_orders→purchases
        # and shopify_revenue→sales; add back the original names as aliases so templates
        # that reference either name will work.
        # Ensure columns always exist (default to 0) to prevent template errors.
        if "shopify_orders" not in df.columns:
            if "purchases" in df.columns:
                df["shopify_orders"] = df["purchases"]
            else:
                df["shopify_orders"] = 0
        if "shopify_revenue" not in df.columns:
            if "sales" in df.columns:
                df["shopify_revenue"] = df["sales"]
            else:
                df["shopify_revenue"] = 0

        for col in ["spend", "shopify_revenue", "net_profit", "net_roas", "gross_roas",
                    "ctr", "bounce_rate", "conversion_rate", "shopify_orders", "cogs", "clicks", "impressions"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        campaign_rows = df.to_dict("records")
        campaign_segments, campaign_total = build_campaign_roas_segments(campaign_rows)
        campaigns = campaign_rows

    # Reconciling "Returned / Cancelled" row so the channel rows + this row sum to Total.
    returns_row = build_returns_row(
        total,
        [c for _, c in channels],
        returns_cancels_count=int(total.get("returns_cancels") or 0),
    )

    meta_funnel_rows = _augment_funnel_rows(
        _visible_funnel_rows(build_meta_funnel_rows(funnel_metrics))
    )
    google_funnel_rows = _augment_funnel_rows(
        _visible_funnel_rows(build_google_funnel_rows(google_funnel))
    )

    # Gross profit convenience field + zero-defaults so KPI cards never hit Undefined.
    total = dict(total)
    for k in ("sales", "ad_spend", "cogs", "net_profit", "gross_roas",
              "net_roas", "be_roas", "order_count", "cpp"):
        if total.get(k) is None:
            total[k] = 0
    total["gross_profit"] = float(total.get("sales") or 0) - float(total.get("cogs") or 0)

    # Best channel (by net profit) for subtle row highlighting in the channel table.
    best_channel_name = None
    _real = [(n, c) for n, c in channels if c]
    if _real:
        best_channel_name = max(_real, key=lambda kv: float(kv[1].get("net_profit") or 0))[0]

    return {
        "report_title": "Daily Marketing Performance Report",
        "date_range": report_date,
        "report_time": report_time,
        "total": total,
        "channels": channels,
        "best_channel_name": best_channel_name,
        "returns_row": returns_row,
        "funnel": _normalize_funnel(funnel_metrics),
        "google_funnel": _normalize_google_funnel(google_funnel),
        "meta_funnel_rows": meta_funnel_rows,
        "google_funnel_rows": google_funnel_rows,
        "campaigns": campaigns,
        "campaign_segments": campaign_segments,
        "campaign_total": campaign_total,
        "roas_reconciliation": roas_reconciliation,
    }
