# Canonical Data Sources — Dashboard, DB & Reporting

> **Purpose.** One source of truth for every dashboard / report metric. For each metric this
> document gives **the ClickHouse gold-layer query to use** (preferred), or — where a metric is
> only reachable through the backend — **the real Node-Backend API endpoint** (which itself reads
> the same gold tables). The intent is that **DB ⇄ Dashboard ⇄ Reporting all read identical
> definitions** and reconcile to the rupee.
>
> **Scope note:** Ad Recommendations and Meta Activity Log are intentionally **excluded** (no data
> fetch required — see the assignment table Section 9).
>
> **Last updated:** 2026-06-29 · **Owner:** Node-Backend `docs/` · Companion: [`FORMULA_REFERENCE.md`](./FORMULA_REFERENCE.md)

---

## 0. The consistency rule (read first)

There are three places data is produced, and historically they disagreed:

| Layer | What it is | What it *should* read |
|---|---|---|
| **My DB** | ClickHouse `gold.fct_*` (synced from dbt-trino Iceberg) | — it is the source of truth |
| **Dashboard** | Node-Backend `/api/v1/*` routes | The `gold.fct_*` tables (already does for CH routes) |
| **Reporting** (PDF / Email, Python on `node.seleric.cloud`) | Was: mix of PG `dw_*_attribution` + CH + ad-platform APIs | **Switch to `gold.fct_*` or the Node-Backend CH endpoints** |

**Decision for every metric below: prefer the ClickHouse `gold.fct_*` query.** The PostgreSQL
`dw_*_attribution` paths are the origin of every discrepancy in the assignment table (incomplete
Meta spend, unweighted CTR, missing `brand_id` filters, 3 COGS definitions). They are **deprecated
for reporting** — do not read them for any dashboard/report number.

Two non-negotiable conventions, applied in **every** query:

1. **Timezone = Asia/Kolkata (IST).** Always filter on the `*_date` columns (`report_date`,
   `order_date`, `session_date`, `purchase_date`) — never raw UTC timestamps.
2. **Always filter `brand_id`** (and `company_id` where the table has it). This is bug #6 in the
   assignment table — the PG path frequently omitted it.

### COGS — there is exactly one canonical definition

The assignment table lists "3 COGS definitions." We standardize:

| Name | Expression | Use for |
|---|---|---|
| **`net_cogs`** (= `total_cogs`) | `sum(fct_order_items.net_cogs)` | **All P&L / dashboard / report COGS.** Pre-computed per-status (ACTIVE = product+ship+pack+gateway; RETURN = rto+ship+pack+gateway, no product; CANCEL = gateway only). |
| `gross_cogs` | `sum(fct_order_items.gross_cogs)` | **Attribution models only** (cost at placement, all statuses). Per-channel ROAS attribution. |
| `product_cost` | `fct_daily_pnl.product_cost` | **P&L waterfall "COGS" line only** — product cost, *no* logistics. Do not compare to `net_cogs`. |

Helper expressions live in `src/integrations/attributionQueryHelpers.js`
(`netCogsSumExpr`, `grossCogsSumExpr`). Gift cards (`is_gift_card = 1`) and test rows
(`is_test = 1`) are always excluded.

---

## 1. KPI Strip (PDF + Email) — was 100% API → **use ClickHouse `gold.fct_daily_pnl`**

**Canonical source:** the dashboard already computes the entire KPI strip from `gold.fct_daily_pnl`
in `src/services/pnlService.js`. Reporting should run the **same query** (or call the endpoint).

**Backend endpoint (already CH-backed):** `GET /api/v1/pnl/summary?brand_id&company_id&startDate&endDate`
— `endDate` is **exclusive**. No cache. Source: `pnlRoutes.js` → `pnlService.getPnlMetrics()`.

### Canonical query (verbatim from `pnlService.js`)

```sql
WITH base AS (
  SELECT *
  FROM gold.fct_daily_pnl FINAL
  WHERE brand_id = {brandId:Int64}
    AND report_date >= toDate({startDate:Date})
    AND report_date <  toDate({endDateExclusive:Date})
)
SELECT
  round(sum(toFloat64(ifNull(gross_revenue, 0))), 4)             AS total_sales,
  round(sum(toFloat64(ifNull(total_sales_incl_tax, 0))), 4)     AS total_sales_incl_tax,
  round(sum(toFloat64(ifNull(net_revenue, 0))), 4)              AS net_sales,
  round(sum(toFloat64(ifNull(returns_excl_tax, 0))), 4)         AS returns,
  round(sum(toFloat64(ifNull(cancelled_revenue, 0))), 4)        AS cancelled,
  round(sum(toFloat64(ifNull(total_refund_amount, 0))), 4)      AS refunds_cash,
  round(sum(toFloat64(ifNull(voided_revenue, 0))), 4)           AS voided_revenue,
  round(sum(toFloat64(ifNull(gross_profit, 0))), 4)             AS gross_profit,
  (SELECT round(sum(toFloat64(ifNull(spend, 0))), 4)
     FROM gold.fct_meta_ads_daily
    WHERE brand_id = {brandId:Int64}
      AND report_date >= toDate({startDate:Date})
      AND report_date <  toDate({endDateExclusive:Date}))       AS meta_ads_cost,
  (SELECT round(sum(toFloat64(ifNull(spend, 0))), 4)
     FROM gold.fct_google_ads_daily
    WHERE brand_id = {brandId:Int64}
      AND report_date >= toDate({startDate:Date})
      AND report_date <  toDate({endDateExclusive:Date}))       AS google_ads_cost,
  round(sum(toFloat64(ifNull(product_cost, 0))), 4)             AS cogs,
  round(sum(toFloat64(ifNull(packaging_cost, 0))), 4)           AS packaging_cost,
  round(sum(toFloat64(ifNull(shipping_cost, 0))), 4)            AS shipping_cost,
  round(sum(toFloat64(ifNull(payment_gateway_fees, 0))), 4)     AS payment_gateway_fees,
  round(sum(toFloat64(ifNull(rto_cost, 0))), 4)                 AS rto_logistics_cost,
  round(sum(toFloat64(ifNull(total_operating_cost, 0))), 4)     AS total_operating_cost,
  round(sum(toFloat64(ifNull(total_discounts_excl_tax, 0))), 4) AS discounts,
  round(sum(toFloat64(ifNull(gross_sales_excl_tax, 0))), 4)     AS gross_sales_excl_tax,
  round(sum(toFloat64(ifNull(net_sales_excl_tax, 0))), 4)       AS net_sales_excl_tax,
  round(sum(toFloat64(ifNull(total_tax_collected, 0))), 4)      AS taxes
FROM base
```

### KPI strip metric mapping

| KPI metric | Canonical source | Formula / column |
|---|---|---|
| **Total Revenue** | `gold.fct_daily_pnl.gross_revenue` | `sum(gross_revenue)` → `total_sales` (ALL orders; for ex-GST headline use `net_sales_excl_tax`) |
| **Total Ad Spend** | `gold.fct_meta_ads_daily.spend` + `gold.fct_google_ads_daily.spend` | `meta_ads_cost + google_ads_cost` — **fixes bug #1** (PG Meta spend ~40% short) |
| **Net Profit** | derived from the query above | `net_sales_excl_tax − net_cogs − total_ad_spend − operating costs`. Simplified waterfall: `gross_profit − total_ad_spend`. See §0 COGS note. |
| **Blended ROAS** | derived | `net_sales_excl_tax / (meta_ads_cost + google_ads_cost)` — **do not** use the old `/api/roas_by_date`; derive from this query so it reconciles with the strip |
| **Order Count** | `fct_order_attribution` (see §1b) | count of non-test, non-voided orders |

> **Net Profit canonical waterfall** (gold semantic layer): Revenue `total_sales_ex_gst`
> − `total_cogs` = Gross profit − `total_ad_spend` = Contribution − (packaging + shipping +
> gateway + RTO) = **Net profit**. Mirror this exactly in reporting.

### 1b. Order Count — canonical query

```sql
SELECT toInt64(count()) AS order_count
FROM fct_order_attribution AS a
WHERE a.brand_id = {brandId:Int64}
  AND a.order_date >= toDate({startDate:String})
  AND a.order_date <= toDate({endDate:String})
  AND coalesce(a.is_test, 0) = 0
  AND lowerUTF8(trimBoth(coalesce(a.order_status, ''))) != 'voided'
```

> **Revenue definition pin (bug #2).** "Total Revenue" on the KPI strip = `fct_daily_pnl.gross_revenue`
> (ALL orders). The *attributed* revenue figure (Meta/Google channel cards) comes from
> `fct_order_attribution` and is a **different number by design** — never reconcile the two 1:1.

---

## 2. Channel Performance (PDF + Email) — was 100% API → **use ClickHouse**

Per-channel splits come from `fct_order_attribution` (channel bucket) joined to `fct_order_items`
(COGS) and the ad-spend gold tables. Implemented in
`src/integrations/historicalAnalytics/analyticsClickhouse.js`.
**Backend endpoint:** `GET /api/v1/historical/dashboard?brand_id&company_id&start_date&end_date`.

Channel bucket expression (`platformChannelSqlExpr`, from `attributionQueryHelpers.js`):

```sql
multiIf(
  lower(trim(lt_platform)) IN ('meta','facebook','instagram','fb','ig'), 'meta',
  lower(trim(lt_platform)) = 'google',  'google',
  lower(trim(lt_platform)) = 'organic', 'organic',
  'other')
```

### Revenue per channel

```sql
SELECT
  lower(coalesce(a.channel, 'organic')) AS channel,
  coalesce(sum(toFloat64(coalesce(o.net_revenue_excl_tax, 0))), 0) AS channel_net_sales
FROM fct_order_attribution AS a
INNER JOIN fct_orders AS o ON o.brand_id = a.brand_id AND o.order_id = a.order_id
WHERE a.brand_id = {brandId:Int64}
  AND a.order_date >= toDate({startDate:String})
  AND a.order_date <= toDate({endDate:String})
  AND coalesce(a.is_test, 0) = 0
  AND lowerUTF8(trimBoth(coalesce(a.order_status, ''))) != 'voided'
GROUP BY channel
```

### Ad spend per channel

```sql
-- Meta
SELECT sum(toFloat64(coalesce(spend,0))) AS meta_spend
FROM gold.fct_meta_ads_daily
WHERE brand_id = {brandId:Int64} AND report_date >= toDate({startDate:Date}) AND report_date <= toDate({endDate:Date});
-- Google
SELECT sum(toFloat64(coalesce(spend,0))) AS google_spend
FROM gold.fct_google_ads_daily
WHERE brand_id = {brandId:Int64} AND report_date >= toDate({startDate:Date}) AND report_date <= toDate({endDate:Date});
-- Organic has no ad spend.
```

### COGS per channel — uses `net_cogs` (canonical, fixes bug #3)

```sql
WITH order_channel AS (
  SELECT a.brand_id, a.order_id, any(<platformChannelSqlExpr>) AS channel
  FROM fct_order_attribution AS a
  WHERE a.brand_id = {brandId:Int64}
    AND a.order_date BETWEEN toDate({startDate:String}) AND toDate({endDate:String})
    AND coalesce(a.is_test,0)=0
    AND lowerUTF8(trimBoth(coalesce(a.order_status,''))) != 'voided'
  GROUP BY a.brand_id, a.order_id
)
SELECT
  coalesce(oc.channel, 'other')           AS channel,
  sum(toFloat64(coalesce(i.net_cogs, 0))) AS channel_cogs
FROM fct_order_items AS i
LEFT JOIN order_channel AS oc ON oc.brand_id = i.brand_id AND oc.order_id = i.order_id
WHERE i.brand_id = {brandId:Int64}
  AND i.order_date BETWEEN toDate({startDate:String}) AND toDate({endDate:String})
  AND coalesce(i.is_gift_card, 0) = 0
GROUP BY channel
```

### Orders per channel

```sql
SELECT
  <platformChannelSqlExpr> AS channel,
  toInt64(count())         AS channel_orders
FROM fct_order_attribution AS a
WHERE a.brand_id = {brandId:Int64}
  AND a.order_date BETWEEN toDate({startDate:String}) AND toDate({endDate:String})
  AND coalesce(a.is_test,0)=0
  AND lowerUTF8(trimBoth(coalesce(a.order_status,''))) != 'voided'
GROUP BY channel
```

> **GST (bug #5).** Use the env-driven GST divisor everywhere — do **not** hardcode `/1.18`. The
> gold `*_excl_tax` columns already net GST out; prefer them over dividing inclusive figures.

---

## 3. Meta Funnel (PDF) — drop `USE_CLICKHOUSE_FUNNEL` → **always ClickHouse**

Two gold tables feed the Meta funnel. Implemented in
`src/integrations/metaFunnel/metaFunnelAttributionClickhouse.js` (ad metrics) and
`metaFunnelSiteFunnelClickhouse.js` (on-site funnel stages).

**Impressions / Clicks / CTR / Spend** → `gold.fct_meta_ads_daily`:

```sql
SELECT
  coalesce(sum(f.impressions), 0) AS impressions,
  coalesce(sum(f.clicks), 0)      AS clicks,
  coalesce(sum(f.spend), 0)       AS spend,
  if(sum(f.impressions) > 0, sum(f.clicks)/sum(f.impressions)*100, 0) AS ctr,  -- weighted (correct)
  if(sum(f.clicks) > 0,      sum(f.spend)/sum(f.clicks), 0)           AS cpc,
  if(sum(f.impressions) > 0, sum(f.spend)/sum(f.impressions)*1000, 0) AS cpm
FROM fct_meta_ads_daily AS f
WHERE f.brand_id = {brandId:UInt32}
  AND f.report_date >= toDate({startDate:String})
  AND f.report_date <= toDate({endDate:String})
  AND f.ad_id IS NOT NULL AND length(trimBoth(toString(f.ad_id))) > 0
```

> **CTR is weighted** here (`sum(clicks)/sum(impressions)`), not `avg(ctr)` — this is the correct
> definition and resolves the PG unweighted-average bug (#4) for Meta.

**Landing Page Views / Add to Cart / Initiate Checkout** → `gold.fct_session_funnel`
(session grain; sum the `stage_*` flags):

```sql
SELECT
  count()                                   AS sessions,
  sum(toUInt8(coalesce(f.stage_product_view, 0)))    AS product_views,      -- LPV / PDP view
  sum(toUInt8(coalesce(f.stage_add_to_cart, 0)))     AS add_to_cart,
  sum(toUInt8(coalesce(f.stage_reached_checkout, 0))) AS checkout_start,    -- initiate checkout
  sum(toUInt8(coalesce(f.stage_purchased, 0)))       AS session_purchases,
  if(count() > 0, 100.0*avg(toUInt8(coalesce(f.page_view_count,0) <= 1
       AND coalesce(f.stage_purchased,0)=0)), 0)     AS bounce_rate
FROM fct_session_funnel AS f
WHERE f.brand_id = {brandId:Int64}
  AND f.session_date >= toDate({startDate:String})
  AND f.session_date <= toDate({endDate:String})
  AND <sessionFunnelPlatformFilter('f','meta')>
  AND f.ad_id IS NOT NULL AND toString(f.ad_id) != ''
```

**Orders (purchases)** → `gold.fct_order_attribution` (Meta platform, attributed), **not** the Meta
pixel. Same pattern as §5 campaign orders:

```sql
SELECT countDistinct(oa.order_id) AS attributed_orders_count,
       sum(toFloat64(coalesce(oa.net_revenue, oa.gross_revenue, 0))) AS attributed_orders_revenue
FROM fct_order_attribution AS oa
WHERE oa.brand_id = {brandId:UInt32}
  AND oa.order_date >= toDate({startDate:String})
  AND oa.order_date <= toDate({endDate:String})
  AND lowerUTF8(trimBoth(coalesce(oa.lt_platform,''))) IN ('meta','facebook','instagram','fb','ig')
  AND oa.lt_ad_id IS NOT NULL AND length(trimBoth(toString(oa.lt_ad_id))) > 0
  AND coalesce(oa.is_test,0)=0
  AND lowerUTF8(trimBoth(coalesce(oa.order_status,''))) != 'voided'
```

**Backend endpoint:** `GET /api/v1/meta-attribution?brand_id&start_date&end_date` (and `/meta-funnel`
for the on-site funnel). Both read the gold tables above.

---

## 4. Google Funnel (PDF) — drop `USE_CLICKHOUSE_FUNNEL` → **always ClickHouse**

Implemented in `src/integrations/googleAttribution/analyticsClickhouse.js`.
**Clicks / Impressions / CTR / Spend** → `gold.fct_google_ads_daily`:

```sql
SELECT
  sum(toFloat64(coalesce(d.spend, 0)))       AS spend,
  sum(toFloat64(coalesce(d.impressions, 0))) AS impressions,
  sum(toFloat64(coalesce(d.clicks, 0)))      AS clicks
FROM fct_google_ads_daily AS d
WHERE d.brand_id = {brandId:Int64}
  AND d.report_date >= toDate({startDate:String})
  AND d.report_date <= toDate({endDate:String})
GROUP BY d.campaign_id, d.adset_id, d.ad_id, toDate(d.report_date)
```

Derived (in `analyticsClickhouse.js`, totals stage — **weighted**, fixes bug #4):

```
ctr         = total_clicks / total_impressions * 100
average_cpc = total_spend  / total_clicks
average_cpm = total_spend  / total_impressions * 1000
```

**Interaction Rate** — bug #7: the gold table does not carry a reliable per-row `interaction_rate`
and the old CH path hardcoded `0.0`. **Canonical fix:** compute it weighted from the same gold
table — `interaction_rate = total_interactions / total_impressions * 100` if
`fct_google_ads_daily.interactions` is populated; otherwise treat clicks as interactions
(`clicks / impressions * 100`). Do **not** read PG `dw_google_ads_attribution.interaction_rate`,
and do **not** emit a hardcoded 0.

**Orders** → `gold.fct_order_attribution` with `lt_platform = 'google'` (same shape as §3 Meta
orders). This replaces the 3-level PG fallback (attribution → conversionTracking → Google Ads API).

**Backend endpoint:** `GET /api/v1/google-attribution?brand_id&start_date&end_date`.

---

## 5. Campaign Data (PDF segments + WTD/MTD top campaigns) — **always ClickHouse**

**Canonical:** `gold.fct_meta_ads_daily` (spend — *canonical, complete*) joined to
`gold.fct_order_attribution` (revenue/orders) and `gold.fct_order_items` (COGS).
Implemented in `src/integrations/metaFunnel/metaFunnelAttributionClickhouse.js`
(`fetchAdMetricsClickhouse`) — aggregate up to campaign with `GROUP BY campaign_id`.

```sql
SELECT
  f.campaign_id, any(f.campaign_name) AS campaign_name,
  sum(f.spend)       AS campaign_spend,        -- fct_meta_ads_daily = complete spend (bug #1 fix)
  sum(f.impressions) AS impressions,
  sum(f.clicks)      AS clicks,
  att.attributed_orders_count   AS campaign_orders,
  att.attributed_orders_revenue AS campaign_revenue,
  att.attributed_orders_cogs    AS campaign_cogs
FROM fct_meta_ads_daily AS f
LEFT JOIN (
  SELECT toString(oa.lt_campaign_id) AS campaign_id,
         countDistinct(oa.order_id) AS attributed_orders_count,
         sum(toFloat64(coalesce(oa.net_revenue, oa.gross_revenue, 0))) AS attributed_orders_revenue,
         sum(oi_cogs.cogs) AS attributed_orders_cogs
  FROM fct_order_attribution AS oa
  LEFT JOIN (
    SELECT brand_id, order_id, sum(toFloat64(coalesce(net_cogs,0))) AS cogs   -- canonical net_cogs
    FROM fct_order_items
    WHERE brand_id = {brandId:UInt32} AND coalesce(is_gift_card,0)=0
    GROUP BY brand_id, order_id
  ) AS oi_cogs ON oi_cogs.order_id = oa.order_id AND oi_cogs.brand_id = oa.brand_id
  WHERE oa.brand_id = {brandId:UInt32}
    AND oa.order_date BETWEEN toDate({startDate:String}) AND toDate({endDate:String})
    AND lowerUTF8(trimBoth(coalesce(oa.lt_platform,''))) IN ('meta','facebook','instagram','fb','ig')
    AND coalesce(oa.is_test,0)=0
    AND lowerUTF8(trimBoth(coalesce(oa.order_status,''))) != 'voided'
  GROUP BY toString(oa.lt_campaign_id)
) AS att ON toString(att.campaign_id) = toString(f.campaign_id)
WHERE f.brand_id = {brandId:UInt32}
  AND f.report_date BETWEEN toDate({startDate:String}) AND toDate({endDate:String})
GROUP BY f.campaign_id, att.attributed_orders_count, att.attributed_orders_revenue, att.attributed_orders_cogs
ORDER BY campaign_spend DESC
```

> Campaign **spend** must come from `fct_meta_ads_daily` (every campaign that ran), **not** the PG
> `dw_meta_ads_attribution.spend` (only campaigns with attributed orders → ~40% undercount, bug #1).
> Campaign **COGS** uses canonical `net_cogs`.

---

## 6. Amazon Report (WTD/MTD) — **always ClickHouse** (already correct, no change)

No PostgreSQL equivalent exists; keep on CH. Full formulas in
[`FORMULA_REFERENCE.md` §4–§5, §19](./FORMULA_REFERENCE.md).
**Backend endpoints:** `GET /api/v1/amazon-organic-orders` and `GET /api/v1/amazon-attribution`
(both `?brand_id&start_date&end_date`).

| Metric | Gold table | Note |
|---|---|---|
| Amazon Revenue | `fct_amazon_sp_orders.order_total` (= `effective_gross_revenue`) | Gross Sales card |
| Amazon Ad Spend | `fct_amazon_ads_campaigns_daily.cost` | exposed as `spend` |
| Amazon COGS | `fct_amazon_sp_order_pnl.cogs_product` (product) | Total COGS card = `amazon_fees + cogs_product` |
| Amazon Net Payout | `fct_amazon_sp_order_pnl.effective_net_payout` | after fees & refunds |
| Amazon ROAS / Net Profit | derived | `roas = order_total/spend` (TACOS); `net_profit = net_payout − cogs_product − spend` |

> Amazon fee columns are stored **negative** — resolve fees per order (`Math.abs` each component),
> then sum. Never `abs(sum(total_amazon_fees))`. See [`FORMULA_REFERENCE.md` §18](./FORMULA_REFERENCE.md).

---

## 7. Entity Report Excel — was "always PG" → **use ClickHouse gold (ad-level rows)**

The reason this stayed on PG was "API returns aggregates, not ad-level rows." ClickHouse gold
**does** expose ad-level rows, so move it to CH for consistency with everything above.

| Sheet | Canonical CH source | Grain |
|---|---|---|
| Meta ad-level rows (impr, clicks, spend, revenue, COGS) | `gold.fct_meta_ads_daily` (+ `fct_order_attribution` for rev/orders, `fct_order_items` for COGS) | one row per `ad_id × report_date` |
| Google ad-level rows | `gold.fct_google_ads_daily` (+ attribution) | `ad_id × report_date` |
| Organic rows | `gold.fct_order_attribution` (`lt_platform='organic'`) + `fct_orders` | order |
| SKU matching (unit_price, unit_cogs) | `gold.fct_order_items` joined `gold.dim_sku` (`sku.unit_cost`, `sku.price`) | sku |
| Sales report (order-level) | `gold.fct_orders` + `gold.fct_order_items` | order / line |

SKU + line-item pattern (from `analyticsChannelClickhouse.js`):

```sql
SELECT
  oi.sku_id,
  max(coalesce(sku.sku, oi.sku))                 AS sku,
  max(coalesce(sku.product_name, oi.product_title)) AS product_name,
  max(toFloat64(coalesce(sku.price, 0)))         AS mrp,
  max(toFloat64(coalesce(sku.unit_cost, oi.unit_cost, 0))) AS unit_cost,
  sum(oi.quantity)                               AS total_quantity,
  sum(toFloat64(coalesce(oi.net_item_amount_after_refunds, 0))) AS total_revenue,
  sum(toFloat64(coalesce(oi.gross_cogs, 0)))     AS total_cogs
FROM fct_order_items AS oi
LEFT JOIN dim_sku AS sku ON oi.sku_id = sku.sku_id AND oi.brand_id = sku.brand_id
WHERE oi.brand_id = {brandId:UInt32}
  AND toString(oi.order_id) IN {orderIds:Array(String)}
  AND coalesce(oi.is_gift_card,0)=0 AND oi.is_test=0
GROUP BY oi.sku_id
```

> **Caveat:** CH order-level data has **no shipping address detail** that the PG sales report
> carried. If the Excel needs ship-to fields, `fct_orders` carries `shipping_city /
> shipping_province / shipping_pincode` (see §2 / `analyticsChannelClickhouse.js`) — confirm these
> cover the report's columns before fully retiring the PG sheet.

---

## 8. Plots — mixed → consolidate on ClickHouse (+ Meta API only for live insights)

| Plot | Canonical source | Query / endpoint |
|---|---|---|
| Daily Shopify Net Profit | `gold.fct_daily_pnl` per day | §1 query grouped `GROUP BY report_date` |
| Hourly Sales Last 7 Days | `gold.fct_meta_ads_hourly` / order hourly facts, or endpoint | prefer CH; `fct_*_hourly` carry `hour_of_day` |
| Channel Performance Bar | `gold.fct_daily_pnl` (channel) | already always-CH — keep |
| ROAS by Date | `gold.fct_daily_pnl` per day | `net_sales_excl_tax / (meta_spend+google_spend)` per `report_date` — **replaces** the `/api/roas_by_date` node endpoint so it reconciles with the KPI strip |
| Sales by State Pie | `gold.fct_orders.shipping_province` | `sum(net_gmv) GROUP BY shipping_province` (CH replacement for the old PG-only plot) |
| Daily Meta Insights | **Meta Marketing API** (live) | only metric that legitimately stays on the ad-platform API — live creative insights not in gold |

Sales-by-state (CH replacement for the PG-only pie):

```sql
SELECT
  o.shipping_province AS state,
  sum(toFloat64(coalesce(o.net_gmv_after_refunds, o.net_gmv_after_refund, 0))) AS revenue,
  uniqExact(o.order_id) AS orders
FROM fct_orders AS o
WHERE o.brand_id = {brandId:UInt32}
  AND o.order_date BETWEEN toDate({startDate:String}) AND toDate({endDate:String})
  AND o.is_test = 0 AND o.is_cancelled = 0
GROUP BY state
ORDER BY revenue DESC
```

---

## 9. Excluded by request

- **Ad Recommendations** (`ad_recommendations`) — no fetch (per request).
- **Meta Activity Log** (`meta_ad_activity_log`) — no fetch (per request).
- **AI Summary** (Azure OpenAI) and **Weather Campaign** (Open-Meteo) — external, unchanged.

---

## 10. Bug-resolution scorecard

| # | Assignment-table bug | Resolution in this doc |
|---|---|---|
| 1 | PG Meta spend ~40% under | All spend from `gold.fct_meta_ads_daily` / `fct_google_ads_daily` (§1, §3, §5) |
| 2 | PG revenue = ALL vs CH = attributed | Pinned: KPI "Total Revenue" = `fct_daily_pnl.gross_revenue`; channel cards = attributed `fct_order_attribution` — documented as different by design (§1, §1b) |
| 3 | 3 COGS definitions | Standardized on `net_cogs` for P&L/reports; `gross_cogs` for attribution; `product_cost` for waterfall line only (§0) |
| 4 | PG Google/Meta CTR unweighted | Weighted `sum(clicks)/sum(impressions)` everywhere (§3, §4) |
| 5 | GST hardcoded `/1.18` | Use gold `*_excl_tax` columns / env GST divisor; never hardcode (§2) |
| 6 | PG missing `brand_id` | Every query filters `brand_id` (+ `company_id` where present) (§0) |
| 7 | CH `interaction_rate` hardcoded 0 | Compute weighted from `fct_google_ads_daily`; never emit 0 or read PG (§4) |

---

## 11. Endpoint quick-reference (all CH-backed, Node-Backend)

| Need | Endpoint | Gold table(s) |
|---|---|---|
| KPI strip / P&L | `GET /api/v1/pnl/summary` | `fct_daily_pnl`, `fct_meta_ads_daily`, `fct_google_ads_daily` |
| Channel performance | `GET /api/v1/historical/dashboard` | `fct_order_attribution`, `fct_orders`, `fct_order_items`, ad-spend facts |
| Meta funnel / campaigns | `GET /api/v1/meta-attribution`, `GET /api/v1/meta-funnel` | `fct_meta_ads_daily`, `fct_session_funnel`, `fct_order_attribution` |
| Google funnel | `GET /api/v1/google-attribution` | `fct_google_ads_daily`, `fct_order_attribution` |
| Organic / channel attribution | `GET /api/v1/channel-attribution` | `fct_orders`, `fct_order_items`, `dim_sku` |
| Amazon organic | `GET /api/v1/amazon-organic-orders` | `fct_amazon_sp_orders`, `fct_amazon_sp_order_pnl` |
| Amazon ads | `GET /api/v1/amazon-attribution` | `fct_amazon_ads_*_daily`, `fct_amazon_order_items` |

Common params: `brand_id` (required), `company_id`, `start_date`/`end_date` (P&L uses `startDate`/
`endDate`, **`endDate` exclusive**). All return `{ "success": true, "data": {...} }`.

> **The only sources that legitimately stay non-ClickHouse:** live ad-platform creative insights
> (Meta Marketing API for the Daily Meta Insights plot), and the external AI/Weather services.
> Everything that drives a number on the dashboard or in a report reads `gold.fct_*`.
