"""Investigate the Jun 1-5 gap in gold.fct_amazon_ads_campaigns_daily.

Looks at:
  1. Available Amazon-related tables across all databases.
  2. Ingestion timestamps (_gold_created_at / _ingested_at) per report_date.
  3. Bronze/silver layers if present.
  4. Per-day row counts for the past 30 days.
"""
import os
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv
import clickhouse_connect

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")
today = datetime.now(IST).date()
start = today - timedelta(days=29)

client = clickhouse_connect.get_client(
    host=os.getenv("CLICKHOUSE_HOST"),
    port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
    username=os.getenv("CLICKHOUSE_USER"),
    password=os.getenv("CLICKHOUSE_PASSWORD"),
    database=os.getenv("CLICKHOUSE_DATABASE", "gold"),
    secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    connect_timeout=int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "60")),
)
print(f"Today (IST): {today}\n")

print("=" * 80)
print("1. All Amazon-related tables across databases")
print("=" * 80)
r = client.query("""
    SELECT database, name, engine, total_rows
    FROM system.tables
    WHERE name ILIKE '%amazon%'
       OR name ILIKE '%adv%amzn%'
       OR name ILIKE '%sp_%'
    ORDER BY database, name
""")
for db, name, eng, rows in r.result_rows:
    rows_disp = f"{rows:,}" if rows is not None else "?"
    print(f"  {db}.{name:50} engine={eng:25} rows={rows_disp}")

print()
print("=" * 80)
print("2. gold.fct_amazon_ads_campaigns_daily per-day rows + ingest time (last 30d)")
print("=" * 80)
r = client.query(f"""
    SELECT report_date,
           count() AS rows,
           uniqExact(campaign_id) AS campaigns,
           min(_gold_created_at) AS first_ingest,
           max(_gold_created_at) AS last_ingest
    FROM gold.fct_amazon_ads_campaigns_daily
    WHERE report_date >= '{start}'
    GROUP BY report_date
    ORDER BY report_date
""")
have = {row[0]: row for row in r.result_rows}
print(f"  {'date':12} {'rows':>6} {'camp':>6} {'first_ingest':>25} {'last_ingest':>25}")
for d in (start + timedelta(days=i) for i in range((today - start).days + 1)):
    if d in have:
        rd, rows, camp, fi, li = have[d]
        print(f"  {str(rd):12} {rows:>6} {camp:>6} {str(fi):>25} {str(li):>25}")
    else:
        print(f"  {str(d):12} {'(no rows)':>62}")

print()
print("=" * 80)
print("3. Distinct columns in gold.fct_amazon_ads_campaigns_daily")
print("=" * 80)
r = client.query("""
    SELECT name, type
    FROM system.columns
    WHERE database = 'gold' AND table = 'fct_amazon_ads_campaigns_daily'
    ORDER BY position
""")
for n, t in r.result_rows:
    print(f"  {n:30} {t}")

print()
print("=" * 80)
print("4. Look for upstream bronze/silver Amazon-ads sources")
print("=" * 80)
for guess in [
    "bronze.amazon_ads_campaigns_daily",
    "silver.amazon_ads_campaigns_daily",
    "bronze.fct_amazon_ads_campaigns_daily",
    "silver.fct_amazon_ads_campaigns_daily",
    "bronze.amazon_advertising_campaign_report",
    "raw.amazon_ads_campaigns_daily",
]:
    try:
        r = client.query(
            f"SELECT count(), min(report_date) AS min_d, max(report_date) AS max_d "
            f"FROM {guess} WHERE report_date >= '{start}'"
        )
        cnt, mn, mx = r.result_rows[0]
        print(f"  {guess:55} rows>={start}: {cnt:,}  min={mn}  max={mx}")
    except Exception as e:
        msg = str(e).splitlines()[0][:80]
        print(f"  {guess:55} (not accessible) {msg}")

print()
print("=" * 80)
print("5. Check if the empty days exist with rows but were filtered out")
print("=" * 80)
r = client.query(f"""
    SELECT count() AS rows,
           min(report_date) AS min_d,
           max(report_date) AS max_d,
           sum(impressions=0 AND clicks=0 AND cost=0) AS all_zero_rows
    FROM gold.fct_amazon_ads_campaigns_daily
    WHERE report_date BETWEEN '{today - timedelta(days=9)}' AND '{today}'
""")
print("  ", dict(zip(["rows", "min_d", "max_d", "all_zero_rows"], r.result_rows[0])))
