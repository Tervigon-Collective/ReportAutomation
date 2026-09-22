"""Shared Excel sheet formatting for the entity / WTD / MTD reports.

Every report sheet used to carry its own copy of the header / total-row /
conditional-format block, and they had drifted apart: only the Product
Profitability sheet set number formats and column widths, and the Amazon SP
sheet styled neither its header nor its totals. ``apply_sheet_formatting``
is the one implementation they all share now.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

# Columns recognised by name so callers rarely need to spell them out.
MONEY_COLS = {
    "spend", "sales", "revenue", "gross", "net_payout", "commission", "closing",
    "shipping", "tax_withheld", "product_cost", "cogs", "gross_profit", "profit",
    "finance_refunds", "refunded_amount", "refunded_amount_incl_gst",
    "order_amount", "label_cost", "item_price", "shopify_revenue", "shopify_cogs",
    "sku_revenue", "sku_cogs", "unit_cost", "unit_price", "order_total",
}
PCT_COLS = {"ctr", "gross_margin_pct", "bounce_rate", "acos"}
RATIO_COLS = {"roas", "gross_roas", "net_roas", "cpc", "cpm"}

_HEADER = {"bold": True, "align": "center", "valign": "vcenter",
           "bg_color": "#F2F2F2", "border": 1}
_TOTAL = {"bold": True, "bg_color": "#E6F3FF"}
_GREEN = {"font_color": "#006100", "bg_color": "#C6EFCE"}
_RED = {"font_color": "#9C0006", "bg_color": "#FFC7CE"}

_NUM_FMT = "#,##0.00"
_PCT_FMT = "0.00"
_RATIO_FMT = "0.00"


def _width_for(col: str) -> int:
    if col in ("title", "item_name", "campaign_name", "ad_group_name", "ad_name"):
        return 40
    if col in ("sku", "merchant_sku", "asin", "amazon_order_id", "order_item_id"):
        return 20
    return 14


def apply_sheet_formatting(
    writer,
    sheet_name: str,
    df: pd.DataFrame,
    total_rows: int = 0,
    money_cols: Optional[Iterable[str]] = None,
    pct_cols: Optional[Iterable[str]] = None,
    ratio_cols: Optional[Iterable[str]] = None,
    heatmap_cols: Sequence[str] = (),
    threshold_cols: Sequence[str] = (),
    sign_cols: Sequence[str] = (),
    pct_as_fraction: bool = False,
    freeze_col: int = 0,
) -> None:
    """Format a written sheet: header, widths, number formats, totals, heatmaps.

    total_rows       how many rows at the bottom are totals / reconciliation
    heatmap_cols     2-colour scale, scaled to each column's own max
    threshold_cols   green at/above 1, red below (ROAS-style columns)
    sign_cols        green above 0, red below (profit-style columns)
    pct_as_fraction  True when percentages are stored as 0.25 rather than 25
    """
    worksheet = writer.sheets.get(sheet_name)
    if worksheet is None or df is None or df.empty:
        return

    workbook = writer.book
    header_fmt = workbook.add_format(_HEADER)
    total_fmt = workbook.add_format(_TOTAL)
    center_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})
    num_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                   "num_format": _NUM_FMT})
    pct_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                   "num_format": "0.00%" if pct_as_fraction else _PCT_FMT})
    ratio_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                     "num_format": _RATIO_FMT})

    money = set(money_cols) if money_cols is not None else MONEY_COLS
    pct = set(pct_cols) if pct_cols is not None else PCT_COLS
    ratio = set(ratio_cols) if ratio_cols is not None else RATIO_COLS

    for idx, col in enumerate(df.columns):
        name = str(col)
        if name in money:
            fmt = num_fmt
        elif name in pct:
            fmt = pct_fmt
        elif name in ratio:
            fmt = ratio_fmt
        else:
            fmt = center_fmt
        worksheet.set_column(idx, idx, _width_for(name), fmt)

    worksheet.freeze_panes(1, freeze_col)
    worksheet.set_row(0, None, header_fmt)

    last_row = len(df)  # header occupies row 0, so data ends here
    for offset in range(total_rows):
        worksheet.set_row(last_row - offset, None, total_fmt)

    data_last = last_row - total_rows if total_rows else last_row
    if data_last < 1:
        return

    for col in threshold_cols:
        if col not in df.columns:
            continue
        pos = df.columns.get_loc(col)
        worksheet.conditional_format(1, pos, data_last, pos, {
            "type": "cell", "criteria": ">=", "value": 1,
            "format": workbook.add_format(_GREEN)})
        worksheet.conditional_format(1, pos, data_last, pos, {
            "type": "cell", "criteria": "<", "value": 1,
            "format": workbook.add_format(_RED)})

    for col in sign_cols:
        if col not in df.columns:
            continue
        pos = df.columns.get_loc(col)
        worksheet.conditional_format(1, pos, data_last, pos, {
            "type": "cell", "criteria": ">", "value": 0,
            "format": workbook.add_format(_GREEN)})
        worksheet.conditional_format(1, pos, data_last, pos, {
            "type": "cell", "criteria": "<", "value": 0,
            "format": workbook.add_format(_RED)})

    for col in heatmap_cols:
        if col not in df.columns:
            continue
        pos = df.columns.get_loc(col)
        values = pd.to_numeric(df[col], errors="coerce").fillna(0)
        top = float(values.max()) if len(values) else 0.0
        if not np.isfinite(top) or top <= 0:
            top = 1000.0
        colour = "#90EE90" if col in ("sales", "revenue", "gross") else "#FFFF00"
        worksheet.conditional_format(1, pos, data_last, pos, {
            "type": "2_color_scale",
            "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
            "max_type": "num", "max_value": top, "max_color": colour,
        })
