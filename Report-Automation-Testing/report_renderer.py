"""
Jinja2 environment with custom filters for rendering email and PDF templates.
"""
from __future__ import annotations
import os
from pathlib import Path

import jinja2

TEMPLATES_DIR = Path(__file__).parent / "templates"

_env: jinja2.Environment | None = None


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
        """Format INR for PDF/email. Uses 'Rs.' — xhtml2pdf fonts lack the rupee glyph."""
        try:
            v = float(val)
        except Exception:
            return "Rs.0"
        neg = v < 0
        v = abs(v)
        if v >= 1_00_000:
            s = f"Rs.{v/1_00_000:.1f}L"
        elif v >= 1_000:
            s = f"Rs.{v/1_000:.1f}K"
        else:
            s = f"Rs.{v:.0f}"
        return f"-{s}" if neg else s

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
        try:
            v = float(val)
        except Exception:
            return "Rs 0.00"
        neg = v < 0
        v = abs(v)
        return f"-Rs {v:,.2f}" if neg else f"Rs {v:,.2f}"

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
    _env.filters["fmt_inr_detail"] = fmt_inr_detail
    _env.filters["fmt_roas"] = fmt_roas
    _env.filters["fmt_metric"] = fmt_metric
    _env.filters["fmt_pct"] = fmt_pct
    _env.filters["fmt_pct2"] = fmt_pct2
    _env.filters["fmt_num"] = fmt_num
    _env.filters["truncate"] = truncate_filter

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
    """Remove @page rules that xhtml2pdf can't handle."""
    import re
    # Remove @page blocks with nested rules (like @bottom-right)
    # Match @page { ... } including nested braces
    pattern = r'@page\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    return re.sub(pattern, '', html, flags=re.DOTALL)


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

    Mapping applied:
      initiate_checkout  ← checkout  (old key name)
      bounce_rate        ← drop_off_clicks_to_landing  (closest equivalent)
    """
    if not f:
        return f
    out = dict(f)
    if "initiate_checkout" not in out:
        out["initiate_checkout"] = out.get("checkout", 0)
    if "bounce_rate" not in out:
        out["bounce_rate"] = out.get("drop_off_clicks_to_landing", 0)
    return out


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
        ("Net ROAS > 1", lambda nr: nr > 1, False),
        ("0.8 < Net ROAS <= 1", lambda nr: 0.8 < nr <= 1, False),
        ("Net ROAS <= 0.8", lambda nr: nr <= 0.8, True),
    ]

    grand = _aggregate_campaign_rows(campaign_rows)
    grand_spend = grand["spend"] if grand else 0
    grand_revenue = grand["shopify_revenue"] if grand else 0

    segments: list[dict] = []
    for label, predicate, sort_asc in segment_defs:
        bucket = [r for r in campaign_rows if predicate(float(r.get("net_roas") or 0))]
        if not bucket:
            continue
        bucket.sort(key=lambda r: float(r.get("net_roas") or 0), reverse=not sort_asc)
        rows = [_decorate_campaign_row(r) for r in bucket]
        total = _aggregate_campaign_rows(bucket)
        if total:
            total["spend_pct"] = (total["spend"] / grand_spend * 100.0) if grand_spend else 0.0
            total["revenue_pct"] = (total["shopify_revenue"] / grand_revenue * 100.0) if grand_revenue else 0.0
        segments.append({"label": label, "rows": rows, "total": total})

    return segments, grand


def build_daily_pdf_context(
    api_metrics: dict,
    campaign_df,
    funnel_metrics: dict | None,
    google_funnel: dict | None,
    insights: list[str],
    report_date: str,
    report_time: str,
) -> dict:
    """
    Assemble the template context dict for the daily PDF.

    api_metrics structure: {meta:{...}, google:{...}, organic:{...}, total:{...}}
    campaign_df: pandas DataFrame from build_meta_campaigns_rollup()
    """
    import pandas as pd

    total = api_metrics.get("total", {})
    meta = api_metrics.get("meta", {})
    google = api_metrics.get("google", {})
    organic = api_metrics.get("organic", {})

    channels = [
        ("Meta", meta),
        ("Google", google),
        ("Organic", organic),
    ]

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

    return {
        "report_title": "Daily Marketing Performance Report",
        "date_range": report_date,
        "report_time": report_time,
        "total": total,
        "channels": channels,
        "funnel": _normalize_funnel(funnel_metrics),
        "google_funnel": google_funnel,
        "campaigns": campaigns,
        "campaign_segments": campaign_segments,
        "campaign_total": campaign_total,
        "insights": insights,
    }
