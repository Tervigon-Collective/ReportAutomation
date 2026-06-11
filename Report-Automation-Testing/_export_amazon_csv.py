"""Export Amazon SP line-item P&L data as CSV for a fixed date range.

Reuses the production fetchers from amazon_entity_report so the CSV reflects
the exact same business rules as the WTD/MTD report:
  - Canceled orders zeroed at SQL.
  - Line-item granularity (one row per amazon_order_id x seller_sku).
  - fulfillment_channel joined from the orders table.
  - Order-level P&L (gross / refunds / commission / closing / shipping /
    tax_withheld / net_payout / cogs / gross_profit) allocated down to the
    line item proportionally to line revenue share. Sums per order are
    preserved; per-line gross_margin_pct equals the order-level margin.

Date range and brand are configurable below. Defaults match the explicit
request for brand_id=20, 2026-04-01 .. 2026-06-07.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from amazon_entity_report import (
    _build_sp_line_items_display,
    fetch_amazon_sp_items_gold,
    fetch_amazon_sp_order_pnl_gold,
    fetch_amazon_sp_orders_gold,
)


START_DATE = datetime(2026, 4, 1)
END_DATE = datetime(2026, 6, 7)
BRAND_ID: int | None = 20


def main() -> None:
    start_str = START_DATE.strftime("%Y-%m-%d")
    end_str = END_DATE.strftime("%Y-%m-%d")
    brand_suffix = f"_brand{BRAND_ID}" if BRAND_ID is not None else ""
    out_path = f"amazon_sp_line_items_pnl_{start_str}_to_{end_str}{brand_suffix}.csv"

    print(
        f"Fetching Amazon SP P&L data for {start_str} .. {end_str}"
        + (f" (brand_id={BRAND_ID})" if BRAND_ID is not None else "")
    )

    sp_orders_df = fetch_amazon_sp_orders_gold(start_str, end_str, brand_id=BRAND_ID)
    sp_items_df = fetch_amazon_sp_items_gold(start_str, end_str, brand_id=BRAND_ID)
    sp_pnl_df = fetch_amazon_sp_order_pnl_gold(start_str, end_str, brand_id=BRAND_ID)

    line_items = _build_sp_line_items_display(sp_items_df, sp_orders_df, sp_pnl_df)

    if line_items.empty:
        print("No Amazon SP data for this range.")
        return

    numeric_cols = line_items.select_dtypes(include=["number"]).columns
    line_items[numeric_cols] = line_items[numeric_cols].round(2)

    line_items.to_csv(out_path, index=False)
    print(f"\nSaved {len(line_items)} line items -> {out_path}")

    # Quick totals for sanity check.
    print("\n--- Totals (allocated, summed across all line items) ---")
    money_cols = [
        "gross", "refunds", "commission", "closing", "shipping",
        "tax_withheld", "net_payout", "cogs", "gross_profit",
    ]
    for col in money_cols:
        if col in line_items.columns:
            total = pd.to_numeric(line_items[col], errors="coerce").fillna(0).sum()
            print(f"  {col:>14}: {total:>14,.2f}")


if __name__ == "__main__":
    main()
