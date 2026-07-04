import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv()

from api_data_fetcher import fetch_marketing_hourly
from dailyrollup import build_channel_summary_from_marketing_df
from dashboard_stats import build_pdf_api_metrics, fetch_general_statistics
from channel_performance import reconcile_roas_with_pdf_metrics

d = "2026-07-02"
brand_id = int(os.getenv("CLICKHOUSE_BRAND_ID", "20"))
company_id = int(os.getenv("DASHBOARD_COMPANY_ID", "19"))

df = fetch_marketing_hourly(d, d)
attr = build_channel_summary_from_marketing_df(df)
print("=== PDF CHANNEL (marketing hourly attribution) ===")
for ch in ("meta", "google", "organic"):
    m = attr[ch]
    print(ch, "sales=", round(m["sales"], 2), "orders=", m.get("order_count", m.get("quantity")))

stats = fetch_general_statistics(brand_id, company_id, d, d, prefer_api=True)
api = build_pdf_api_metrics(stats)
for ch in ("meta", "google", "organic"):
    api[ch] = attr[ch]

print("\n=== PDF AFTER CHANNEL_FROM_ATTRIBUTION overlay ===")
for ch in ("meta", "google", "organic", "amazon", "total"):
    m = api[ch]
    print(ch, "sales=", m["sales"], "orders=", m.get("order_count", m.get("quantity")))
ch_sum = sum(api[c]["sales"] for c in ("meta", "google", "organic", "amazon"))
print("channel sum", round(ch_sum, 2), "total", api["total"]["sales"], "gap", round(api["total"]["sales"] - ch_sum, 2))

recon = reconcile_roas_with_pdf_metrics(api, d, d, brand_id=brand_id)
print("\n=== RECONCILIATION ===")
for row in recon["channels"]:
    status = "OK" if row["revenue_match"] and row["roas_match"] else "Review"
    print(
        f"{row['label']}: pdf={row['pdf_sales']}, calc={row['calc_revenue']}, "
        f"rev_delta={row['sales_delta_pct']}%, roas_delta={row['roas_delta']}, status={status}"
    )
print("Attributed Total:", recon["total"])
