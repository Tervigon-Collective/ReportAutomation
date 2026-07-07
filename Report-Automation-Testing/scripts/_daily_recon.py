import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import pytz
from api_data_fetcher import fetch_marketing_hourly, get_organized_metrics_for_pdf, fetch_canonical_pnl_totals
from dailyrollup import build_channel_summary_from_marketing_df

d = sys.argv[1] if len(sys.argv) > 1 else "2026-07-07"
ist = pytz.timezone("Asia/Kolkata")
start = ist.localize(datetime.strptime(d, "%Y-%m-%d"))

attr = build_channel_summary_from_marketing_df(fetch_marketing_hourly(d, d))
dash = get_organized_metrics_for_pdf(start, start)
canon = fetch_canonical_pnl_totals(d, d)

print(f"=== DAILY {d}: attribution (entity excel basis) vs dashboard API ===")
for ch in ("meta", "google", "organic"):
    a, b = attr[ch], dash[ch]
    delta = b["sales"] - a["sales"]
    print(
        f"  {ch:7s} attr sales={a['sales']:>12,.0f} orders={a['order_count']:>3d}"
        f" | dash sales={b['sales']:>12,.0f} orders={b['order_count']:>3d}"
        f" | delta={delta:+,.0f}"
    )

attr_sum = sum(attr[c]["sales"] for c in attr)
print(f"\n  attr channel sum = {attr_sum:,.0f}")
print(f"  dash total       = {dash['total']['sales']:,.0f}")
print(f"  canonical        = {canon.get('revenue', 0):,.0f}")
print(f"  dash amazon      = {dash.get('amazon', {}).get('sales', 0):,.0f}")
