"""Use the _path virtual column of the S3-backed raw table to discover which
dt= partitions exist in S3 for amazon_ads campaigns_daily_report.

S3 layout:
  seleric/raw/brand_id=*/platform=amazon_ads/entity=campaigns_daily_report/dt=YYYY-MM-DD/*.parquet
"""
import os
import re
from collections import Counter
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv
import clickhouse_connect

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")
today = datetime.now(IST).date()
start = today - timedelta(days=29)

# Fresh session id so we don't collide with the previous timeout
import uuid

client = clickhouse_connect.get_client(
    host=os.getenv("CLICKHOUSE_HOST"),
    port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
    username=os.getenv("CLICKHOUSE_USER"),
    password=os.getenv("CLICKHOUSE_PASSWORD"),
    database=os.getenv("CLICKHOUSE_DATABASE", "gold"),
    secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    connect_timeout=int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "60")),
    session_id=f"diag-{uuid.uuid4()}",
    settings={"max_execution_time": 120},
)

print(f"Today (IST): {today}; window {start} .. {today}\n")

# Pull just the file paths (cheap, parquet metadata only).
sql = """
    SELECT _path
    FROM raw.amazon_ads_campaigns_daily_report
    GROUP BY _path
    ORDER BY _path
"""
print("Listing S3 parquet partitions (this reads only metadata)...")
r = client.query(sql)
paths = [row[0] for row in r.result_rows]
print(f"Found {len(paths)} parquet files in S3.\n")

dt_re = re.compile(r"/dt=(\d{4}-\d{2}-\d{2})/")
brand_re = re.compile(r"/brand_id=(\d+)/")

dt_counter = Counter()
brand_counter = Counter()
brand_x_dt = Counter()
for p in paths:
    m_dt = dt_re.search(p)
    m_br = brand_re.search(p)
    dt = m_dt.group(1) if m_dt else "?"
    br = m_br.group(1) if m_br else "?"
    dt_counter[dt] += 1
    brand_counter[br] += 1
    brand_x_dt[(br, dt)] += 1

print(f"Distinct dt= partitions overall: {len(dt_counter)}")
print(f"Distinct brand_id partitions   : {len(brand_counter)}\n")

print("=== Partitions present in the last 30 days, per dt ===")
present_dates = set()
for d in (start + timedelta(days=i) for i in range((today - start).days + 1)):
    key = d.strftime("%Y-%m-%d")
    n = dt_counter.get(key, 0)
    if n:
        present_dates.add(key)
        brands_for_day = sorted({b for (b, dd), _ in brand_x_dt.items() if dd == key})
        print(f"  {key}  files={n:>3}  brands={brands_for_day}")
    else:
        print(f"  {key}  (NO partition in S3)")

print("\n=== Most recent 10 dt partitions present (any time, sorted desc) ===")
for dt in sorted(dt_counter, reverse=True)[:10]:
    print(f"  {dt}  files={dt_counter[dt]}")

# Also dump the actual gold dates for comparison.
print("\n=== Comparing S3 vs gold for the same window ===")
r = client.query(
    f"SELECT DISTINCT toString(report_date) FROM gold.fct_amazon_ads_campaigns_daily "
    f"WHERE report_date BETWEEN '{start}' AND '{today}' ORDER BY report_date"
)
gold_dates = {row[0] for row in r.result_rows}

print(f"{'date':12}  s3?  gold?")
for d in (start + timedelta(days=i) for i in range((today - start).days + 1)):
    key = d.strftime("%Y-%m-%d")
    s3 = "YES" if key in present_dates else "no "
    gd = "YES" if key in gold_dates else "no "
    flag = ""
    if key in present_dates and key not in gold_dates:
        flag = "  <-- in S3 but NOT loaded to gold"
    print(f"  {key}   {s3}   {gd}{flag}")
