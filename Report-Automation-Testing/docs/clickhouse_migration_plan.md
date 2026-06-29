# Migrate all report data sources to ClickHouse gold

## Resume here (session handoff)

- **Status:** plan handed off to **Ultraplan** (Claude Code on the web) for remote refinement.
- **Web session link:** https://claude.ai/code/session_01TZT45d8xm47brGnFyiS449?from=cli
  — open this to edit/iterate on the refined plan in the browser.
- **Next step:** when the cloud plan is ready, review it in the browser, then *teleport the
  plan back here* for implementation after approval. (Implementation has NOT started; repo is
  unchanged.)
- **Plan file (this doc):** `C:\Users\Gaurav\.claude\plans\fizzy-crafting-dolphin.md`
- **Working dir / branch:** `C:\Users\Gaurav\Downloads\Report-Automation-Testing` · branch
  `Testing`.
- **Canonical reference:** `Report-Automation-Testing/docs/data_sources_cannonical.md`.
- **Key decisions already locked (see rules below):** direct CH gold queries (no PG/API);
  channel + per-campaign COGS = `gross_cogs`; net profit always actual from
  `gold.fct_channel_pnl` / `gold.fct_daily_pnl`; entity report (ad-level + SKU + sales sheets)
  unchanged; Daily Meta Insights stays on Meta API.

## Context

Reporting (PDF / email / plots, Python on `node.seleric.cloud`) currently reads from a
mix of **PostgreSQL `dw_*_attribution` tables** and the **Node-Backend HTTP API**, which is
the origin of every discrepancy catalogued in `docs/data_sources_cannonical.md` (incomplete
Meta spend, unweighted CTR, missing `brand_id`, 3 conflicting COGS definitions, endpoints
that sometimes return *gross* profit labelled as net).

The goal: **every number that drives a report reads `gold.fct_*` directly via ClickHouse**,
with the canonical definitions/calculations from `docs/data_sources_cannonical.md`. PostgreSQL
and the Node-Backend API are removed as *report-data* sources. The only sources that
legitimately stay non-CH: the **live Daily Meta Insights** plot (Meta Marketing API), the
**AI summary** (Azure OpenAI) and **weather** (Open-Meteo); and the **PG `env_config` config
table** (configuration, not report data).

### Two hard rules from the user (override the doc where they differ)

1. **COGS shown** — *channel* and *per-campaign* breakdowns show **`gross_cogs`**
   (attribution view, doc §0 "gross_cogs = attribution models only"). All other reports
   (company P&L / KPI strip / totals) show **`net_cogs`** (`total_cogs`).
2. **Net profit is ALWAYS the actual net profit**, computed per-channel **from the ClickHouse
   P&L tables** — `gold.fct_channel_pnl` (per channel) and `gold.fct_daily_pnl` (company
   totals) — *not* `revenue − gross_cogs − spend`, and *not* taken from an endpoint that may
   return gross profit. Graphs (monthly performance, net ROAS, daily net profit) derive from
   these same P&L tables so they reflect the channel-wise segregation used in the PDF.
3. **Entity report = NO CHANGE.** The §7 Excel sheets — `generate_ad_level_report()` ad-level
   rows, the per-SKU sheets, and the order-level sales report — keep their current data
   sources untouched.

## Existing infrastructure to reuse

- **CH client:** `amazon_entity_report.get_clickhouse_client()` (clickhouse-connect; env
  `CLICKHOUSE_HOST/PORT/USER/PASSWORD/DATABASE/SECURE`). `clickhouse-connect` already in
  `requirements.txt`. Query pattern: `client.query(sql, parameters={...})` →
  `pd.DataFrame(result.result_rows, columns=result.column_names)`.
- **Brand id:** `CLICKHOUSE_BRAND_ID` env (helper already duplicated in `clickhouse_report.py`
  `_get_brand_id` and `channel_performance.py` `get_brand_id`).
- **Already on CH (keep, see edits below):** `clickhouse_report.py` (meta/google funnel +
  campaign), `channel_performance.py` (channel chart), `amazon_entity_report.py` (Amazon —
  doc §6, no change).
- **Canonical per-channel net profit:** `gold.fct_channel_pnl` — exposes `*_net_profit`,
  `*_ad_spend`, `*_attributed_revenue(_ex_gst)`, `*_attributed_orders`, `*_roas` per platform
  (verified via `cube_channel_pnl`). Confirm exact column names with `DESCRIBE
  gold.fct_channel_pnl` before writing SQL (it appears wide: one row per `brand_id × date`
  with `meta_*`/`google_*`/`organic_*` columns).
- **GST:** never hardcode `/1.18`; use `*_excl_tax`/`*_ex_gst` gold columns or the
  `GST_RATE`-derived divisor (`clickhouse_report.GST_DIVISOR`). NB `channel_performance.py:169`
  currently hardcodes `/1.18` — fix to the env divisor.

## Changes

### 1. New CH builder for the PDF channel table / KPI buckets
`api_data_fetcher.get_organized_metrics_for_pdf()` (lines ~905-1096) is 100% Node-API
(`ad_spend_by_hour` + `sales_unitCost_by_hour`) and feeds the PDF channel breakdown
(meta/google/organic/total buckets) consumed by `excel_generation.py`,
`WTD_MTD/timerange_wtd_mtd_rollup.py`, and `report_insights.py`.

- Add a builder (extend `clickhouse_report.py`, e.g. `get_channel_metrics_ch()`) returning the
  **same dict shape** (`meta`/`google`/`organic`/`total` with `sales, ad_spend, cogs,
  net_profit, gross_roas, net_roas, be_roas, order_count, ...`) so downstream renderers stay
  unchanged. Sources:
  - `sales` (ex-GST) + `order_count` per channel → `gold.fct_order_attribution`.
  - `ad_spend` per channel → `gold.fct_daily_pnl` (`meta_spend`/`google_spend`).
  - `cogs` → **`gross_cogs`** per channel from `gold.fct_order_items` (this is a channel table).
  - `net_profit` + `net_roas` → **`gold.fct_channel_pnl`** (actual). `gross_roas = sales/spend`,
    `be_roas` derived.
- Repoint `get_organized_metrics_for_pdf()` to call the builder (keep the function name/shape;
  drop the two API calls).

### 2. Channel Performance chart — `channel_performance.py`
- COGS subquery (lines 177-201): `sum(toFloat64(net_cost))` → **`gross_cogs`**; drop the
  `included_in_pnl_cogs` filter (gross = cost at placement, all statuses).
- `net_profit` / `net_roas` (lines 144-153, 264-269, 357): source from
  **`gold.fct_channel_pnl`** per platform instead of `revenue − cogs − ad_spend`. The bar
  chart then shows revenue (ex-GST), COGS (gross, for visibility), ad spend, **actual** net
  profit, orders. Net-ROAS trend panel uses daily `fct_channel_pnl`.
- Fix the `/1.18` hardcode at line 169 → env GST divisor.

### 3. Funnel + campaign — drop the PG fallback, always CH
`excel_generation.py` (~lines 804-845) selects CH vs PG via `USE_CLICKHOUSE_FUNNEL`
(default true). Make CH the only path (doc §3/§4) and delete the PG branches
(`excel_generation.get_google_funnel_metrics`, `dailyrollup.get_meta_funnel_metrics` /
`get_campaign_data`). `clickhouse_report.py` builders already produce the right shapes.
- Per-campaign COGS / ROAS in `get_campaign_data_ch()` (lines 258, 290-293) **stay on
  `gross_cogs`** (attribution view) per rule 1 — change its `net_cogs` to `gross_cogs`.
- `get_google_funnel_metrics_ch` interaction_rate currently `0.0`; compute weighted from
  `fct_google_ads_daily` per doc §4 bug #7 (clicks/impressions if `interactions` absent).

### 4. Plots — `plots.py`
Migrate API/PG-fed plots to CH; keep Daily Meta Insights on the Meta API.
- Daily Shopify net profit (`plot_daily_shopify_profit`, ~1013) and the
  `fetch_net_profit_from_db`/`fetch_net_profit_single_day` inputs → **`gold.fct_daily_pnl`**
  per `report_date` (actual `net_profit`).
- ROAS by date (`fetch_roas_data_from_api`, 208) → `net_sales_excl_tax /
  (meta_spend+google_spend)` per day from `fct_daily_pnl` (doc §8).
- Sales by state pie (`fetch_shopify_sales_by_state`, used at 1280) →
  `gold.fct_orders.shipping_province` (doc §8 query).
- Hourly sales last 7 days → CH hourly facts (`fct_*_hourly`) if feasible, else leave.
- Monthly performance / net-ROAS graphs → `fct_daily_pnl` (+ `fct_channel_pnl` for channel
  split), per rule 2.

### 5. Amazon in WTD/MTD — `WTD_MTD/timerange_wtd_mtd_rollup.py:991`
`fetch_amazon_data` (PG `amazon_product_metrics_daily`) → use the existing CH Amazon path
(`amazon_entity_report.get_amazon_clickhouse_summary` / gold Amazon facts, doc §6). Amazon
*entity sheets* themselves are unchanged.

### 6. Retire dead PG/API code (only once nothing references it)
After the above, the PG/API fetchers in `api_data_fetcher.py` (`fetch_net_profit`,
`fetch_net_profit_single_day`, `fetch_cogs`, `fetch_roas`, `fetch_order_count`,
`fetch_net_profit_from_db`, `fetch_db_sales`, `fetch_shopify_sales_by_state`,
`fetch_amazon_data`) are unused by reports — remove or leave clearly deprecated. **Do NOT
remove** `fetch_marketing_hourly` and `fetch_shopify_sales_orders_detail`: they still feed the
**entity report** (`dailyrollup.py:1513/1619/1764/1896/1940`, `generate_ad_level_report`) which
must not change. Drop the `USE_CLICKHOUSE_FUNNEL` flag.

## The entity-report boundary (must stay unchanged)
Leave entirely on current sources: `excel_generation.generate_ad_level_report` (542),
`excel_generation.fetch_product_metrics` (239), and the SKU / ad-level / order-sales builders
in `dailyrollup.py` that consume `fetch_marketing_hourly` + `fetch_shopify_sales_orders_detail`.
When editing `WTD_MTD/timerange_wtd_mtd_rollup.py`, verify each `fetch_marketing_hourly` call
(e.g. line 1208) — migrate only the channel/campaign *summary* paths, never the entity/SKU
sheet paths.

## Critical files
- `clickhouse_report.py` — new `get_channel_metrics_ch()`; campaign COGS → gross; google
  interaction_rate fix.
- `api_data_fetcher.py` — repoint `get_organized_metrics_for_pdf()` to CH; deprecate PG/API
  fetchers (keep the two entity-report fetchers).
- `channel_performance.py` — gross COGS, actual net profit from `fct_channel_pnl`, GST fix.
- `excel_generation.py` — drop `USE_CLICKHOUSE_FUNNEL` PG branches; CH-only funnel/campaign.
- `plots.py` — net-profit / ROAS-by-date / sales-by-state → CH; keep Meta-API insights plot.
- `WTD_MTD/timerange_wtd_mtd_rollup.py` — Amazon → CH; audit marketing-hourly usages.
- `dailyrollup.py` — remove now-dead PG funnel/campaign builders only (keep SKU/entity paths).

## Verification
1. `DESCRIBE gold.fct_channel_pnl` and `gold.fct_order_items` (confirm `gross_cogs`,
   `net_cogs`, platform/date columns) via `get_clickhouse_client()` or the
   `cube_channel_pnl` / `cube_query` MCP tools.
2. Run `python scripts/run_hourly.py` (daily PDF + entity Excel + plots) and
   `python scripts/run_daily.py` (WTD/MTD) against a known date; confirm no PG/Node-API report
   calls fire (grep logs for `BASE_URL`/psycopg) except config, weather, and the Meta-insights
   plot.
3. Reconcile a sample day: channel/campaign COGS = `sum(gross_cogs)`; per-channel net profit =
   `fct_channel_pnl.*_net_profit`; company net profit = `fct_daily_pnl` waterfall; cross-check
   against `cube_channel_pnl` / `cube_canonical_pnl` MCP output for the same brand/date.
4. Diff the **entity report** workbook before/after — it must be byte-equivalent (proves the
   no-change boundary held).
