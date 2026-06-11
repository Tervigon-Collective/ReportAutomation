"""Verify the Amazon SP sheet now:
  1. Has no item_price / order_total / cogs columns.
  2. Exposes 'product_cost' instead of 'cogs'.
  3. Satisfies gross_profit == net_payout - product_cost on every line.
"""
from datetime import date, timedelta

import pandas as pd

from amazon_entity_report import (
    _build_sp_line_items_display,
    fetch_amazon_sp_items_gold,
    fetch_amazon_sp_order_pnl_gold,
    fetch_amazon_sp_orders_gold,
)


end = date.today() - timedelta(days=1)
start = end - timedelta(days=9)
s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
print(f"Window: {s} .. {e}\n")

orders = fetch_amazon_sp_orders_gold(s, e)
items = fetch_amazon_sp_items_gold(s, e)
pnl = fetch_amazon_sp_order_pnl_gold(s, e)

display = _build_sp_line_items_display(items, orders, pnl)

if display.empty:
    print("(no line items in this window)")
    raise SystemExit(0)

print(f"Rows: {len(display)}")
print(f"Columns: {list(display.columns)}\n")

forbidden = {"item_price", "order_total", "cogs"}
present_forbidden = forbidden & set(display.columns)
print(f"Forbidden columns present? {sorted(present_forbidden) or 'NONE (good)'}")
print(f"'product_cost' present?    {'YES (good)' if 'product_cost' in display.columns else 'NO'}\n")

# Identity check: gross_profit should equal net_payout - product_cost.
np_ = pd.to_numeric(display["net_payout"], errors="coerce").fillna(0.0)
pc = pd.to_numeric(display["product_cost"], errors="coerce").fillna(0.0)
gp = pd.to_numeric(display["gross_profit"], errors="coerce").fillna(0.0)
expected = np_ - pc
diff = (gp - expected).abs()
print(f"max |gross_profit - (net_payout - product_cost)| = {diff.max():.6f}")
print(f"mean diff = {diff.mean():.6f}")
print(f"rows where diff > 0.01 : {(diff > 0.01).sum()} / {len(display)}\n")

print("First 3 line items (relevant cols):")
cols = [
    "purchase_date", "amazon_order_id", "sku",
    "gross", "net_payout", "product_cost", "gross_profit", "gross_margin_pct",
]
cols = [c for c in cols if c in display.columns]
print(display[cols].head(3).to_string(index=False))
