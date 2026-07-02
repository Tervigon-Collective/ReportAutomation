"""
Clean, attribution-sourced Entity Report.

This is a self-contained rebuild of the legacy entity report (the
`meta_ads_rollup` / `meta_campaigns` / `google_campaigns` / `organic_campaigns`
workbook produced by ``dailyrollup.py``). Unlike the legacy path it is sourced
**exclusively** from the Node-Backend v1 attribution APIs — which read the
ClickHouse ``gold.fct_*`` tables the dashboard and P&L use — so every value
reconciles with the dashboard "to the rupee".

Canonical rules applied (see ``docs/data_sources_cannonical.md`` §0, §5, §7):
  * No PostgreSQL ``dw_*_attribution`` fallback. API-only; raise on failure.
  * Rate metrics (CTR / CPC / ROAS) are **weighted** — derived from summed
    base metrics, never averaged per row (fixes the unweighted-CTR bug #4).
  * Every ``brand_id``-scoped call goes through the shared v1 client.

Grains (per product decision):
  * Meta    -> ad level      (campaign -> adset -> ad)
  * Google  -> campaign level
  * Organic -> utm-campaign level
  * Amazon  -> reused as-is from ``amazon_entity_report.py``

Entry points:
  * ``add_entity_sheets(writer, start, end, ...)`` — append sheets to an open
    ``pd.ExcelWriter`` (Meta / Google / Organic / Amazon + reconciliation).
  * ``build_entity_report(start, end, out_dir)`` — standalone workbook runner.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd

from api_data_fetcher import (
    fetch_marketing_from_api,
    fetch_meta_funnel,
    fetch_pnl_summary,
    get_api_brand_id,
)
from metric_calculators import (
    compute_be_roas,
    compute_ctr,
    compute_net_profit,
    compute_net_roas,
    compute_roas,
    safe_div,
)

logger = logging.getLogger(__name__)

GRAND_TOTAL_LABEL = "Grand Total"

# Base additive metrics carried through every channel rollup. Rates are always
# derived from these sums (never summed / averaged directly).
_ADDITIVE_COLS = (
    "impressions",
    "clicks",
    "spend",
    "shopify_orders",
    "shopify_revenue",
    "shopify_cogs",
    "total_sku_quantity",
)

# Map the flattened attribution columns onto the report's metric names. Mirrors
# dailyrollup.transform_attribution_data but kept local to avoid a circular
# import (dailyrollup wires this module in).
_COLUMN_MAP = {
    "spend_cost": "spend",
    "attributed_orders_count": "shopify_orders",
    "attributed_orders_revenue": "shopify_revenue",
    "attributed_orders_cogs": "shopify_cogs",
    "attributed_orders_quantity": "total_sku_quantity",
}


# ---------------------------------------------------------------------------
# Fetch (API-only, no PostgreSQL fallback)
# ---------------------------------------------------------------------------
def fetch_entity_attribution(start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch the flattened Meta+Google+Organic attribution rows from the clean
    v1 APIs. Raises on failure — this report never reads the deprecated
    ``dw_*_attribution`` Postgres tables.
    """
    df = fetch_marketing_from_api(start_date, end_date)
    if df is None or df.empty:
        raise RuntimeError(
            f"No attribution data returned by v1 APIs for {start_date}..{end_date}. "
            "Check BACKEND_API_BASE_URL / JWT credentials; this report will not "
            "fall back to PostgreSQL."
        )
    return _normalize_marketing_df(df)


def _normalize_marketing_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for src, dst in _COLUMN_MAP.items():
        if src in out.columns:
            out[dst] = out[src]
    for col in _ADDITIVE_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    # NOTE: do NOT apply a GST ÷1.18 net-revenue adjustment here. The v1
    # attribution API already returns ex-GST revenue (its `net_sales` /
    # `attributed_orders_revenue`; the tax-inclusive figure is a separate
    # field). Dividing again double-nets GST and understates revenue ~18%,
    # breaking reconciliation with the dashboard/KPI strip (canonical bug #5:
    # never hardcode ÷1.18).
    return out


# ---------------------------------------------------------------------------
# Rollup builders (weighted metrics + Grand Total)
# ---------------------------------------------------------------------------
def _sum_base(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    present_base = [c for c in _ADDITIVE_COLS if c in df.columns]
    return (
        df.groupby(group_cols, dropna=False)[present_base]
        .sum()
        .reset_index()
    )


def _derive_row_metrics(rollup: pd.DataFrame, *, include_spend_metrics: bool) -> pd.DataFrame:
    """Add weighted per-row rate metrics computed from the summed base columns."""
    rev = rollup.get("shopify_revenue")
    cogs = rollup.get("shopify_cogs")
    spend = rollup.get("spend")
    clicks = rollup.get("clicks")
    impr = rollup.get("impressions")

    if clicks is not None and impr is not None:
        rollup["ctr"] = [compute_ctr(c, i) for c, i in zip(clicks, impr)]
    if include_spend_metrics and spend is not None:
        if clicks is not None:
            rollup["cpc"] = [safe_div(s, c) for s, c in zip(spend, clicks)]
        if rev is not None:
            rollup["gross_roas"] = [compute_roas(r, s) for r, s in zip(rev, spend)]
        if rev is not None and cogs is not None:
            rollup["net_roas"] = [compute_net_roas(r, c, s) for r, c, s in zip(rev, cogs, spend)]
            rollup["be_roas"] = [compute_be_roas(c, s) for c, s in zip(cogs, spend)]
            rollup["net_profit"] = [compute_net_profit(r, c, s) for r, c, s in zip(rev, cogs, spend)]
    elif rev is not None and cogs is not None:
        # Organic: no ad spend, so "profit" is revenue net of COGS only.
        rollup["net_profit"] = [float(r) - float(c) for r, c in zip(rev, cogs)]
    return rollup


def _append_grand_total(
    rollup: pd.DataFrame,
    label_col: str,
    *,
    include_spend_metrics: bool,
) -> pd.DataFrame:
    """Append a Grand Total row whose rates are recomputed from summed base
    metrics (so the total ROAS/CTR is weighted, not a mean of row rates)."""
    if rollup.empty:
        return rollup

    total: dict = {c: "" for c in rollup.columns}
    total[label_col] = GRAND_TOTAL_LABEL
    for col in _ADDITIVE_COLS:
        if col in rollup.columns:
            total[col] = float(pd.to_numeric(rollup[col], errors="coerce").fillna(0).sum())

    imp = total.get("impressions", 0) or 0
    clk = total.get("clicks", 0) or 0
    spd = total.get("spend", 0) or 0
    rev = total.get("shopify_revenue", 0) or 0
    cogs = total.get("shopify_cogs", 0) or 0

    if "ctr" in rollup.columns:
        total["ctr"] = compute_ctr(clk, imp)
    if include_spend_metrics:
        if "cpc" in rollup.columns:
            total["cpc"] = safe_div(spd, clk)
        if "gross_roas" in rollup.columns:
            total["gross_roas"] = compute_roas(rev, spd)
        if "net_roas" in rollup.columns:
            total["net_roas"] = compute_net_roas(rev, cogs, spd)
        if "be_roas" in rollup.columns:
            total["be_roas"] = compute_be_roas(cogs, spd)
        if "net_profit" in rollup.columns:
            total["net_profit"] = compute_net_profit(rev, cogs, spd)
    elif "net_profit" in rollup.columns:
        total["net_profit"] = float(rev) - float(cogs)

    return pd.concat([rollup, pd.DataFrame([total])], ignore_index=True)


# Real on-site funnel columns, sourced from GET /v1/meta-funnel (gold.fct_session_funnel),
# joined to Meta ad rows by ad_id. The attribution API does NOT carry landing-page /
# product-view counts, which is why the old (clicks - lpv)/clicks bounce rate collapsed
# to a fake 100. See docs/data_sources_cannonical.md §3.
_META_FUNNEL_COLS = (
    "sessions",
    "landing_page_views",
    "add_to_cart",
    "checkout_start",
    "bounce_rate",
    "conversion_rate",
)


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _meta_funnel_maps(start_date: str, end_date: str) -> tuple[dict, dict]:
    """Return ({ad_id: {funnel metrics}}, summary) from GET /v1/meta-funnel."""
    try:
        data = fetch_meta_funnel(start_date, end_date) or {}
    except Exception as exc:
        logger.warning("entity_report: meta-funnel fetch failed; funnel columns left blank: %s", exc)
        return {}, {}
    by_ad: dict = {}
    for ad in data.get("ads") or []:
        sf = ad.get("site_funnel") or {}
        fr = ad.get("funnel_rates") or {}
        by_ad[str(ad.get("ad_id"))] = {
            "sessions": _to_float(sf.get("total_sessions")),
            "landing_page_views": _to_float(sf.get("total_product_views")),
            "add_to_cart": _to_float(sf.get("total_add_to_cart")),
            "checkout_start": _to_float(sf.get("total_checkout_start", sf.get("total_initiate_checkout"))),
            "bounce_rate": _to_float(sf.get("avg_bounce_rate")),
            "conversion_rate": _to_float(fr.get("conversion_rate")),
        }
    return by_ad, (data.get("summary") or {})


def build_meta_ad_rollup(
    df: pd.DataFrame,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """Meta ad-level rollup: one row per (date, campaign, adset, ad).

    When start/end are given, real on-site funnel metrics (sessions, landing-page
    views, add-to-cart, checkout starts, bounce rate, conversion rate) are joined
    from /v1/meta-funnel by ad_id — replacing the attribution-derived fake bounce
    rate. Funnel figures are per-ad over the whole range, so the per-row values are
    exact for a single-day report; the Grand Total uses the funnel summary directly.
    """
    meta = df[df.get("source") == "Meta Ads"].copy()
    if meta.empty:
        return pd.DataFrame()
    group_cols = [c for c in ["date_start", "channel", "campaign_name", "adset_name", "ad_name", "ad_id"] if c in meta.columns]
    rollup = _sum_base(meta, group_cols)
    rollup = _derive_row_metrics(rollup, include_spend_metrics=True)

    funnel_summary: dict = {}
    if start_date and end_date and "ad_id" in rollup.columns:
        by_ad, funnel_summary = _meta_funnel_maps(start_date, end_date)
        ids = rollup["ad_id"].astype(str)
        for col in _META_FUNNEL_COLS:
            rollup[col] = [by_ad.get(a, {}).get(col, 0.0) for a in ids]

    if "net_roas" in rollup.columns:
        rollup = rollup.sort_values("net_roas", ascending=False).reset_index(drop=True)

    out = _append_grand_total(rollup, "campaign_name", include_spend_metrics=True)

    if funnel_summary:
        # Grand Total funnel = the funnel endpoint's own summary (session-weighted),
        # not a naive sum of per-ad rows (avoids double counts on multi-day ranges).
        fr = funnel_summary.get("funnel_rates") or {}
        idx = out.index[-1]
        out.loc[idx, "sessions"] = _to_float(funnel_summary.get("total_sessions"))
        out.loc[idx, "landing_page_views"] = _to_float(funnel_summary.get("total_product_views"))
        out.loc[idx, "add_to_cart"] = _to_float(funnel_summary.get("total_add_to_cart"))
        out.loc[idx, "checkout_start"] = _to_float(
            funnel_summary.get("total_checkout_start", funnel_summary.get("total_initiate_checkout"))
        )
        out.loc[idx, "bounce_rate"] = _to_float(funnel_summary.get("avg_bounce_rate"))
        out.loc[idx, "conversion_rate"] = _to_float(fr.get("overall_conversion_rate"))
    return out


def build_google_campaign_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """Google campaign-level rollup: one row per (date, campaign)."""
    google = df[df.get("source") == "Google Ads"].copy()
    if google.empty:
        return pd.DataFrame()
    group_cols = [c for c in ["date_start", "channel", "campaign_name"] if c in google.columns]
    rollup = _sum_base(google, group_cols)
    rollup = _derive_row_metrics(rollup, include_spend_metrics=True)
    if "net_roas" in rollup.columns:
        rollup = rollup.sort_values("net_roas", ascending=False).reset_index(drop=True)
    return _append_grand_total(rollup, "campaign_name", include_spend_metrics=True)


def build_organic_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """Organic rollup by utm-campaign: one row per (date, campaign)."""
    organic = df[df.get("source") == "Organic"].copy()
    if organic.empty:
        return pd.DataFrame()
    if "campaign_name" in organic.columns:
        organic["campaign_name"] = organic["campaign_name"].fillna("Organic Traffic")
    else:
        organic["campaign_name"] = "Organic Traffic"
    group_cols = [c for c in ["date_start", "channel", "campaign_name"] if c in organic.columns]
    rollup = _sum_base(organic, group_cols)
    # Organic has no ad spend; keep revenue/cogs/orders and net (rev - cogs).
    keep = group_cols + [c for c in ["shopify_orders", "shopify_revenue", "shopify_cogs", "total_sku_quantity"] if c in rollup.columns]
    rollup = rollup[keep]
    rollup = _derive_row_metrics(rollup, include_spend_metrics=False)
    if "shopify_revenue" in rollup.columns:
        rollup = rollup.sort_values("shopify_revenue", ascending=False).reset_index(drop=True)
    return _append_grand_total(rollup, "campaign_name", include_spend_metrics=False)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def _grand_total_value(rollup: pd.DataFrame, col: str) -> float:
    if rollup is None or rollup.empty or col not in rollup.columns:
        return 0.0
    key = "campaign_name"
    if key in rollup.columns:
        gt = rollup[rollup[key] == GRAND_TOTAL_LABEL]
        if not gt.empty:
            return float(pd.to_numeric(gt[col], errors="coerce").fillna(0).iloc[0])
    return float(pd.to_numeric(rollup[col], errors="coerce").fillna(0).sum())


def reconcile_entity_report(
    rollups: dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
    *,
    tolerance: float = 1.0,
) -> pd.DataFrame:
    """Build a reconciliation table verifying the report ties out.

    Checks:
      1. Internal — each channel's Grand Total == sum of its entity rows.
      2. Cross-source — Meta + Google ad spend == pnl/summary ad-spend
         (both read the same gold tables).
    The KPI "Total Revenue" (fct_daily_pnl.gross_revenue, all orders) is
    intentionally *not* asserted equal to attributed revenue (§1b) — it is
    reported for context only.
    """
    rows: list[dict] = []

    def add(metric: str, report_val: float, source_val, source_label: str, *, assertable: bool = True):
        diff = None
        status = "info"
        if assertable and source_val is not None:
            diff = round(float(report_val) - float(source_val), 4)
            status = "OK" if abs(diff) <= tolerance else "MISMATCH"
        rows.append({
            "metric": metric,
            "entity_report": round(float(report_val), 4),
            "source": None if source_val is None else round(float(source_val), 4),
            "source_of_truth": source_label,
            "diff": diff,
            "status": status,
        })

    # 1. Internal consistency: Grand Total row == sum of the entity rows.
    for name, rollup in rollups.items():
        if rollup is None or rollup.empty:
            continue
        body = rollup
        if "campaign_name" in rollup.columns:
            body = rollup[rollup["campaign_name"] != GRAND_TOTAL_LABEL]
        for col in ("spend", "shopify_revenue", "shopify_cogs", "shopify_orders"):
            if col in rollup.columns:
                add(
                    f"{name}: {col} (grand total vs Σ rows)",
                    _grand_total_value(rollup, col),
                    float(pd.to_numeric(body[col], errors="coerce").fillna(0).sum()),
                    "internal Σ rows",
                )

    # 2. Cross-source: ad spend vs pnl/summary (gold-backed).
    try:
        pnl = fetch_pnl_summary(start_date, end_date) or {}
        meta_spend_rep = _grand_total_value(rollups.get("meta"), "spend")
        google_spend_rep = _grand_total_value(rollups.get("google"), "spend")
        add("Meta ad spend", meta_spend_rep, pnl.get("meta_ads_cost"), "pnl/summary.meta_ads_cost")
        add("Google ad spend", google_spend_rep, pnl.get("google_ads_cost"), "pnl/summary.google_ads_cost")
        # Context-only: attributed revenue != KPI gross revenue by design (§1b).
        attributed_rev = (
            _grand_total_value(rollups.get("meta"), "shopify_revenue")
            + _grand_total_value(rollups.get("google"), "shopify_revenue")
            + _grand_total_value(rollups.get("organic"), "shopify_revenue")
        )
        add(
            "Attributed revenue (context; != KPI gross revenue by design)",
            attributed_rev,
            pnl.get("total_sales"),
            "pnl/summary.total_sales",
            assertable=False,
        )
    except Exception as exc:
        logger.warning("entity_report: pnl/summary cross-check skipped: %s", exc)
        rows.append({
            "metric": "pnl/summary cross-check",
            "entity_report": None,
            "source": None,
            "source_of_truth": "pnl/summary",
            "diff": None,
            "status": f"skipped: {exc}",
        })

    recon = pd.DataFrame(rows)
    mism = recon[recon["status"] == "MISMATCH"] if not recon.empty else recon
    if not mism.empty:
        logger.warning("entity_report: %d reconciliation mismatch(es):\n%s", len(mism), mism.to_string(index=False))
    else:
        logger.info("entity_report: reconciliation OK (%d checks)", len(recon))
    return recon


# ---------------------------------------------------------------------------
# Excel formatting + emission
# ---------------------------------------------------------------------------
def _apply_entity_formatting(writer, sheet_name: str, df: pd.DataFrame) -> None:
    """WTD/MTD-style formatting: header, Grand Total highlight, ROAS/CTR/spend scales."""
    workbook = writer.book
    center_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})
    header_fmt = workbook.add_format({
        "bold": True, "align": "center", "valign": "vcenter",
        "bg_color": "#F2F2F2", "border": 1,
    })
    total_fmt = workbook.add_format({"bold": True, "bg_color": "#E6F3FF"})

    ws = writer.sheets[sheet_name]
    ws.set_column(0, len(df.columns) - 1, None, center_fmt)
    ws.freeze_panes(1, 0)
    ws.set_row(0, None, header_fmt)
    ws.set_row(len(df), None, total_fmt)  # Grand Total is the last row

    last_row = len(df)
    for roas_col_name in ("net_roas", "gross_roas", "roas"):
        if roas_col_name in df.columns:
            col = df.columns.get_loc(roas_col_name)
            green = workbook.add_format({"font_color": "#006100", "bg_color": "#C6EFCE"})
            red = workbook.add_format({"font_color": "#9C0006", "bg_color": "#FFC7CE"})
            ws.conditional_format(1, col, last_row, col, {"type": "cell", "criteria": ">=", "value": 1, "format": green})
            ws.conditional_format(1, col, last_row, col, {"type": "cell", "criteria": "<", "value": 1, "format": red})
            break

    if "ctr" in df.columns:
        col = df.columns.get_loc("ctr")
        ws.conditional_format(1, col, last_row, col, {
            "type": "2_color_scale",
            "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
            "max_type": "num", "max_value": 5, "max_color": "#90EE90",
        })
    for money_col, max_color in (("spend", "#FFFF00"), ("shopify_revenue", "#90EE90")):
        if money_col in df.columns:
            col = df.columns.get_loc(money_col)
            vals = pd.to_numeric(df[money_col], errors="coerce").fillna(0)
            ws.conditional_format(1, col, last_row, col, {
                "type": "2_color_scale",
                "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
                "max_type": "num", "max_value": (vals.max() if len(vals) else 1000), "max_color": max_color,
            })


def _write_sheet(writer, sheet_name: str, df: pd.DataFrame, round_fn: Optional[Callable]) -> None:
    sheet_name = sheet_name[:31]
    if df is None or df.empty:
        pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
        return
    out = round_fn(df) if round_fn else df
    out.to_excel(writer, sheet_name=sheet_name, index=False)
    try:
        _apply_entity_formatting(writer, sheet_name, out)
    except Exception as exc:  # pragma: no cover - formatting must never break the write
        logger.warning("entity_report: formatting error on %s: %s", sheet_name, exc)


def add_entity_sheets(
    writer,
    start_date: str,
    end_date: str,
    round_for_output_fn: Optional[Callable] = None,
    *,
    brand_id: Optional[int] = None,
    include_amazon: bool = True,
    sheet_prefix: str = "",
) -> pd.DataFrame:
    """Append the clean entity-report sheets to an open ``pd.ExcelWriter``.

    Sheets: ``{p}meta_ads``, ``{p}google_campaigns``, ``{p}organic``,
    the reused Amazon sheets, and ``{p}reconciliation``.
    Returns the reconciliation DataFrame.
    """
    df = fetch_entity_attribution(start_date, end_date)

    meta = build_meta_ad_rollup(df, start_date, end_date)
    google = build_google_campaign_rollup(df)
    organic = build_organic_rollup(df)

    _write_sheet(writer, f"{sheet_prefix}meta_ads", meta, round_for_output_fn)
    _write_sheet(writer, f"{sheet_prefix}google_campaigns", google, round_for_output_fn)
    _write_sheet(writer, f"{sheet_prefix}organic", organic, round_for_output_fn)

    if include_amazon:
        try:
            from amazon_entity_report import add_amazon_sheets_for_timeframe

            start_dt = datetime.strptime(str(start_date)[:10], "%Y-%m-%d")
            end_dt = datetime.strptime(str(end_date)[:10], "%Y-%m-%d")
            add_amazon_sheets_for_timeframe(
                writer,
                f"{sheet_prefix}entity" if sheet_prefix else "entity",
                start_dt,
                end_dt,
                round_for_output_fn=round_for_output_fn,
                brand_id=brand_id if brand_id is not None else get_api_brand_id(),
            )
        except Exception as exc:
            logger.warning("entity_report: Amazon sheets skipped: %s", exc)

    recon = reconcile_entity_report(
        {"meta": meta, "google": google, "organic": organic}, start_date, end_date
    )
    _write_sheet(writer, f"{sheet_prefix}reconciliation", recon, None)
    return recon


def build_entity_report(
    start_date: str,
    end_date: str,
    out_dir: Optional[str] = None,
    *,
    brand_id: Optional[int] = None,
    include_amazon: bool = True,
) -> str:
    """Standalone runner: build a clean entity-report workbook and return its path."""
    if out_dir is None:
        from global_config import get_report_dir

        out_dir = get_report_dir()
    os.makedirs(out_dir, exist_ok=True)

    round_fn: Optional[Callable]
    try:
        from dailyrollup import round_for_output as round_fn  # type: ignore
    except Exception:
        round_fn = None

    s, e = str(start_date)[:10], str(end_date)[:10]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = os.path.join(out_dir, f"Entity report (clean)_{s}_to_{e}_{ts}.xlsx")

    with pd.ExcelWriter(
        xlsx_path, engine="xlsxwriter",
        engine_kwargs={"options": {"nan_inf_to_errors": True}},
    ) as writer:
        add_entity_sheets(
            writer, s, e, round_fn,
            brand_id=brand_id, include_amazon=include_amazon,
        )
    logger.info("entity_report: wrote %s", xlsx_path)
    return xlsx_path


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Build the clean attribution-sourced entity report")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--out-dir", default=None, help="Output directory (defaults to get_report_dir())")
    parser.add_argument("--no-amazon", action="store_true", help="Skip Amazon sheets")
    args = parser.parse_args()

    path = build_entity_report(args.start, args.end, args.out_dir, include_amazon=not args.no_amazon)
    print(f"Wrote {path}")
