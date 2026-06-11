"""Verify Amazon Ads sheet is now at daily grain."""
from datetime import date, timedelta

from amazon_entity_report import build_amazon_ads_campaign_rollup, fetch_amazon_ads_gold

end = date.today() - timedelta(days=1)
start = end - timedelta(days=9)
s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

print(f"Window: {s} .. {e}\n")
ads = fetch_amazon_ads_gold(s, e)
print(f"raw rows: {len(ads)}, distinct dates: {ads['report_date'].nunique() if not ads.empty else 0},"
      f" distinct campaigns: {ads['campaign_name'].nunique() if not ads.empty else 0}\n")

roll = build_amazon_ads_campaign_rollup(ads)
print(f"rollup rows (incl Grand Total): {len(roll)}")
print(f"rollup columns: {list(roll.columns)}\n")
print(roll.to_string(index=False))
