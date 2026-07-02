import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv()

from api_data_fetcher import (
    fetch_historical_dashboard,
    fetch_historical_sales_by_region,
    fetch_marketing_hourly,
)
from dailyrollup import build_channel_summary_from_marketing_df
from dashboard_stats import build_pdf_api_metrics, fetch_general_statistics, _clickhouse_stats
from channel_performance import fetch_channel_performance, aggregate_channel_performance

d = "2026-07-02"
brand_id = int(os.getenv("CLICKHOUSE_BRAND_ID", "20"))
company_id = int(os.getenv("DASHBOARD_COMPANY_ID", "19"))

print("=" * 60)
print("ORDER COUNT BY SOURCE —", d)
print("=" * 60)

# 1) Historical dashboard API
dash = fetch_historical_dashboard(d, d) or {}
ob = dash.get("orders_breakdown") or {}
ns = dash.get("net_sales_breakdown") or {}
print("\n1. API /historical/dashboard orders_breakdown (Channel Performance chart):")
for ch in ("meta", "google", "organic"):
    print(f"   {ch}: {ob.get(ch, 0)} orders, sales={ns.get(ch, 0)}")
amz = ob.get("amazon")
amz_orders = amz.get("orders", 0) if isinstance(amz, dict) else amz
print(f"   amazon: {amz_orders}")
ch_sum = sum(int(ob.get(c, 0) or 0) for c in ("meta", "google", "organic"))
print(f"   Shopify channel sum: {ch_sum}")
print(f"   total_orders (headline): {dash.get('total_orders')}")

# 2) Sales by state
region = fetch_historical_sales_by_region(d, d) or {}
regions = region.get("regions") or region.get("sales_by_region") or region.get("data") or []
if isinstance(region, dict) and not regions:
    regions = region.get("breakdown") or []
state_orders = 0
state_sales = 0
if isinstance(regions, list):
    for r in regions:
        state_orders += int(r.get("order_count") or r.get("orders") or 0)
        state_sales += float(r.get("net_sales") or r.get("total_sales") or r.get("sales") or 0)
print("\n2. API /historical/sales-by-region:")
print(f"   Sum of state orders: {state_orders}")
print(f"   Sum of state sales: {state_sales:.2f}")
print(f"   API total_orders field: {region.get('total_orders', 'n/a')}")

# 3) PDF channel table (attribution overlay)
attr = build_channel_summary_from_marketing_df(fetch_marketing_hourly(d, d))
stats = fetch_general_statistics(brand_id, company_id, d, d, prefer_api=True)
api = build_pdf_api_metrics(stats)
for ch in ("meta", "google", "organic"):
    api[ch] = attr[ch]
print("\n3. PDF Channel Performance table (marketing hourly attribution):")
for ch in ("meta", "google", "organic", "amazon", "total"):
    m = api[ch]
    oc = m.get("order_count", m.get("quantity", 0))
    print(f"   {ch}: {oc} orders, sales={m['sales']}")
ch_sum_pdf = sum(api[c].get("order_count", api[c].get("quantity", 0)) for c in ("meta", "google", "organic", "amazon"))
print(f"   Channel sum: {ch_sum_pdf}")

# 4) ClickHouse canonical attribution
ch = _clickhouse_stats(brand_id, d, d)
print("\n4. ClickHouse canonical attribution (dashboard modal basis):")
for ch_name in ("meta", "google", "organic"):
    c = ch["channels"][ch_name]
    print(f"   {ch_name}: {c['order_count']} orders, sales={c['sales']}")
print(f"   total_orders (master query): {ch['totals']['total_orders']}")
print(f"   amazon_orders: {ch['totals']['amazon_orders']}")

# 5) Channel performance plot aggregate
cp = fetch_channel_performance(d, d, brand_id=brand_id)
agg = aggregate_channel_performance(cp)
print("\n5. Channel Performance chart (plot_channel_performance_daily):")
if not agg.empty:
    for _, row in agg.iterrows():
        print(f"   {row['platform']}: {int(row.get('attributed_orders', 0))} orders, rev={row.get('gross_revenue_excl_gst', 0)}")
    print(f"   Sum orders: {int(agg['attributed_orders'].sum())}")
