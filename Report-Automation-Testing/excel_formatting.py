"""Shared Excel sheet formatting for the entity / WTD / MTD reports.

Every report sheet used to carry its own copy of the header / total-row /
conditional-format block, and they had drifted apart: only the Product
Profitability sheet set number formats and column widths, and the Amazon SP
sheet styled neither its header nor its totals. ``apply_sheet_formatting``
is the one implementation they all share now.

Colour
------
Positive/negative and all gradients use a **blue<->red** pair, never green/red.
The old green/red fills (#C6EFCE / #FFC7CE) fail colour-vision-deficiency
separation badly: deuteranopia Delta E 3.2, and 12.3 even for normal vision -
below the 15 floor, i.e. hard to tell apart for everyone. The blue/red anchors
used here (#2a78d6 / #e34948) measure Delta E 21.6 protan, 32.3 normal, and pass
every check in the validated reference palette.

Cells carry black text, so fills are light tints of those hues and the sign is
reinforced by a dark same-hue font colour - the number itself is always visible,
so meaning never rests on colour alone.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

# Columns recognised by name so callers rarely need to spell them out.
MONEY_COLS = {
    "spend", "sales", "ad_sales", "revenue", "gross", "net_payout", "commission",
    "closing", "shipping", "tax_withheld", "product_cost", "cogs", "gross_profit",
    "profit", "net_profit", "net_after_spend", "finance_refunds",
    "refunded_amount", "refunded_amount_incl_gst", "order_amount", "label_cost",
    "item_price", "shopify_revenue", "shopify_cogs", "sku_revenue", "sku_cogs",
    "unit_cost", "unit_price", "order_total",
}
PCT_COLS = {"ctr", "gross_margin_pct", "bounce_rate", "acos"}
RATIO_COLS = {"roas", "gross_roas", "net_roas", "cpc", "cpm"}
INT_COLS = {
    "impressions", "clicks", "orders", "ad_orders", "quantity_ordered",
    "quantity_shipped", "quantity", "return_quantity", "order_quantity",
    "units", "order_count", "shopify_orders",
}
DATE_COLS = {
    "date", "purchase_date", "report_date", "return_delivery_date", "order_date",
    "date_start", "return_request_date",
}
# Long digit strings (order / item / ad ids) must stay text, never 4.03E+14.
ID_COLS = {
    "ad_id", "amazon_order_id", "order_item_id", "campaign_id", "ad_group_id",
    "asin", "sku", "merchant_sku", "advertised_sku",
}

# --- validated palette (see module docstring) --------------------------------
_INK = "#0b0b0b"
_NEUTRAL = "#F0EFEC"          # documented neutral midpoint
_BLUE_TINT = "#CDE2FB"        # blue step 100
_BLUE_MID = "#9EC5F4"         # blue step 200 - gradient ceiling, text stays legible
_BLUE_DARK = "#104281"        # blue step 650, for font colour
_RED_TINT = "#F8D1D1"         # #e34948 at 25% over white
_RED_DARK = "#d03b3b"         # status critical, 4.68:1 on white

_HEADER = {"bold": True, "align": "center", "valign": "vcenter",
           "bg_color": _NEUTRAL, "border": 1, "font_color": _INK, "text_wrap": True}
_TOTAL = {"bold": True, "bg_color": _BLUE_TINT, "font_color": _INK, "top": 1}
_SECTION = {"bold": True, "bg_color": _NEUTRAL, "font_color": _INK}
_POS = {"font_color": _BLUE_DARK, "bg_color": _BLUE_TINT}
_NEG = {"font_color": _RED_DARK, "bg_color": _RED_TINT}

_NUM_FMT = "#,##0.00"
_INT_FMT = "#,##0"
_PCT_FMT = "0.00"
_RATIO_FMT = "0.00"
_DATE_FMT = "dd-mm-yyyy"


def _width_for(col: str) -> int:
    if col in ("title", "item_name"):
        return 44
    if col in ("campaign_name", "ad_group_name", "ad_name", "adset_name"):
        return 30
    if col in ("amazon_order_id", "order_item_id", "ad_id", "sku", "merchant_sku"):
        return 21
    if col in DATE_COLS:
        return 12
    return 14


TOTAL_LABELS = ("Grand Total", "All", "Total")


def move_totals_to_top(
    df: pd.DataFrame, label_cols: Sequence[str] = ()
) -> tuple[pd.DataFrame, int]:
    """Lift the Grand Total row(s) to the top; returns (df, how_many_moved).

    Totals sit directly under the header so the headline numbers are the first
    thing on screen, with no scrolling to the end of a long sheet.
    """
    if df is None or df.empty:
        return df, 0
    cols = [c for c in (label_cols or ("campaign_name", "channel", "amazon_order_id",
                                       "date_start", "SKU", "sku"))
            if c in df.columns]
    if not cols:
        return df, 0
    mask = pd.Series(False, index=df.index)
    for col in cols:
        mask |= df[col].astype(str).str.strip().isin(TOTAL_LABELS)
    if not mask.any():
        return df, 0
    totals = df[mask]
    rest = df[~mask]
    return (pd.concat([totals, rest], ignore_index=True), int(mask.sum()))


def section_format(workbook):
    """Bold neutral banner used for the block labels inside a sheet."""
    return workbook.add_format(_SECTION)


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
    totals_at_top: bool = True,
) -> None:
    """Format a written sheet: header, widths, number formats, totals, gradients.

    total_rows       how many rows are totals / reconciliation
    totals_at_top    totals sit directly under the header (the default) rather
                     than at the end of the sheet
    heatmap_cols     light white->blue gradient, scaled to the column's own max
    threshold_cols   blue at/above 1, red below (ROAS-style columns)
    sign_cols        blue above 0, red below (profit-style columns)
    pct_as_fraction  True when percentages are stored as 0.25 rather than 25
    """
    worksheet = writer.sheets.get(sheet_name)
    if worksheet is None or df is None or df.empty:
        return

    workbook = writer.book
    header_fmt = workbook.add_format(_HEADER)
    total_fmt = workbook.add_format(_TOTAL)
    center_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})
    left_fmt = workbook.add_format({"align": "left", "valign": "vcenter"})
    num_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                   "num_format": _NUM_FMT})
    int_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                   "num_format": _INT_FMT})
    date_fmt = workbook.add_format({"align": "center", "valign": "vcenter",
                                    "num_format": _DATE_FMT})
    id_fmt = workbook.add_format({"align": "left", "valign": "vcenter",
                                  "num_format": "@"})
    pct_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                   "num_format": "0.00%" if pct_as_fraction else _PCT_FMT})
    ratio_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                     "num_format": _RATIO_FMT})

    money = set(money_cols) if money_cols is not None else MONEY_COLS
    pct = set(pct_cols) if pct_cols is not None else PCT_COLS
    ratio = set(ratio_cols) if ratio_cols is not None else RATIO_COLS

    for idx, col in enumerate(df.columns):
        name = str(col)
        if name in DATE_COLS:
            fmt = date_fmt
        elif name in ID_COLS:
            fmt = id_fmt
        elif name in money:
            fmt = num_fmt
        elif name in INT_COLS:
            fmt = int_fmt
        elif name in pct:
            fmt = pct_fmt
        elif name in ratio:
            fmt = ratio_fmt
        elif name in ("title", "item_name"):
            fmt = left_fmt
        else:
            fmt = center_fmt
        worksheet.set_column(idx, idx, _width_for(name), fmt)

    worksheet.set_row(0, 30, header_fmt)

    if total_rows and totals_at_top:
        # Header + totals stay visible; no autofilter, since filtering or
        # sorting a range containing the total row would drag it into the data.
        for r in range(1, total_rows + 1):
            worksheet.set_row(r, None, total_fmt)
        worksheet.freeze_panes(1 + total_rows, freeze_col)
        data_first, data_last = 1 + total_rows, len(df)
    else:
        worksheet.freeze_panes(1, freeze_col)
        last_row = len(df)  # header occupies row 0, so data ends here
        for offset in range(total_rows):
            worksheet.set_row(last_row - offset, None, total_fmt)
        worksheet.autofilter(0, 0, max(len(df) - total_rows, 1), len(df.columns) - 1)
        data_first, data_last = 1, (last_row - total_rows if total_rows else last_row)

    if data_last < data_first:
        return

    for col in threshold_cols:
        if col not in df.columns:
            continue
        pos = df.columns.get_loc(col)
        worksheet.conditional_format(data_first, pos, data_last, pos, {
            "type": "cell", "criteria": ">=", "value": 1,
            "format": workbook.add_format(_POS)})
        worksheet.conditional_format(data_first, pos, data_last, pos, {
            "type": "cell", "criteria": "<", "value": 1,
            "format": workbook.add_format(_NEG)})

    for col in sign_cols:
        if col not in df.columns:
            continue
        pos = df.columns.get_loc(col)
        worksheet.conditional_format(data_first, pos, data_last, pos, {
            "type": "cell", "criteria": ">", "value": 0,
            "format": workbook.add_format(_POS)})
        worksheet.conditional_format(data_first, pos, data_last, pos, {
            "type": "cell", "criteria": "<", "value": 0,
            "format": workbook.add_format(_NEG)})

    for col in heatmap_cols:
        if col not in df.columns:
            continue
        pos = df.columns.get_loc(col)
        # Scale to the data rows: including the total would flatten the ramp.
        values = pd.to_numeric(
            df[col].iloc[data_first - 1:data_last], errors="coerce").fillna(0)
        top = float(values.max()) if len(values) else 0.0
        if not np.isfinite(top) or top <= 0:
            top = 1000.0
        low = float(values.min()) if len(values) else 0.0
        if low < 0:
            # Signed magnitude: red arm -> neutral -> blue arm, gray midpoint.
            worksheet.conditional_format(data_first, pos, data_last, pos, {
                "type": "3_color_scale",
                "min_type": "num", "min_value": low, "min_color": _RED_TINT,
                "mid_type": "num", "mid_value": 0, "mid_color": _NEUTRAL,
                "max_type": "num", "max_value": top, "max_color": _BLUE_MID,
            })
        else:
            worksheet.conditional_format(data_first, pos, data_last, pos, {
                "type": "2_color_scale",
                "min_type": "num", "min_value": 0, "min_color": "#FFFFFF",
                "max_type": "num", "max_value": top, "max_color": _BLUE_MID,
            })
