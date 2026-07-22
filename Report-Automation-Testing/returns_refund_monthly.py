#!/usr/bin/env python3
"""
Monthly refund automation for Amazon returns.

Rule (agreed with business):
    Refund amount for a month = SUM("Refunded Amount") over all return lines
    whose "Return delivery date" falls in that month.

Notes:
  * Rows with a blank "Return delivery date" are NOT yet booked to any month
    (return not scanned/delivered back). They are reported separately as
    "pending" so the running monthly total is transparent.
  * Rejected / Replacement lines carry no "Refunded Amount" and therefore add
    0 to the month even when a delivery date exists.
  * Monetary output is rounded to 2 decimals for presentation.

Usage:
    python returns_refund_monthly.py <returns_export.csv|.tsv|.txt> [--out summary.csv]

Input may be comma- or tab-separated; the delimiter is auto-detected. It must
contain the Amazon returns-report columns, including "Return delivery date"
and "Refunded Amount".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Amazon export headers we depend on (matched case-insensitively / trimmed).
COL_DELIVERY = "Return delivery date"
COL_REFUND = "Refunded Amount"
# Extra columns kept only to make the pending / diagnostic view readable.
COL_ORDER = "Order ID"
COL_RESOLUTION = "Resolution"
COL_STATUS = "Return request status"


def _read_returns(path: Path) -> pd.DataFrame:
    """Load the returns export, auto-detecting comma vs tab delimiter."""
    # sep=None + engine="python" sniffs the delimiter (handles .csv and .tsv/.txt).
    df = pd.read_csv(path, sep=None, engine="python", dtype=str, keep_default_na=False)
    # Normalise header whitespace so "Refunded Amount " etc. still match.
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in (COL_DELIVERY, COL_REFUND) if c not in df.columns]
    if missing:
        raise SystemExit(
            f"Input is missing required column(s): {missing}\n"
            f"Found columns: {list(df.columns)}"
        )
    return df


def _to_amount(series: pd.Series) -> pd.Series:
    """Blank -> 0.0; strip commas/currency; coerce to float."""
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.strip()
        .replace({"": "0", "nan": "0"})
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)


def _to_date(series: pd.Series) -> pd.Series:
    """Parse dates like '6-Jul-26'. Blanks -> NaT."""
    trimmed = series.astype(str).str.strip().replace({"": None, "nan": None})
    # Amazon report uses D-Mon-YY; dayfirst covers it, format is inferred per value.
    return pd.to_datetime(trimmed, format="%d-%b-%y", errors="coerce")


def build_monthly_summary(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (monthly_summary, pending_lines)."""
    work = df.copy()
    work["_refund"] = _to_amount(work[COL_REFUND])
    work["_delivery"] = _to_date(work[COL_DELIVERY])

    booked = work[work["_delivery"].notna()].copy()
    pending = work[work["_delivery"].isna()].copy()

    booked["Month"] = booked["_delivery"].dt.to_period("M").astype(str)
    summary = (
        booked.groupby("Month")
        .agg(Returns=("_refund", "size"), Refunded_Amount=("_refund", "sum"))
        .reset_index()
        .sort_values("Month")
    )
    summary["Refunded_Amount"] = summary["Refunded_Amount"].round(2)

    # Diagnostic view of the not-yet-booked lines.
    keep = [c for c in (COL_ORDER, COL_STATUS, COL_RESOLUTION) if c in pending.columns]
    pending_view = pending[keep].copy()

    return summary, pending_view


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Amazon returns export (.csv/.tsv/.txt)")
    parser.add_argument("--out", type=Path, help="Write monthly summary to this CSV")
    args = parser.parse_args(argv)

    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    df = _read_returns(args.input)
    summary, pending = build_monthly_summary(df)

    total = round(summary["Refunded_Amount"].sum(), 2)

    print("Refund amount by month (booked on Return delivery date)")
    print("=" * 56)
    if summary.empty:
        print("  (no lines with a return delivery date)")
    else:
        for _, r in summary.iterrows():
            print(f"  {r['Month']:>7}   {int(r['Returns']):>4} returns   "
                  f"INR {r['Refunded_Amount']:>12,.2f}")
    print("-" * 56)
    print(f"  {'TOTAL':>7}   {int(summary['Returns'].sum()) if not summary.empty else 0:>4} returns   "
          f"INR {total:>12,.2f}")
    print(f"\n  Pending (no delivery date yet, unbooked): {len(pending)} line(s)")

    if args.out:
        summary.to_csv(args.out, index=False)
        print(f"\nSummary written to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
