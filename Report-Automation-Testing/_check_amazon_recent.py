"""Check Amazon ad data availability for the most recent dates.

Used to decide whether the WTD/MTD Amazon end-date should still be `today - 1`
or can safely be `today` after the WTD-window change.
"""
import os
from datetime import date, datetime, timedelta

import pytz
from dotenv import load_dotenv
import clickhouse_connect

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")
today_ist = datetime.now(IST).date()
start = today_ist - timedelta(days=9)
end = today_ist

print(f"Today (IST): {today_ist}")
print(f"Checking report_date in [{start} .. {end}] (last 10 days incl. today)\n")

client = clickhouse_connect.get_client(
    host=os.getenv("CLICKHOUSE_HOST"),
    port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
    username=os.getenv("CLICKHOUSE_USER"),
    password=os.getenv("CLICKHOUSE_PASSWORD"),
    database=os.getenv("CLICKHOUSE_DATABASE", "gold"),
    secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    connect_timeout=int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "60")),
)
print(f"ClickHouse server: {client.server_version}\n")

print("=== gold.fct_amazon_ads_campaigns_daily (per-day) ===")
print(f"{'report_date':12} {'rows':>8} {'campaigns':>10} {'impressions':>14} {'clicks':>10} {'spend':>14}")
r = client.query(f"""
    SELECT report_date,
           count() AS rows,
           uniqExact(campaign_id) AS campaigns,
           sum(impressions) AS impressions,
           sum(clicks) AS clicks,
           round(sum(cost), 2) AS spend
    FROM gold.fct_amazon_ads_campaigns_daily
    WHERE report_date BETWEEN '{start}' AND '{end}'
    GROUP BY report_date
    ORDER BY report_date
""")
have = {row[0]: row for row in r.result_rows}
for d in (start + timedelta(days=i) for i in range((end - start).days + 1)):
    if d in have:
        rd, rows, camp, imp, clk, sp = have[d]
        print(f"{str(rd):12} {rows:>8} {camp:>10} {imp:>14} {clk:>10} {sp:>14}")
    else:
        print(f"{str(d):12} {'(no rows)':>58}")

print("\n=== overall freshness ===")
r = client.query(
    "SELECT count(), min(report_date), max(report_date), max(_gold_created_at) "
    "FROM gold.fct_amazon_ads_campaigns_daily"
)
total, mn, mx, last_ingest = r.result_rows[0]
print(f"  total rows         : {total:,}")
print(f"  min report_date    : {mn}")
print(f"  max report_date    : {mx}  (lag from today = {(today_ist - mx).days} day(s))")
print(f"  last _gold_created_at: {last_ingest}")
