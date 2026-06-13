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
        try:
            v = float(val)
        except Exception:
            return "₹0"
        neg = v < 0
        v = abs(v)
        if v >= 1_00_000:
            s = f"₹{v/1_00_000:.1f}L"
        elif v >= 1_000:
            s = f"₹{v/1_000:.1f}K"
        else:
            s = f"₹{v:.0f}"
        return f"−{s}" if neg else s

    def fmt_roas(val) -> str:
        try:
            v = float(val)
        except Exception:
            return "—"
        return f"{v:.2f}x"

    def fmt_pct(val) -> str:
        try:
            v = float(val)
        except Exception:
            return "—"
        return f"{v:.1f}%"

    def fmt_num(val) -> str:
        try:
            v = int(float(val))
        except Exception:
            return "0"
        return f"{v:,}"

    def truncate_filter(s, length=38, end="…") -> str:
        s = str(s)
        if len(s) <= length:
            return s
        return s[: length - len(end)] + end

    _env.filters["fmt_inr"] = fmt_inr
    _env.filters["fmt_roas"] = fmt_roas
    _env.filters["fmt_pct"] = fmt_pct
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
        cleaned_html = _strip_page_css_for_xhtml2pdf(html)
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
                    "ctr", "bounce_rate", "conversion_rate", "shopify_orders"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df = df.sort_values("spend", ascending=False)
        campaigns = df.to_dict("records")

        rev_col = "shopify_revenue" if "shopify_revenue" in df.columns else "sales"
        ord_col = "shopify_orders" if "shopify_orders" in df.columns else "purchases"
        totals = {
            "spend": df["spend"].sum() if "spend" in df.columns else 0,
            "shopify_revenue": df[rev_col].sum() if rev_col in df.columns else 0,
            "shopify_orders": df[ord_col].sum() if ord_col in df.columns else 0,
            "net_profit": df["net_profit"].sum() if "net_profit" in df.columns else 0,
        }
        if totals["spend"]:
            totals["gross_roas"] = totals["shopify_revenue"] / totals["spend"]
            totals["net_roas"] = totals["shopify_revenue"] / totals["spend"]
        else:
            totals["gross_roas"] = 0
            totals["net_roas"] = 0
        totals["ctr"] = df["ctr"].mean() if "ctr" in df.columns else 0
        totals["bounce_rate"] = df["bounce_rate"].mean() if "bounce_rate" in df.columns else 0
        totals["conversion_rate"] = df["conversion_rate"].mean() if "conversion_rate" in df.columns else 0
        campaign_total = totals

    return {
        "report_title": "Daily Marketing Performance Report",
        "date_range": report_date,
        "report_time": report_time,
        "total": total,
        "channels": channels,
        "funnel": _normalize_funnel(funnel_metrics),
        "google_funnel": google_funnel,
        "campaigns": campaigns,
        "campaign_total": campaign_total,
        "insights": insights,
    }
