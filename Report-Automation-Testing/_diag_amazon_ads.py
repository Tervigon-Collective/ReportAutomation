"""Diagnostic: query ClickHouse for the same date range as the empty wtd_amazon tab."""
import os
from datetime import date
from dotenv import load_dotenv
import clickhouse_connect

load_dotenv()

client = clickhouse_connect.get_client(
    host=os.getenv("CLICKHOUSE_HOST"),
    port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
    username=os.getenv("CLICKHOUSE_USER"),
    password=os.getenv("CLICKHOUSE_PASSWORD"),
    database=os.getenv("CLICKHOUSE_DATABASE", "gold"),
    secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    connect_timeout=int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "60")),
)

print(f"\nClickHouse connected. CLICKHOUSE_DATABASE env = {os.getenv('CLICKHOUSE_DATABASE')!r}")
print(f"server version: {client.server_version}\n")

# Date range matches the sheet name "wtd_amazon (02-06 to 07-06)" => 2026-06-02..2026-06-07
START, END = "2026-06-02", "2026-06-07"

print(f"=== gold.fct_amazon_ads_campaigns_daily for {START}..{END} ===")
r = client.query(f"""
    SELECT report_date,
           count() AS rows,
           uniqExact(campaign_id) AS campaigns,
           sum(impressions) AS impressions,
           sum(clicks) AS clicks,
           round(sum(cost), 2) AS spend
    FROM gold.fct_amazon_ads_campaigns_daily
    WHERE report_date BETWEEN '{START}' AND '{END}'
    GROUP BY report_date
    ORDER BY report_date
""")
for row in r.result_rows:
    print(" ", row)
if not r.result_rows:
    print("  (no rows)")

print(f"\n=== overall row count in gold.fct_amazon_ads_campaigns_daily ===")
r = client.query("SELECT count(), min(report_date), max(report_date) FROM gold.fct_amazon_ads_campaigns_daily")
print(" ", r.result_rows[0])

print(f"\n=== same query exactly as fetch_amazon_ads_gold uses ===")
r = client.query(
    """
        SELECT
            company_id, brand_id, shop_domain, campaign_id, campaign_name,
            campaign_type, campaign_status, report_date,
            COALESCE(impressions, 0) AS impressions,
            COALESCE(clicks, 0) AS clicks,
            COALESCE(cost, 0) AS spend,
            COALESCE(purchases_7d, 0) AS orders,
            COALESCE(sales_7d, 0) AS sales
        FROM gold.fct_amazon_ads_campaigns_daily
        WHERE report_date BETWEEN %(start_date)s AND %(end_date)s
        ORDER BY report_date, campaign_name
        LIMIT 5
    """,
    parameters={"start_date": START, "end_date": END},
)
print(f"  columns: {r.column_names}")
for row in r.result_rows:
    print(" ", row)
if not r.result_rows:
    print("  (no rows)")

print(f"\n=== latest 5 rows (any date) ===")
r = client.query("""
    SELECT report_date, campaign_name, impressions, clicks, cost, _gold_created_at
    FROM gold.fct_amazon_ads_campaigns_daily
    ORDER BY report_date DESC, _gold_created_at DESC
    LIMIT 5
""")
for row in r.result_rows:
    print(" ", row)
