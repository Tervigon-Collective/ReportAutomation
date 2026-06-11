"""Inspect raw.amazon_ads_campaigns_daily_report (S3-backed) to find out
which report_dates actually exist in the source S3 files vs. what made it to gold."""
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
    query_limit=0,
    settings={"max_execution_time": 120},
)


def safe(label, sql):
    print("-" * 78)
    print(f"# {label}")
    print(f"  SQL: {sql.strip()[:140].replace(chr(10), ' ')}")
    try:
        r = client.query(sql)
        if not r.result_rows:
            print("  (no rows)")
            return
        if r.column_names:
            print("  cols:", r.column_names)
        for row in r.result_rows[:50]:
            print(" ", row)
        if len(r.result_rows) > 50:
            print(f"  ... ({len(r.result_rows)-50} more rows hidden)")
    except Exception as e:
        first = str(e).splitlines()[0][:160]
        print(f"  ERROR: {first}")


print(f"Today (IST): {today}, looking at last 30 days from {start}\n")

safe(
    "raw.amazon_ads_campaigns_daily_report - SHOW CREATE (to see S3 path)",
    "SHOW CREATE TABLE raw.amazon_ads_campaigns_daily_report",
)

safe(
    "raw.amazon_ads_campaigns_daily_report - columns",
    "SELECT name, type FROM system.columns "
    "WHERE database='raw' AND table='amazon_ads_campaigns_daily_report' ORDER BY position",
)

safe(
    "raw.amazon_ads_campaigns_daily_report - count() (whole table)",
    "SELECT count() FROM raw.amazon_ads_campaigns_daily_report",
)

safe(
    "raw.amazon_ads_campaigns_daily_report - per-day rows for the last 30 days",
    f"""
    SELECT report_date, count() AS rows, uniqExact(campaign_id) AS campaigns,
           sum(impressions) AS impressions, round(sum(cost), 2) AS spend
    FROM raw.amazon_ads_campaigns_daily_report
    WHERE report_date BETWEEN '{start}' AND '{today}'
    GROUP BY report_date
    ORDER BY report_date
    """,
)

safe(
    "raw.amazon_ads_campaigns_daily_report - min/max report_date",
    "SELECT count(), min(report_date), max(report_date) "
    "FROM raw.amazon_ads_campaigns_daily_report",
)

print()
print("=" * 78)
print("dbt sources/macros visible in ClickHouse?")
print("=" * 78)

safe(
    "Find any tables referencing 'amazon_ads' in their CREATE definition",
    "SELECT database, name FROM system.tables "
    "WHERE create_table_query ILIKE '%amazon_ads_campaigns_daily_report%' "
    "ORDER BY database, name",
)
