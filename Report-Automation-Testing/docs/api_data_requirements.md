# API Data Requirements — Report Automation

Maps each report module to Node-Backend `/api/v1` routes and dashboard-aligned formulas.

**Formula source of truth:** `Seleric_Dashboard/Node-Backend/docs/FORMULA_REFERENCE.md`

**Auth:** Firebase Bearer token (`FIREBASE_ID_TOKEN` or email/password auto-refresh)

**Tenant:** `brand_id` = `CLICKHOUSE_BRAND_ID`, `company_id` = `DASHBOARD_COMPANY_ID`

---

## A. PDF channel KPI table (`get_organized_metrics_for_pdf`)

| Report field | API route | API field / formula |
|--------------|-----------|---------------------|
| Channel sales | `GET /v1/historical/dashboard` | `net_sales_breakdown.{meta,google,organic}` |
| Ad spend | same | `ad_spend_breakdown` |
| COGS | same | `cogs_breakdown` |
| Net profit | same | `net_sales - cogs - ad_spend` per channel |
| Gross ROAS | computed | `gross_sales / ad_spend` |
| Net ROAS | computed | `(net_sales - cogs) / ad_spend` |
| BE ROAS | computed | `(cogs + ad_spend) / ad_spend` |

Implementation: `metric_calculators.channel_metrics_from_historical_dashboard()`

---

## B. Entity Excel sheets (`dailyrollup.py`)

| Legacy source | Replacement |
|---------------|-------------|
| `fetch_marketing_hourly` → PG `dw_*_attribution` | `GET /v1/meta-attribution`, `/google-attribution`, `/channel-attribution?channel=organic` |

Flattened by `api_response_transformers.flatten_all_attribution_to_hourly_df()`.

| PG column | API source |
|-----------|------------|
| `source`, `channel`, `date_start`, `hour` | Campaign → adset → ad → `daily_data` / `hourly_data` |
| `spend_cost`, `impressions`, `clicks` | Bucket metrics |
| `attributed_orders_*` | `metrics.attributed_orders_*` |
| `product_details`, `attributed_orders` | Ad node JSON |

**Time aggregation:** single-day → `hourly`; multi-day → `daily`.

---

## C. Amazon WTD/MTD (`amazon_entity_report.py`)

| Function | API route |
|----------|-----------|
| Ads campaign data | `GET /v1/amazon-attribution` (`campaigns[].daily_data`) |
| SP orders / P&L / line items | `GET /v1/amazon-attribution` → `orders[]` |
| Email summary | `GET /v1/historical/amazon/dashboard` or attribution `summary` |
| Legacy `fetch_amazon_data` | `GET /v1/historical/amazon/ads` |

Amazon COGS: `product_cost + |marketplace fees|` per `amazonOrderPnl.js`.

---

## D. Funnel + campaign PDF (`clickhouse_report.py`)

| Function | API route |
|----------|-----------|
| `get_meta_funnel_metrics_ch` | `GET /v1/meta-funnel` |
| `get_google_funnel_metrics_ch` | `GET /v1/google-attribution` |
| `get_campaign_data_ch` | `GET /v1/meta-attribution` |

---

## E. Channel performance chart (`channel_performance.py`)

| Data | API route |
|------|-----------|
| Daily channel rows | `GET /v1/historical/time-patterns` + `GET /v1/historical/dashboard` breakdowns |

---

## F. Plots (`plots.py`)

| Plot | API route |
|------|-----------|
| Daily net profit | `GET /v1/historical/time-patterns` |
| ROAS by date | same |
| Sales by state | `GET /v1/historical/sales-by-region` |
| Meta insights | Meta Graph API (unchanged) |

---

## G. General Statistics PDF (`dashboard_stats.py`)

| Data | API route |
|------|-----------|
| Top KPI strip | `GET /v1/historical/dashboard` |

---

## Environment flags

```env
BACKEND_API_BASE_URL=https://node.seleric.com/api
USE_API_ONLY=false          # true = no DB/ClickHouse fallbacks
USE_API_FALLBACK=true       # try API before legacy sources
```

---

## P&L date convention

`GET /v1/pnl/summary` uses **exclusive** `endDate`. Client adds +1 day via `pnl_end_exclusive()`.

---

## Verification scripts

```bash
python scripts/test_api_endpoints.py
python scripts/compare_api_vs_current.py --start 2026-03-01 --end 2026-03-07
```
