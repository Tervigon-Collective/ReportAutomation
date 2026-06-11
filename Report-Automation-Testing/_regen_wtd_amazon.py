"""Regenerate the wtd_amazon and wtd_amazon_sp sheets for the broken date range.

The original report was generated before the gold ingestion ran, so the wtd_amazon
tab is empty. This script re-creates the same two tabs using the same function
the production WTD/MTD job uses, but with the data that now exists in ClickHouse.

Matches the screenshot:
  - wtd_amazon (02-06 to 07-06)      -> Amazon Ads campaign rollup
  - wtd_amazon_sp (02-06 to 07-06)   -> Amazon SP orders

Caller contract: start_date inclusive, end_date inclusive, days_lag=1
(so end_date - 1 = display end).
"""
from datetime import datetime

import pandas as pd

from amazon_entity_report import add_amazon_sheets_for_timeframe


def round_for_output(df: pd.DataFrame) -> pd.DataFrame:
    """Minimal local copy of dailyrollup.round_for_output (decimals -> floats, 2-dp)."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for col in out.select_dtypes(include=["object"]).columns:
        if len(out[col]) and all(hasattr(v, "as_tuple") or v is None for v in out[col].dropna().head(5)):
            out[col] = pd.to_numeric(out[col], errors="ignore")
    numeric_cols = out.select_dtypes(include=["number"]).columns
    out[numeric_cols] = out[numeric_cols].round(2)
    return out


def main():
    # End date the broken report used (sheet name "02-06 to 07-06" + days_lag=1 -> end=08-06)
    start_date = datetime(2026, 6, 2)
    end_date = datetime(2026, 6, 8)
    out_path = "wtd_amazon_regenerated.xlsx"

    print(f"Regenerating wtd_amazon sheets for {start_date.date()} .. {end_date.date()} "
          f"(display range will be shifted back 1 day for ingestion lag)")

    with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
        add_amazon_sheets_for_timeframe(
            writer,
            timeframe_key="wtd",
            start_date=start_date,
            end_date=end_date,
            round_for_output_fn=round_for_output,
            days_lag=1,
        )

    print(f"\nDone -> {out_path}")


if __name__ == "__main__":
    main()
