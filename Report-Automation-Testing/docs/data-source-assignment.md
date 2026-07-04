# Data Source Assignment Table

Unified per-metric data requirement list for mix-and-match source selection (PostgreSQL, ClickHouse, API).

---

## Section 1: KPI Strip (PDF + Email) — currently 100% API

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Total Revenue | API | `shopify_orders.total_price_amount` | `fct_order_attribution.shopify_revenue` | `/api/sales` or `/api/sales_unitCost_by_hour` | PG=ALL orders; CH=attributed only; API=ALL orders |
| Total Ad Spend | API | `dw_meta_ads_attribution.spend` + `dw_google_ads_attribution.spend` | `fct_daily_pnl.meta_spend + google_spend` | `/api/ad_spend` or `/api/ad_spend_by_hour` | PG under-counts Meta spend ~40% |
| Net Profit | API (derived) | `fetch_net_profit_from_db()` | `fct_daily_pnl.net_profit` | `/api/net_profit` or `/api/net_profit_single_day` | See COGS discrepancy below |
| Blended ROAS | API (derived) | Derived from PG | Derived from CH | `/api/v1/historical/dashboard` | Use dashboard net/gross ROAS fields |
| Order Count | API | `shopify_orders` count | `fct_order_attribution` count | `/api/order_count` | PG=ALL; CH=attributed only |

---

## Section 2: Channel Performance (PDF + Email) — currently 100% API

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Revenue per channel (Meta/Google/Organic) | API | `dw_*_attribution.attributed_orders_revenue` | `fct_order_attribution` by channel | `/api/sales_unitCost_by_hour` (returns per-channel) | Only API splits by channel correctly |
| Ad Spend per channel | API | Same as above | `fct_daily_pnl` | `/api/ad_spend_by_hour` (facebookSpend/googleSpend) | PG Meta spend is incomplete |
| COGS per channel | API | `dw_*_attribution.attributed_orders_cogs` | `fct_order_items.net_cogs` or `net_cost` | `/api/sales_unitCost_by_hour` (unit_cost_meta/google/organic) | 3 different COGS definitions exist! |
| Orders per channel | API | `dw_*_attribution.attributed_orders_count` | `fct_order_attribution` count | `/api/sales_unitCost_by_hour` | |

---

## Section 3: Meta Funnel (PDF) — toggled by `USE_CLICKHOUSE_FUNNEL`

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Impressions | CH (if flag=true) | `dw_meta_ads_attribution.impressions` | `fct_meta_ads_daily.impressions` | **N/A** | |
| Clicks | CH (if flag=true) | `dw_meta_ads_attribution.clicks` | `fct_meta_ads_daily.clicks` | **N/A** | |
| CTR | CH (if flag=true) | `AVG(dw_meta_ads_attribution.ctr)` | `sum(clicks)/sum(impressions)` | **N/A** | PG=unweighted avg; CH=weighted (correct) |
| Landing Page Views | CH (if flag=true) | `dw_meta_ads_attribution.action_landing_page_view` | `fct_meta_ads_daily.landing_page_views` | **N/A** | |
| Add to Cart | CH (if flag=true) | `dw_meta_ads_attribution.action_*_add_to_cart` | `fct_meta_ads_daily.add_to_cart` | **N/A** | |
| Initiate Checkout | CH (if flag=true) | `dw_meta_ads_attribution.action_*_initiate_checkout` | `fct_meta_ads_daily.initiate_checkout` | **N/A** | |
| Orders (purchases) | CH (if flag=true) | `dw_meta_ads_attribution.attributed_orders_count` | `fct_order_attribution` count | **N/A** | |

---

## Section 4: Google Funnel (PDF) — toggled by `USE_CLICKHOUSE_FUNNEL`

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Clicks | CH (if flag=true) | `dw_google_ads_attribution.clicks` | `fct_google_ads_daily.clicks` | **N/A** | |
| CTR | CH (if flag=true) | `AVG(dw_google_ads_attribution.ctr)*100` (unweighted!) | `sum(clicks)/sum(impressions)*100` (weighted) | **N/A** | **PG bug: unweighted avg** |
| Interaction Rate | CH (if flag=true) | `AVG(dw_google_ads_attribution.interaction_rate)*100` | **Hardcoded 0.0** | **N/A** | **CH bug: hardcoded to 0** |
| Orders | CH (if flag=true) | `dw_google_ads_attribution.attributed_orders_count` | `fct_order_attribution` count | **N/A** | 3-level PG fallback: attribution → conversionTracking → Google Ads API |

---

## Section 5: Campaign Data (PDF segments + WTD/MTD top campaigns) — toggled by `USE_CLICKHOUSE_FUNNEL`

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Campaign Name | CH (if flag=true) | `dw_meta_ads_attribution.campaign_name` | `fct_meta_ads_daily.campaign_name` | **N/A** | |
| Campaign Spend | CH (if flag=true) | `dw_meta_ads_attribution.spend` (incomplete) | `fct_meta_ads_daily.spend` (canonical) | **N/A** | PG spend only for campaigns with attributed orders |
| Campaign Revenue | CH (if flag=true) | `dw_meta_ads_attribution.attributed_orders_revenue` | `fct_order_attribution.shopify_revenue` | **N/A** | |
| Campaign COGS | CH (if flag=true) | `dw_meta_ads_attribution.attributed_orders_cogs` | `fct_order_items.net_cost` (with pnl filter) | **N/A** | CH is more consistent (uses `included_in_pnl_cogs=1`) |
| Campaign Orders | CH (if flag=true) | `dw_meta_ads_attribution.attributed_orders_count` | `fct_order_attribution` count | **N/A** | |

---

## Section 6: Amazon Report (WTD/MTD) — always CH

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Amazon Revenue | CH | **N/A** | `fct_amazon_sp_orders` + `fct_amazon_ads_daily` | **N/A** | No PG equivalent |
| Amazon Ad Spend | CH | **N/A** | `fct_amazon_ads_daily` | **N/A** | |
| Amazon COGS | CH | **N/A** | `fct_amazon_sp_order_pnl` | **N/A** | |
| Amazon ROAS | CH (derived) | **N/A** | Derived | **N/A** | |

---

## Section 7: Entity Report Excel — always PG

| Metric | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Meta ad-level row data (impressions, clicks, spend, revenue, COGS per ad) | PG | `dw_meta_ads_attribution` (hourly rows) | `fct_meta_ads_daily` | `/api/ad_spend` (summary only) | API only returns aggregates, not ad-level rows |
| Google ad-level row data | PG | `dw_google_ads_attribution` | `fct_google_ads_daily` | **N/A** | |
| Organic row data | PG | `dw_organic_attribution` | `fct_order_attribution` (organic) | **N/A** | |
| SKU matching (unit_price, unit_cogs) | PG | `shopify_product_variants` + `amazon_product_metrics_daily` | `fct_order_items` | `/api/product_spend/all` | Azure SKU API also available |
| Sales report (order-level detail) | PG | `shopify_orders` | `fct_order_attribution` + `fct_order_items` | **N/A** | CH has order-level but no shipping info |

---

## Section 8: Plots — mixed PG + CH + Meta API

| Plot | Current Source | PG Source | CH Source | API Endpoint | Notes |
|---|---|---|---|---|---|
| Daily Meta Insights | Meta Marketing API | — | — | Meta Insights API directly | |
| Daily Shopify Net Profit | PG | `shopify_orders` + `fetch_net_profit_from_db()` | `fct_daily_pnl` (aggregated) | `/api/net_profit` | Could switch to API or CH |
| Hourly Sales Last 7 Days | PG | `shopify_orders` | **N/A** | `/api/sales_unitCost_by_hour` | Could switch to API |
| Sales by State Pie | PG | `shopify_orders.ship_province` | **N/A** | **N/A** | No CH or API equivalent |
| Channel Performance Bar | CH | `dw_*_attribution` (possible) | `fct_daily_pnl` (current) | `/api/sales` + `/api/cogs` + `/api/ad_spend` | Could switch to API |
| ROAS by Date | API | — | — | `/api/v1/historical/time-patterns` | Hourly/daily ROAS series from Node-Backend |

---

## Section 9: Other Attachments — PG + External APIs

| Data | Current Source | PG Source | External API | Notes |
|---|---|---|---|---|
| Ad Recommendations | PG `ad_recommendations` table | **Yes** | **N/A** | Latest batch by `created_at` |
| Meta Activity Log | PG `meta_ad_activity_log` | **Yes** | Meta Graph API (ingestion) | |
| AI Summary | — | — | Azure OpenAI | |
| Weather Campaign | PG + Open-Meteo API | `env_config` | Open-Meteo | |

---

## USE_CLICKHOUSE_FUNNEL Impact Summary

| Area | When `true` | When `false`/absent |
|---|---|---|
| Meta funnel (impressions, clicks, LPV, ATC, IC, orders, CTR, bounce, CVR) | CH `gold.fct_meta_ads_daily` + `gold.fct_order_attribution` | PG `dw_meta_ads_attribution` |
| Google funnel (clicks, CTR, interaction_rate, orders) | CH `gold.fct_google_ads_daily` + `gold.fct_order_attribution` | PG `dw_google_ads_attribution` (3-level fallback: PG → conversionTracking → Google Ads API) |
| Campaign data (PDF segments, WTD top campaigns) | CH `gold.fct_meta_ads_daily` + `gold.fct_order_attribution` + `gold.fct_order_items` | PG `dw_meta_ads_attribution` + `dw_google_ads_attribution` + `dw_organic_attribution` |
| Channel performance bar chart | **Always** CH `gold.fct_daily_pnl` | Same (always CH) |
| Amazon reports | **Always** CH | Same (always CH) |
| API metrics (KPI strip, channel table) | **Not affected** — always API | Same |
| Entity Report Excel sheets | **Not affected** — always PG `dw_*_attribution` | Same |
| Sales report / plots | **Not affected** — always PG `shopify_orders` | Same |
| Meta API spend & campaign data | **Not affected** — always Meta Marketing API | Same |

---

## Known Bugs / Discrepancies to Resolve

| # | Issue | Affected Metrics | Recommendation |
|---|---|---|---|
| 1 | PG Meta spend ~40% under (only attributed campaigns) | Meta spend, ROAS, CPP | **Use CH or API for Meta spend** |
| 2 | PG revenue = ALL orders; CH = attributed only | Revenue, Net Profit | Pick one definition per use case |
| 3 | 3 COGS definitions (no filter vs `included_in_pnl_cogs=1` vs `unit_cost*qty`) | COGS, Net Profit, ROAS | Standardize on `included_in_pnl_cogs=1` (CH `net_cost`) |
| 4 | PG Google CTR = unweighted `AVG(ctr)` | Google CTR | **Use CH** (weighted) or fix PG query |
| 5 | GST hardcoded `/1.18` in `channel_performance.py` vs env var in CH path | All GST-affected values | **Use env var everywhere** |
| 6 | PG queries missing `brand_id` filter | All PG attribution queries | **Add `brand_id` filter** or switch to CH/API |
| 7 | CH `interaction_rate` hardcoded `0.0` | Google interaction rate | **Fix CH query** or fall back to PG |
