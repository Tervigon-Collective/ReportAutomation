"""
Test Amazon ClickHouse integration: fetch Ads + SP data for the last 3 days.
"""
from datetime import date, timedelta

import pandas as pd
from tabulate import tabulate

from amazon_entity_report import (
    fetch_amazon_ads_gold,
    fetch_amazon_sp_items_gold,
    fetch_amazon_sp_orders_gold,
)


def main():
    end = date.today()
    start = end - timedelta(days=3)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"Amazon ClickHouse Integration Test")
    print(f"Date range: {start_str} to {end_str} (last 3 days)")
    print(f"{'='*60}\n")

    # --- Amazon Ads ---
    print("--- Amazon Ads (gold.fct_amazon_ads_campaigns_daily) ---")
    ads_df = fetch_amazon_ads_gold(start_str, end_str)
    if ads_df.empty:
        print("  No ads data found.")
    else:
        daily = (
            ads_df.groupby("report_date")
            .agg(
                campaigns=("campaign_id", "nunique"),
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
                spend=("spend", "sum"),
                orders=("orders", "sum"),
                sales=("sales", "sum"),
            )
            .reset_index()
        )
        print(tabulate(daily, headers="keys", tablefmt="simple", floatfmt=".2f", showindex=False))
        print(f"\n  Top campaigns:")
        top = (
            ads_df.groupby("campaign_name")
            .agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"), spend=("spend", "sum"))
            .sort_values("spend", ascending=False)
            .head(5)
            .reset_index()
        )
        print(tabulate(top, headers="keys", tablefmt="simple", floatfmt=".2f", showindex=False))

    # --- Amazon SP Orders ---
    print("\n--- Amazon SP Orders (gold.fct_amazon_sp_orders) ---")
    sp_orders = fetch_amazon_sp_orders_gold(start_str, end_str)
    if sp_orders.empty:
        print("  No SP order data found.")
    else:
        daily_sp = (
            sp_orders.groupby("purchase_date")
            .agg(orders=("amazon_order_id", "count"), revenue=("order_total", "sum"))
            .reset_index()
        )
        print(tabulate(daily_sp, headers="keys", tablefmt="simple", floatfmt=".2f", showindex=False))
        print(f"\n  Sample orders:")
        sample = sp_orders[["purchase_date", "amazon_order_id", "order_status", "order_total", "currency_code"]].head(5)
        print(tabulate(sample, headers="keys", tablefmt="simple", floatfmt=".2f", showindex=False))

    # --- Amazon SP Order Items ---
    print("\n--- Amazon SP Order Items (gold.fct_amazon_order_items) ---")
    sp_items = fetch_amazon_sp_items_gold(start_str, end_str)
    if sp_items.empty:
        print("  No SP item data found.")
    else:
        daily_items = (
            sp_items.groupby("purchase_date")
            .agg(items=("order_item_id", "count"), qty=("quantity_ordered", "sum"), revenue=("item_price_amount", "sum"))
            .reset_index()
        )
        print(tabulate(daily_items, headers="keys", tablefmt="simple", floatfmt=".2f", showindex=False))

    # --- Save Excel snapshot ---
    out_path = f"amazon_clickhouse_test_{end_str}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        if not ads_df.empty:
            ads_df.to_excel(writer, sheet_name="Ads Campaigns", index=False)
        if not sp_orders.empty:
            sp_orders.to_excel(writer, sheet_name="SP Orders", index=False)
        if not sp_items.empty:
            sp_items.to_excel(writer, sheet_name="SP Order Items", index=False)
    print(f"\nExcel snapshot saved: {out_path}")

    print(f"\n{'='*60}")
    print("Test complete.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
