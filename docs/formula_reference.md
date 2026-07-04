# Analytics Platform Reference

> **For users, developers, and AI agents (Claude / Cursor).**
> This document covers every analytics platform integrated in the Node-Backend:
> API routes, middleware stack, database credential queries, data source queries, and all metric formulas.
>
> **Last updated:** 2026-06-20

---

## Table of Contents

1. [Quick Route Reference](#1-quick-route-reference)
2. [Shared Infrastructure](#2-shared-infrastructure)
3. [Shopify — Organic Ecommerce](#3-shopify--organic-ecommerce)
4. [Amazon SP — Organic Orders (ClickHouse)](#4-amazon-sp--organic-orders-clickhouse)
5. [Amazon Ads — Attribution (ClickHouse)](#5-amazon-ads--attribution-clickhouse)
6. [Meta Ads — Real-Time (Graph API)](#6-meta-ads--real-time-graph-api)
7. [Meta Attribution — Historical (ClickHouse)](#7-meta-attribution--historical-clickhouse)
8. [Google Ads — Real-Time (GAQL)](#8-google-ads--real-time-gaql)
9. [Google Attribution — Historical (ClickHouse)](#9-google-attribution--historical-clickhouse)
10. [GA4 Analytics](#10-ga4-analytics)
11. [Historical Dashboard (ClickHouse Gold Layer)](#11-historical-dashboard-clickhouse-gold-layer)
12. [Channel Attribution (ClickHouse)](#12-channel-attribution-clickhouse)
13. [P&L Aggregated Routes](#13-pl-aggregated-routes)
14. [Cross-Platform Derived Metrics](#14-cross-platform-derived-metrics)
15. [Terminology Cross-Reference](#15-terminology-cross-reference)
16. [Date Handling](#16-date-handling)
17. [Order Exclusion Rules](#17-order-exclusion-rules)
18. [Amazon Fee Signs Reference](#18-amazon-fee-signs-reference)
19. [Amazon Attribution — Two P&L Views](#19-amazon-attribution--two-pl-views)

---

## 1. Quick Route Reference

> For AI agents and developers: complete route map with file paths and data sources.


| Route Base                      | Method | Path           | Route File                            | Data Source                                                       | Cache                      |
| ------------------------------- | ------ | -------------- | ------------------------------------- | ----------------------------------------------------------------- | -------------------------- |
| `/api/v1/shopify/analytics`     | GET    | multiple       | `routes/shopifyAnalyticsRoutes.js`    | Shopify REST/GraphQL API                                          | 120 s dynamic              |
| `/api/v1/meta/analytics`        | GET    | multiple       | `routes/metaAnalyticsRoutes.js`       | Meta Graph API v21.0                                              | 120 s dynamic              |
| `/api/v1/google-ads/analytics`  | GET    | multiple       | `routes/googleAdsAnalyticsRoutes.js`  | Google Ads API v22 (GAQL)                                         | 120 s dynamic              |
| `/api/v1/ga4/analytics`         | GET    | multiple       | `routes/ga4AnalyticsRoutes.js`        | GA4 Data API                                                      | varies                     |
| `/api/v1/amazon-organic-orders` | GET    | `/`, `/bounds` | `routes/amazonOrganicOrdersRoutes.js` | ClickHouse `fct_amazon_sp_orders` + `fct_amazon_sp_order_pnl`     | 300 s fixed                |
| `/api/v1/amazon-attribution`    | GET    | `/`, `/bounds` | `routes/amazonAttributionRoutes.js`   | ClickHouse `fct_amazon_ads_*_daily`                               | 300 s fixed                |
| `/api/v1/meta-attribution`      | GET    | `/`            | `routes/metaAttributionRoutes.js`     | ClickHouse `fct_meta_ads_daily` / `fct_meta_ads_hourly`           | 300 s fixed                |
| `/api/v1/google-attribution`    | GET    | `/`            | `routes/googleAttributionRoutes.js`   | ClickHouse `fct_google_ads_daily` / `fct_google_campaigns_hourly` | 300 s fixed                |
| `/api/v1/historical`            | GET    | `/dashboard`   | `routes/historicalAnalyticsRoutes.js` | ClickHouse gold-layer tables                                      | 300 s dynamic              |
| `/api/v1/channel-attribution`   | GET    | `/`            | `routes/organicAttributionRoutes.js`  | ClickHouse `fct_order_attribution` + `fct_orders`                 | 300 s fixed                |
| `/api/v1/pnl`                   | GET    | multiple       | `routes/pnlRoutes.js`                 | ClickHouse `gold.fct_daily_pnl`                                   | none (no cache middleware) |
| `/api/v1/meta-funnel`           | GET    | multiple       | `routes/metaFunnelRoutes.js`          | Meta Graph API                                                    | varies                     |


> **Critical distinction:** `/meta/analytics` and `/google-ads/analytics` are **real-time API** routes.
> `/meta-attribution` and `/google-attribution` read from **ClickHouse historical tables**. They are different systems.

---

## 2. Shared Infrastructure

### 2.1 Credential Storage — PostgreSQL

All platform credentials (OAuth tokens, account IDs) live in one table:

```sql
-- Table: core.brand_envs (PostgreSQL)
id              SERIAL PRIMARY KEY
brand_id        INTEGER
company_id      INTEGER
platform        TEXT        -- 'shopify' | 'meta' | 'google-ads' | 'ga4' | 'amazon-sp' | 'amazon-ads'
account_id      TEXT        -- ad account ID / shop domain / customer ID
access_token    TEXT
refresh_token   TEXT
token_expiry    TIMESTAMP
shop_domain     TEXT        -- Shopify only
is_active       BOOLEAN
extra_data      JSONB       -- platform-specific overrides (currency, timezone, preferred_account_id, etc.)
installed_at    TIMESTAMP
updated_at      TIMESTAMP
```

Brand metadata is joined from:

```sql
-- Table: core.brands (PostgreSQL)
id              INTEGER PRIMARY KEY
company_id      INTEGER
timezone        TEXT        -- used for Shopify date interpretation
default_currency TEXT
```

### 2.2 Standard Middleware Stack

All analytics routes apply this middleware chain (order matters):

```
Request
  1. authenticate             — JWT Bearer; sets req.auth (brandId, companyId, userId, timezone, currency)
  2. tenantContextMiddleware  — validates brand+company context; sets req.tenantContext
  3. requestDeduplicationMiddleware — drops duplicate in-flight requests (same URL+params)
  4. responseCacheMiddleware  — in-memory cache; TTL varies by platform
  5. validateBrandAccess      — req.auth.brandId must match query brand_id
  6. validateXxxIntegration   — loads credentials from core.brand_envs into req.xxxCredentials
  7. validateDateRange        — start_date and end_date required (except /orders/last, /customer-journey)
  8. Route Handler
```

Routes without cache middleware: `/api/v1/pnl` (no cache applied).
Routes without integration validation: `/api/v1/amazon-organic-orders`, `/api/v1/amazon-attribution`, `/api/v1/historical`, `/api/v1/channel-attribution` (ClickHouse, no OAuth cred needed).

### 2.3 Cache TTL Reference


| Route                | TTL              | Logic                                                                                         |
| -------------------- | ---------------- | --------------------------------------------------------------------------------------------- |
| Shopify analytics    | Dynamic          | `getCacheTTL(end_date, 'shopify')` — longer for older dates                                   |
| Meta analytics       | Dynamic          | `getCacheTTL(end_date, 'meta')` — same dynamic pattern as Shopify; older dates = longer TTL   |
| Google Ads analytics | Dynamic          | `getCacheTTL(end_date, 'google')` — same dynamic pattern as Shopify; older dates = longer TTL |
| GA4 analytics        | Platform default | varies                                                                                        |
| Amazon Organic       | 300 s            | Fixed                                                                                         |
| Amazon Attribution   | 300 s            | Fixed                                                                                         |
| Meta Attribution     | 300 s            | Fixed (`getTTL: () => 300000`)                                                                |
| Google Attribution   | 300 s            | Fixed                                                                                         |
| Historical           | 300 s            | Dynamic: `getCacheTTL(end_date, 'historical')`                                                |
| Channel Attribution  | 300 s            | Fixed                                                                                         |
| P&L                  | No cache         | —                                                                                             |


### 2.4 Timezone Resolution Priority (Shopify)

```
1. req.auth.timezone         (JWT claim — highest priority)
2. brand_envs.extra_data.timezone
3. brands.timezone           (from DB join)
4. IST                       (hardcoded default)
```

File: `src/routes/shopifyAnalyticsRoutes.js → getRequestLocale()`

### 2.5 Token Auto-Refresh (Google Ads, GA4)

Triggered when `token_expiry < now + 5 minutes` OR when `token_expiry IS NULL`:

```sql
UPDATE core.brand_envs
SET access_token = $1, token_expiry = $2, updated_at = NOW()
WHERE id = $3
```

For GA4 with null expiry: forces `refreshGoogleToken()` regardless of expiry check.
File: `src/utils/tokenRefresh.js → refreshTokenIfNeeded()`

### 2.6 Response Envelope

All endpoints return:

```json
{ "success": true, "data": { ... } }
{ "success": false, "error": "message" }
```

Common error HTTP codes:


| Code | Meaning                                            |
| ---- | -------------------------------------------------- |
| 400  | Missing brand_id / company_id / date params        |
| 401  | JWT missing or expired; or platform token expired  |
| 403  | brand_id mismatch; integration not linked to brand |
| 404  | Integration not connected / inactive               |
| 500  | Internal error / upstream API failure              |
| 504  | Upstream API timeout (Meta dashboard)              |


---

## 3. Shopify — Organic Ecommerce

### For Users

Shopify tracks all orders placed on your store. Revenue figures include cancelled and refunded orders unless otherwise noted. COGS is fetched separately via Shopify GraphQL and is not stored in a database.

### Route File

`src/routes/shopifyAnalyticsRoutes.js`
**Base:** `GET /api/v1/shopify/analytics`
**Auth:** `shopifyAnalyticsAuth` (custom: JWT + Shopify integration check)

### Credential DB Query

```sql
-- File: src/integrations/shared/shopifyCredentials.js
SELECT be.*, b.timezone AS brand_timezone, b.default_currency AS brand_currency
FROM core.brand_envs be
LEFT JOIN core.brands b ON b.id = be.brand_id AND b.company_id = be.company_id
WHERE be.brand_id = $1
  AND be.company_id = $2
  AND be.platform = 'shopify'
  AND be.is_active = true
LIMIT 1
```

Returns: `shop_domain`, `access_token`, `brand_timezone`, `brand_currency`
Cached via `getCachedCredentials()`.

### Data Source

**No database storage — real-time Shopify API calls only.**

```
REST:    GET https://{shop_domain}/admin/api/2024-01/orders.json
GraphQL: POST https://{shop_domain}/admin/api/2024-01/graphql.json
Headers: X-Shopify-Access-Token: {access_token}
```

### API Endpoints


| Path                           | Handler                                     | Notes                                   |
| ------------------------------ | ------------------------------------------- | --------------------------------------- |
| GET `/revenue`                 | `analytics.getTotalRevenue()`               | Revenue summary with breakdowns         |
| GET `/orders/last`             | `analytics.getLastOrders()`                 | Recent orders list (no date required)   |
| GET `/orders/statistics`       | `analytics.getOrderStatistics()`            | Returns/cancellations stats             |
| GET `/sales/province`          | `analytics.getSalesByProvince()`            | Revenue by Indian state                 |
| GET `/sales/region`            | `analytics.getSalesByRegion()`              | Revenue by country/region               |
| GET `/sales/payment-methods`   | `analytics.getSalesByPaymentMethod()`       | Payment gateway breakdown               |
| GET `/dashboard`               | `analytics.getAnalyticsDashboard()`         | All cards combined                      |
| GET `/live-dashboard-bundle`   | `fetchLiveDashboardBundle()`                | Shopify + Ad Spend + Amazon SP combined |
| GET `/cogs`                    | `analytics.getCOGS()`                       | COGS via GraphQL unitCost               |
| GET `/products/top`            | `analytics.getTopProducts()`                | Top N products by revenue               |
| GET `/products/performance`    | `analytics.getProductPerformance()`         | Per-product metrics                     |
| GET `/customers`               | `analytics.getCustomerAnalytics()`          | Customer metrics (LTV, repeat rate)     |
| GET `/customers/cohort`        | `analytics.getCohortAnalysis()`             | Month-over-month cohort retention       |
| GET `/time-patterns`           | `analytics.getTimeBasedAnalytics()`         | Hour-of-day / day-of-week patterns      |
| GET `/fulfillment`             | `analytics.getFulfillmentAnalytics()`       | Fulfillment status breakdown            |
| GET `/discounts`               | `analytics.getDiscountAnalytics()`          | Promo code usage                        |
| GET `/shipping`                | `analytics.getShippingAnalytics()`          | Shipping cost breakdown                 |
| GET `/funnel/sales`            | `analytics.getSalesFunnel()`                | Order funnel stages                     |
| GET `/funnel/cart-abandonment` | `analytics.getCartAbandonment()`            | Cart abandonment rate                   |
| GET `/funnel/add-to-cart`      | `analytics.getAddToCartMetrics()`           | Add-to-cart metrics                     |
| POST `/customer-journey`       | `analytics.getCustomerJourneyFromShopify()` | Journey from order data                 |


**Common params:** `brand_id` (required), `company_id` (required), `start_date`, `end_date`

### Revenue Formulas (`/revenue`)

#### Raw Fields from Shopify REST


| Field               | Source                                                     | Notes                                               |
| ------------------- | ---------------------------------------------------------- | --------------------------------------------------- |
| `total_revenue`     | `SUM(order.total_price)` ALL statuses                      | Includes paid, unpaid, pending, cancelled, refunded |
| `paid_revenue`      | `SUM(order.total_price)` where `financial_status = 'paid'` | Only fully paid orders                              |
| `refunded_amount`   | `SUM(refund.transactions.amount)`                          | Full + partial refunds                              |
| `cancelled_revenue` | `SUM(order.total_price)` where `cancelled_at IS NOT NULL`  |                                                     |
| `total_orders`      | `COUNT(order.id)`                                          | All statuses                                        |
| `paid_orders`       | `COUNT` where `financial_status = 'paid'`                  |                                                     |
| `refunded_orders`   | `COUNT(DISTINCT order.id)` with any refund                 |                                                     |
| `cancelled_orders`  | `COUNT` where `cancelled_at IS NOT NULL`                   |                                                     |


#### Derived Fields

```
──────────────────────────────────────────────────────────────────────────────
CASE A — Cancelled orders ARE included in the total_revenue fetch
          (cancelled_revenue > 0 because cancelled orders were not filtered out)
──────────────────────────────────────────────────────────────────────────────
gross_revenue = total_revenue - cancelled_revenue
                ↑ Remove cancellations first to get true gross
net_revenue   = gross_revenue - refunded_amount
              = total_revenue - cancelled_revenue - refunded_amount

──────────────────────────────────────────────────────────────────────────────
CASE B — Cancelled orders are EXCLUDED from the total_revenue fetch
          (cancelled_revenue = 0; query filtered them out at source)
──────────────────────────────────────────────────────────────────────────────
gross_revenue = total_revenue              (already clean, no cancels)
net_revenue   = total_revenue - refunded_amount

──────────────────────────────────────────────────────────────────────────────
RULE: net_revenue ALWAYS excludes both cancelled orders AND returns/refunds.
      Never count cancelled or returned revenue as earned revenue.
──────────────────────────────────────────────────────────────────────────────

average_order_value   = net_revenue / (total_orders - cancelled_orders - fully_refunded_orders)
cost_of_goods_sold    = SUM(unitCost.amount × quantity) via GraphQL
gross_profit          = net_revenue - cost_of_goods_sold
gross_profit_margin   = (gross_profit / net_revenue) × 100
```

> **Why `total_revenue` not `paid_revenue`?**  
> `net_revenue` represents what the business earned from all orders after deducting cancellations and returns — including orders placed but not yet paid (pending, partially paid). Using only `paid_revenue` would undercount by excluding valid pending orders.

#### Example — Cancelled included in fetch (Case A)

```
total_revenue     = 1,707,591   (ALL orders: paid + unpaid + pending + cancelled)
paid_revenue      =   808,647   (only financial_status = 'paid')
refunded_amount   =    19,051
cancelled_revenue =    42,380   (cancelled orders were included in total_revenue)

gross_revenue = 1,707,591 - 42,380 = 1,665,211   ← remove cancellations first
net_revenue   = 1,665,211 - 19,051 = 1,646,160   ← then remove refunds
```

#### Example — Cancelled excluded from fetch (Case B)

```
total_revenue     = 1,665,211   (fetch already excludes cancelled_at IS NOT NULL)
cancelled_revenue = 0           (not fetched / filtered at query level)
refunded_amount   =    19,051

net_revenue = 1,665,211 - 19,051 = 1,646,160   ← same result, cleaner path
```

### COGS Source (GraphQL)

```graphql
query {
  productVariant(id: "gid://shopify/ProductVariant/{id}") {
    inventoryItem {
      unitCost { amount currencyCode }
    }
  }
}
```

Partial refund COGS adjustment:

```
refunded_cogs = unit_cost × (refunded_quantity / ordered_quantity)
```

### Returns & Cancellations (`/orders/statistics`)

```json
{
  "returns_and_cancellations": {
    "total_cancelled":   13,
    "total_refunded":    73,
    "total_affected":    86,
    "impact_percentage": 10.5,
    "lost_revenue":      234843
  }
}
```

`total_affected = total_cancelled + total_refunded`

### Live Dashboard Bundle (`/live-dashboard-bundle`)

Single endpoint combining Shopify + Meta/Google ad spend + Amazon SP data.
Requires: `brand_id`, `company_id`, `start_date`, `end_date`
Optional: `prev_start_date`, `prev_end_date`, `include_current_dashboard`, `include_previous_dashboard`

---

## 4. Amazon SP — Organic Orders (ClickHouse)

### For Users

Shows only **organic** Amazon orders (not attributed to any ad campaign). Date axis is the order's `purchase_date`. Includes P&L from Amazon settlement reports.

### Route File

`src/routes/amazonOrganicOrdersRoutes.js`
**Base:** `GET /api/v1/amazon-organic-orders`
**Auth:** `authenticate` (standard JWT) | **Cache:** 300 s fixed

### No Credential DB Lookup

Requires ClickHouse to be configured:

```javascript
if (!getClickhouseClient()) throw new Error('ClickHouse is not configured');
```

### API Endpoints


| Path          | Params                               | Description                       |
| ------------- | ------------------------------------ | --------------------------------- |
| GET `/bounds` | `brand_id`                           | Min/max available `purchase_date` |
| GET `/`       | `brand_id`, `start_date`, `end_date` | Orders + P&L for date range       |


### ClickHouse Step 1 — Orders (`fct_amazon_sp_orders`)

```sql
SELECT
  brand_id, company_id, shop_domain, amazon_order_id,
  argMax(seller_order_id, _gold_created_at)         AS seller_order_id,
  argMax(marketplace_id, _gold_created_at)          AS marketplace_id,
  argMax(purchase_date, _gold_created_at)           AS purchase_date,
  argMax(purchase_timestamp, _gold_created_at)      AS purchase_timestamp,
  argMax(order_status, _gold_created_at)            AS order_status,
  argMax(fulfillment_channel, _gold_created_at)     AS fulfillment_channel,
  argMax(sales_channel, _gold_created_at)           AS sales_channel,
  argMax(order_type, _gold_created_at)              AS order_type,
  toFloat64(argMax(order_total, _gold_created_at))  AS order_total,
  argMax(currency_code, _gold_created_at)           AS currency_code,
  toInt64(argMax(number_of_items_shipped, _gold_created_at))   AS number_of_items_shipped,
  toInt64(argMax(number_of_items_unshipped, _gold_created_at)) AS number_of_items_unshipped,
  toUInt8(argMax(is_business_order, _gold_created_at)) AS is_business_order,
  toUInt8(argMax(is_prime, _gold_created_at))       AS is_prime,
  argMax(ship_to_city, _gold_created_at)            AS ship_to_city,
  argMax(ship_to_state, _gold_created_at)           AS ship_to_state,
  argMax(ship_to_country_code, _gold_created_at)    AS ship_to_country_code,
  argMax(order_attribution_tag, _gold_created_at)   AS order_attribution_tag
FROM fct_amazon_sp_orders AS o
WHERE o.brand_id = {brandId:Int64}
  AND o.purchase_date IS NOT NULL
  AND toDate(o.purchase_date) >= toDate({startDate:String})
  AND toDate(o.purchase_date) <= toDate({endDate:String})
  AND upperUTF8(trimBoth(coalesce(o.order_attribution_tag, ''))) = 'ORGANIC'
GROUP BY brand_id, company_id, shop_domain, amazon_order_id
ORDER BY purchase_date DESC, amazon_order_id
```

Deduplication: `argMax(..., _gold_created_at)` selects the most recent version of each row.
Grouping key: `(brand_id, company_id, shop_domain, amazon_order_id)`

### ClickHouse Step 2 — P&L (`fct_amazon_sp_order_pnl`)

```sql
SELECT
  brand_id, amazon_order_id,
  argMax(pnl_status, _gold_created_at)                         AS pnl_status,
  argMax(payout_basis, _gold_created_at)                       AS payout_basis,
  toFloat64(argMax(gross_revenue, _gold_created_at))           AS gross_revenue,
  toFloat64(argMax(effective_gross_revenue, _gold_created_at)) AS effective_gross_revenue,
  toFloat64(argMax(estimated_gross_revenue, _gold_created_at)) AS estimated_gross_revenue,
  toFloat64(argMax(total_cogs, _gold_created_at))              AS total_cogs,
  toFloat64(argMax(cogs_product, _gold_created_at))            AS cogs_product,
  toInt64(argMax(cogs_items_matched, _gold_created_at))        AS cogs_items_matched,
  toInt64(argMax(cogs_items_unmatched, _gold_created_at))      AS cogs_items_unmatched,
  toFloat64(argMax(effective_gross_profit, _gold_created_at))  AS effective_gross_profit,
  toFloat64(argMax(net_payout, _gold_created_at))              AS net_payout,
  toFloat64(argMax(effective_net_payout, _gold_created_at))    AS effective_net_payout,
  toFloat64(argMax(estimated_net_payout, _gold_created_at))    AS estimated_net_payout,
  toFloat64(argMax(total_amazon_fees, _gold_created_at))       AS total_amazon_fees,
  toFloat64(argMax(effective_refunds, _gold_created_at))       AS effective_refunds,
  toFloat64(argMax(effective_commission, _gold_created_at))    AS effective_commission,
  toFloat64(argMax(effective_closing, _gold_created_at))       AS effective_closing,
  toFloat64(argMax(effective_shipping, _gold_created_at))      AS effective_shipping,
  toFloat64(argMax(effective_other_service_fees, _gold_created_at)) AS effective_other_service_fees,
  toFloat64(argMax(effective_tax_withheld, _gold_created_at))  AS effective_tax_withheld,
  toFloat64(argMax(total_refund_amount, _gold_created_at))     AS total_refund_amount,
  toFloat64(argMax(tcs_igst, _gold_created_at))                AS tcs_igst,
  toFloat64(argMax(tcs_cgst, _gold_created_at))                AS tcs_cgst,
  toFloat64(argMax(tcs_sgst, _gold_created_at))                AS tcs_sgst,
  toFloat64(argMax(item_tds, _gold_created_at))                AS item_tds,
  toFloat64(argMax(total_tax_withheld, _gold_created_at))      AS total_tax_withheld
FROM fct_amazon_sp_order_pnl AS p
WHERE p.brand_id = {brandId:Int64}
  AND p.amazon_order_id IN {orderIds:Array(String)}
GROUP BY brand_id, amazon_order_id
```

### Bounds Query

```sql
SELECT min(purchase_date) AS min_date, max(purchase_date) AS max_date
FROM fct_amazon_sp_orders
WHERE brand_id = {brandId:Int64}
  AND purchase_date IS NOT NULL
  AND upperUTF8(trimBoth(coalesce(order_attribution_tag, ''))) = 'ORGANIC'
```

### P&L Formula Chain

File: `src/integrations/amazonShared/amazonOrderPnl.js`

```
gross_revenue   = effective_gross_revenue ?? gross_revenue
                  ↑ Sum of order_total for non-cancelled orders in range
                  ↑ Same as total_gross_revenue in attribution summary

gross_sales     = gross_revenue + effective_tax_withheld
                  ↑ Settlement-adjusted revenue (TCS/TDS netted in)
                  ↑ NOT the same as the "Gross Sales" UI card — see below

net_payout      = effective_net_payout         ← preferred; use this
               OR gross_revenue
                + effective_refunds            (negative)
                + effective_commission         (negative)
                + effective_closing            (negative)
                + effective_shipping           (negative)
                + effective_tax_withheld       (negative)
                + effective_other_service_fees (negative)

amazon_fees     = total_amazon_fees            ← per order, ONLY if value > 0
               OR |effective_commission|
                + |effective_closing|
                + |effective_shipping|
                + |effective_other_service_fees|
                + |effective_tax_withheld|
                  ↑ Sum per-order amazon_fees — never Math.abs() a negative
                    total_amazon_fees aggregate across orders

product_cost    = cogs_product
                  ↑ Use cogs_product directly (seller-uploaded unit cost × qty)
                  ↑ Do NOT use total_cogs on the PnL row — that field mixes fees + product

total_cogs      = amazon_fees + product_cost
                  ↑ "Total cost of selling on Amazon" (platform fees + product cost)
                  ↑ Used on attribution COGS card; NOT the same as product_cost alone

gross_profit    = net_payout - product_cost
                  ↑ Deduct product_cost (cogs_product), not total_cogs

net_profit      = net_payout - product_cost - ad_spend
```

#### Gross revenue vs gross_sales (naming)

| Field | Formula | UI / API use |
| ----- | ------- | ------------ |
| `gross_revenue` / `total_gross_revenue` | `SUM(effective_gross_revenue)` = sum of `order_total` | **Gross Sales card** on Amazon Attribution (`amazonDisplayGrossSales()` → `total_gross_revenue`) |
| `gross_sales` / `total_gross_sales` | `gross_revenue + effective_tax_withheld` | Settlement math only — tax-withheld adjustment, not the headline Gross Sales card |
| `total_order_total` | `SUM(order_total)` from orders table | TACOS / ACoS denominators; equals `total_gross_revenue` when PnL is joined |

#### Amazon Fees — components

Per order, resolve fees in this priority (`resolveAmazonPlatformFees`):

1. `total_amazon_fees` or `total_service_fees` **if value > 0** (positive absolute in CH)
2. Else `|effective_commission| + |effective_closing| + |effective_shipping| + |effective_other_service_fees| + |effective_tax_withheld|`
3. Else sum of raw `fee_*` columns + `|effective_tax_withheld|`
4. Else `total_cogs - cogs_product` on the PnL row
5. Else `gross_sales - net_payout + refunds + |tax_withheld|` (gap formula)

| Component | CH column | In amazon_fees? |
| --------- | --------- | --------------- |
| Referral / commission | `effective_commission` | Yes (`|value|`) |
| Closing fee | `effective_closing` | Yes |
| Shipping (MFN/FBA) | `effective_shipping` | Yes |
| Other service fees | `effective_other_service_fees` | Yes |
| TCS/TDS withheld | `effective_tax_withheld` | Yes (`|value|`) — **not** customer GST on the order |
| Refunds | `effective_refunds` | **No** — separate Refunds card |
| Product cost | `cogs_product` | **No** — part of Total COGS, not fees |
| Customer GST on item price | baked into `order_total` | **No** — already in gross_revenue |

**Tax distinction:** Customer GST is included in `order_total` / `gross_revenue`. TCS/TDS that Amazon withholds (`effective_tax_withheld`, plus `tcs_igst`, `tcs_cgst`, `tcs_sgst`, `item_tds`) is counted **inside** `amazon_fees`, not subtracted again from gross_revenue on the Gross Sales card.

**`total_amazon_fees` aggregate trap:** Summing `total_amazon_fees` across orders can be **negative** when refunded orders have net fee credits. Never `Math.abs(SUM(total_amazon_fees))`. Always compute `amazon_fees` per order, then sum.

#### Total COGS — composition

```
total_cogs (attribution) = amazon_fees + product_cost
                         = SUM(per-order amazon_fees) + SUM(cogs_product)
```

| Card | Includes |
| ---- | -------- |
| **Amazon Fees** | Commission + closing + shipping + other + TCS/TDS |
| **Product Cost** | `cogs_product` only |
| **Total COGS** | Amazon Fees + Product Cost |
| **Net Payout** | After fees and refunds — product cost **not** deducted here |
| **Net Profit** | Net Payout − Product Cost − Ad Spend |

#### Worked example (brand 20, 2026-06-01 → 2026-06-15, 48 orders)

| Metric | Value (INR) |
| ------ | ----------- |
| Gross Sales card (`total_gross_revenue`) | 97,049.00 |
| `total_gross_sales` (settlement) | 96,603.67 (= 97,049 − 445.33 TCS) |
| Refunds | −11,493.00 |
| Amazon Fees | 20,850.80 (commission 11,610.61 + closing 3,568.91 + shipping 5,225.95 + TCS 445.33) |
| Net Payout | 64,705.20 |
| Product Cost | 19,138.74 |
| Total COGS | 39,989.54 |
| Net Profit | 64,705.20 − 19,138.74 − ad_spend |

**Fee signs:** Effective fee columns in ClickHouse are stored as **negative** (deductions). Use `Math.abs()` on each component when summing costs. `total_amazon_fees` per row is normally **positive absolute** — use directly only when `> 0`; if `≤ 0`, fall through to component sum.

### Summary Output


| Field                 | Value                         |
| --------------------- | ----------------------------- |
| `total_orders`        | COUNT non-cancelled orders    |
| `total_order_total`   | SUM `order_total`             |
| `total_gross_revenue` | SUM `effective_gross_revenue` — **Gross Sales card** |
| `total_gross_sales`   | SUM `gross_sales` per order (`gross_revenue + tax_withheld`) — settlement only |
| `total_refunds`       | SUM `effective_refunds`       |
| `total_net_payout`    | SUM `effective_net_payout`    |
| `total_amazon_fees`   | SUM per-order `amazon_fees` (component-based; see §4) |
| `total_product_cost`  | SUM `cogs_product`              |
| `total_cogs`          | SUM `total_cogs` per order (`amazon_fees + product_cost`) |
| `total_gross_profit`  | SUM `gross_profit` per order  |
| `canceled_orders`     | COUNT excluded-status orders  |
| `currency`            | `INR` (hardcoded)             |


---

## 5. Amazon Ads — Attribution (ClickHouse)

### For Users

Shows performance of your Amazon advertising campaigns. Date axis is `report_date` (when the ad ran), not the order date. Two ROAS views: channel-level (vs all SP orders) and attributed (vs ad-linked orders only).

### Route File

`src/routes/amazonAttributionRoutes.js`
**Base:** `GET /api/v1/amazon-attribution`
**Auth:** `authenticate` (standard JWT) | **Cache:** 300 s fixed

### API Endpoints


| Path          | Params                                                                         | Description                                        |
| ------------- | ------------------------------------------------------------------------------ | -------------------------------------------------- |
| GET `/bounds` | `brand_id`                                                                     | Min/max available `report_date` in campaigns table |
| GET `/`       | `brand_id`, `start_date`, `end_date`, `campaign_id`?, `ad_group_id`?, `ad_id`? | Full hierarchy                                     |


Optional filters (`campaign_id`, `ad_group_id`, `ad_id`) narrow ClickHouse WHERE to a single entity.

### ClickHouse Queries

#### Campaigns Daily (`fct_amazon_ads_campaigns_daily`)

```sql
SELECT
  brand_id, company_id, shop_domain, campaign_id,
  argMax(campaign_name, _gold_created_at)     AS campaign_name,
  argMax(campaign_type, _gold_created_at)     AS campaign_type,    -- SP, SB, SD
  argMax(campaign_status, _gold_created_at)   AS campaign_status,  -- ENABLED, PAUSED, ARCHIVED
  report_date,
  toInt64(argMax(impressions, _gold_created_at))     AS impressions,
  toInt64(argMax(clicks, _gold_created_at))          AS clicks,
  toFloat64(argMax(cost, _gold_created_at))          AS spend,  -- stored as 'cost', exposed as 'spend'
  toInt64(argMax(purchases_1d, _gold_created_at))    AS purchases_1d,
  toInt64(argMax(purchases_7d, _gold_created_at))    AS purchases_7d,
  toInt64(argMax(purchases_14d, _gold_created_at))   AS purchases_14d,
  toInt64(argMax(purchases_30d, _gold_created_at))   AS purchases_30d,
  toFloat64(argMax(sales_1d, _gold_created_at))      AS sales_1d,
  toFloat64(argMax(sales_7d, _gold_created_at))      AS sales_7d,
  toFloat64(argMax(sales_14d, _gold_created_at))     AS sales_14d,
  toFloat64(argMax(sales_30d, _gold_created_at))     AS sales_30d,
  toFloat64(argMax(acos, _gold_created_at))          AS acos,
  toFloat64(argMax(cpc, _gold_created_at))           AS cpc,
  toFloat64(argMax(cpm, _gold_created_at))           AS cpm,
  toFloat64(argMax(ctr, _gold_created_at))           AS ctr,
  toInt64(argMax(orders, _gold_created_at))          AS orders,
  toFloat64(argMax(sales, _gold_created_at))         AS sales,
  toFloat64(argMax(roas, _gold_created_at))          AS roas,
  argMax(amazon_order_ids, _gold_created_at)         AS amazon_order_ids,  -- comma-separated
  toInt64(argMax(matched_order_count, _gold_created_at)) AS matched_order_count
FROM fct_amazon_ads_campaigns_daily AS d
WHERE d.brand_id = {brandId:Int64}
  AND toDate(d.report_date) >= toDate({startDate:String})
  AND toDate(d.report_date) <= toDate({endDate:String})
  -- Optional: AND toString(d.campaign_id) = {campaignId:String}
GROUP BY brand_id, company_id, shop_domain, campaign_id, report_date
ORDER BY report_date, campaign_id
```

#### Ad Groups Daily (`fct_amazon_ads_ad_groups_daily`)

Same columns plus `ad_group_id`, `ad_group_name`.
Spend uses coalesce: `toFloat64(coalesce(argMax(spend, _gold_created_at), argMax(cost, _gold_created_at))) AS spend`

#### Ads Daily (`fct_amazon_ads_ads_daily`)

Same plus `ad_id`, `advertised_asin`, `advertised_sku`. Same coalesce spend pattern.

#### SP Orders — All (for Channel-Level Summary)

```sql
-- No order_attribution_tag filter — fetches ALL SP orders in date range
WHERE o.brand_id = {brandId:Int64}
  AND o.purchase_date IS NOT NULL
  AND toDate(o.purchase_date) >= toDate({startDate:String})
  AND toDate(o.purchase_date) <= toDate({endDate:String})
GROUP BY brand_id, company_id, shop_domain, amazon_order_id
```

#### Order Items (`fct_amazon_order_items`)

```sql
SELECT brand_id, amazon_order_id, order_item_id,
  argMax(seller_sku, _gold_created_at)          AS seller_sku,
  argMax(asin, _gold_created_at)                AS asin,
  argMax(title, _gold_created_at)               AS title,
  toInt64(argMax(quantity_ordered, _gold_created_at))     AS quantity_ordered,
  toFloat64(argMax(item_price_amount, _gold_created_at))  AS item_price_amount,
  toFloat64(argMax(total_cogs, _gold_created_at))         AS total_cogs
FROM fct_amazon_order_items AS oi
WHERE oi.brand_id = {brandId:Int64}
  AND oi.amazon_order_id IN {orderIds:Array(String)}
GROUP BY brand_id, amazon_order_id, order_item_id
```

#### Bounds Query

```sql
SELECT min(report_date) AS min_date, max(report_date) AS max_date
FROM fct_amazon_ads_campaigns_daily
WHERE brand_id = {brandId:Int64}
```

### Derived Metrics

File: `src/integrations/amazonAttribution/buildAmazonAttributionPayload.js → deriveMetrics()`

```
spend   = cost  (campaigns table)
          coalesce(spend, cost)  (ad_groups / ads tables — field name varies)

cpc     = spend / clicks                    (0 if clicks = 0)
cpm     = (spend / impressions) × 1000     (0 if impressions = 0)
ctr     = (clicks / impressions) × 100     (0 if impressions = 0)
roas    = sales / spend                    (0 if spend = 0) — attributed only
acos    = (spend / sales) × 100            (0 if sales = 0) — attributed only
```

### Channel vs Attributed ROAS


| Metric           | Formula                                    | Denominator                          |
| ---------------- | ------------------------------------------ | ------------------------------------ |
| `roas` (channel) | `total_order_total / total_spend`          | ALL SP orders (organic + attributed) |
| `acos` (channel) | `(total_spend / total_order_total) × 100`  | Same — TACOS                         |
| `ads_roas`       | `attributed_sales / total_spend`           | Ad-linked orders only                |
| `ads_acos`       | `(total_spend / attributed_sales) × 100`   | Ad-linked orders only                |
| `average_cpc`    | `total_spend / total_clicks`               |                                      |
| `average_cpm`    | `(total_spend / total_impressions) × 1000` |                                      |


### Net Profit (Amazon)

```
net_profit = total_net_payout - total_product_cost - total_spend
```

Source: `computeAmazonNetProfit()` in `src/integrations/amazonShared/amazonOrderPnl.js`

---

## 6. Meta Ads — Real-Time (Graph API)

### For Users

Shows Facebook/Instagram ad performance pulled live from the Meta API. Organized by Campaign → Ad Set → Ad. Token expiry returns a special error code so the frontend can prompt reconnection.

### Route File

`src/routes/metaAnalyticsRoutes.js`
**Base:** `GET /api/v1/meta/analytics`
**Auth:** `authenticate` (standard JWT) | **Cache:** 120 s minimum

### Credential DB Query

```sql
-- File: src/integrations/shared/platformCredentials.js

-- Primary lookup
SELECT id, brand_id, company_id, account_id, access_token, extra_data, is_active
FROM core.brand_envs
WHERE platform = 'meta'
  AND brand_id = $1 AND company_id = $2
  AND is_active = true
ORDER BY updated_at DESC NULLS LAST
LIMIT 1

-- Fallback (rows without strict brand_id match)
WHERE platform = 'meta' AND is_active = true
  AND (brand_id IS NULL OR brand_id = $1)
  AND (company_id IS NULL OR company_id = $2)
ORDER BY updated_at DESC NULLS LAST, installed_at DESC NULLS LAST
LIMIT 1
```

`extra_data` JSONB may contain: `preferred_account_id`, `all_accounts[]`, `currency`, `timezone`
Account ID normalized to `act_<digits>` format.
Optional `ad_account_id` query param overrides stored account.

### Data Source

Meta Graph API v21.0 — real-time, no DB caching.

```
GET https://graph.facebook.com/v21.0/{act_account_id}/insights
  ?fields=spend,impressions,clicks,reach,actions,action_values,...
  &time_range={"since":"YYYY-MM-DD","until":"YYYY-MM-DD"}
  &level=campaign|adset|ad
```

### API Endpoints


| Path                        | Handler                          | Notes                                |
| --------------------------- | -------------------------------- | ------------------------------------ |
| GET `/campaigns`            | `getCampaignPerformance()`       | All campaigns                        |
| GET `/campaigns/:id/adsets` | `getCampaignAdSets()`            | Ad sets for one campaign (lazy load) |
| GET `/adsets`               | `getAdSetPerformance()`          | All ad sets                          |
| GET `/adsets/:id/ads`       | `getAdSetAds()`                  | Ads for one ad set (lazy load)       |
| GET `/ads`                  | `getAdPerformance()`             | All ads                              |
| GET `/audience`             | `getAudienceInsights()`          | Demographics + geography             |
| GET `/demographics`         | `getDemographicAnalytics()`      | Age / gender breakdown               |
| GET `/devices`              | `getDeviceAnalytics()`           | Device platform                      |
| GET `/creatives`            | `getCreativeAnalytics()`         | Creative performance                 |
| GET `/actions`              | `getActionTypeBreakdown()`       | All conversion events                |
| GET `/platform-positions`   | `getPlatformPositionAnalytics()` | Publisher × placement                |
| GET `/budget-pacing`        | `getBudgetPacing()`              | Budget utilization                   |
| GET `/funnel`               | `getConversionFunnel()`          | Purchase funnel stages               |
| GET `/time-based`           | `getTimeBasedPerformance()`      | Hourly/daily patterns                |
| GET `/spend`                | `getSpendAnalytics()`            | Spend breakdown                      |
| GET `/dashboard`            | `getMetaDashboard()`             | Combined all (120 s timeout → 504)   |


### Core Fields


| Field             | Source                                                              |
| ----------------- | ------------------------------------------------------------------- |
| `spend`           | `insights.spend`                                                    |
| `impressions`     | `insights.impressions`                                              |
| `clicks`          | `insights.clicks`                                                   |
| `reach`           | `insights.reach`                                                    |
| `frequency`       | `insights.frequency`                                                |
| `purchases`       | `actions[action_type = offsite_conversion.fb_pixel_purchase].value` |
| `conversionValue` | `action_values[action_type = fb_pixel_purchase].value`              |


> ⚠️ **DO NOT use `purchases` or `conversionValue` from Meta API for order counts or attributed revenue.**  
> Meta's pixel-based attribution uses its own click-through/view-through windows (e.g., 7-day click, 1-day view) and can over-count or double-count versus actual Shopify orders.  
> **Source of truth for order counts:** Use Shopify attributed data (UTM-tagged Shopify orders where `utm_source = 'facebook'` or `referring_site` matches). This is available via the channel attribution routes (`/api/v1/channel-attribution`).  
> Meta API fields like `spend`, `impressions`, `clicks`, `reach`, `ctr`, `cpc` ARE accurate and should be used from the Meta API.
> | `oneDayViewConversionValue` | actions with 1d_view attribution window |
> | `sevenDayViewConversionValue` | actions with 7d_view attribution window |

### Conversion Event Action Types


| Event             | `action_type`                                   |
| ----------------- | ----------------------------------------------- |
| Purchase          | `offsite_conversion.fb_pixel_purchase`          |
| Add to Cart       | `offsite_conversion.fb_pixel_add_to_cart`       |
| Initiate Checkout | `offsite_conversion.fb_pixel_initiate_checkout` |
| View Content      | `offsite_conversion.fb_pixel_view_content`      |


### Derived Metrics

```
ctr             = clicks / impressions × 100      (0 if no impressions)
cpc             = spend / clicks                  (null if no clicks)
cpm             = (spend / impressions) × 1000    (0 if no impressions)
cpp             = (spend / reach) × 1000          (cost per 1,000 reached)
roas            = conversionValue / spend         (null if no spend)
cpa             = spend / purchases               (null if no purchases)
profit          = total_revenue - total_spend
profit_margin   = (profit / revenue) × 100
avg_cost_per_purchase = total_spend / total_purchases
```

### Error Codes


| Condition             | HTTP | Code                   |
| --------------------- | ---- | ---------------------- |
| Token expired         | 401  | `META_TOKEN_EXPIRED`   |
| Dashboard timeout     | 504  | `META_REQUEST_TIMEOUT` |
| Account not linked    | 403  | error message          |
| Integration not found | 404  | error message          |


---

## 7. Meta Attribution — Historical (ClickHouse)

### For Users

Shows Meta ad performance read from ClickHouse historical tables (not live from Meta API). Includes order-level attribution via UTM parameters. Use this for historical analysis; use `/meta/analytics` for real-time data.

### Key Distinction

- `/api/v1/meta/analytics` = **Real-time Meta Graph API** calls
- `/api/v1/meta-attribution` = **ClickHouse historical tables** (`fct_meta_ads_daily`, `fct_meta_ads_hourly`, `fct_order_attribution`)

### Route File

`src/routes/metaAttributionRoutes.js`
**Base:** `GET /api/v1/meta-attribution`
**Auth:** `authenticate` (standard JWT) | **Cache:** 300 s fixed

### Data Sources (ClickHouse)


| Table                   | Used When                                     |
| ----------------------- | --------------------------------------------- |
| `fct_meta_ads_hourly`   | Single-day range or `time_aggregation=hourly` |
| `fct_meta_ads_daily`    | Multi-day range or `time_aggregation=daily`   |
| `fct_order_attribution` | Attribution linking (`lt_platform = 'meta'`)  |
| `fct_orders`            | Order details                                 |
| `fct_order_items`       | Line items                                    |


### API Endpoints


| Path                  | Handler                          | Notes                                        |
| --------------------- | -------------------------------- | -------------------------------------------- |
| GET `/creative-image` | `handleCreativeImageRequest()`   | Proxies Meta creative image (no cache/dedup) |
| GET `/creative-video` | `handleCreativeVideoRequest()`   | Proxies Meta creative video (no cache/dedup) |
| GET `/`               | `analytics.getMetaAttribution()` | Full campaign → adset → ad hierarchy         |


### Query Parameters for `GET /`


| Param              | Required | Notes                                                      |
| ------------------ | -------- | ---------------------------------------------------------- |
| `brand_id`         | Yes      | From query or JWT                                          |
| `start_date`       | Yes      | YYYY-MM-DD or ISO 8601                                     |
| `end_date`         | Yes      | YYYY-MM-DD or ISO 8601                                     |
| `time_aggregation` | No       | `daily` (default) or `hourly`; auto-resolved by date range |
| `campaign_id`      | No       | Filter to one campaign                                     |
| `adset_id`         | No       | Filter to one ad set                                       |
| `ad_id`            | No       | Filter to one ad                                           |


Auto time-aggregation: single-day range → `fct_meta_ads_hourly`, multi-day → `fct_meta_ads_daily`

### Response Structure

```json
{
  "period": { "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" },
  "time_aggregation": "daily",
  "summary": {
    "total_spend": 0, "total_revenue": 0, "total_orders": 0, "roas": 0
  },
  "campaigns": [
    {
      "campaign_id": "...", "campaign_name": "...",
      "metrics": { "spend": 0, "impressions": 0, "clicks": 0, "cpc": 0, "cpm": 0, "ctr": 0,
                   "attributed_orders_count": 0 },
      "utm_source": "...", "utm_medium": "...", "utm_campaign": "...",
      "adsets": [ { "adset_id": "...", "ads": [ { "ad_id": "...", "orders": [...], "daily_data": [...] } ] } ]
    }
  ],
  "orders": [...]
}
```

---

## 8. Google Ads — Real-Time (GAQL)

### For Users

Shows Google Ads performance pulled live via the Google Ads Query Language (GAQL). Google uses `cost` (not `spend`) and `conversions` (not `purchases`). Cost is stored in micros (millionths) internally.

### Route File

`src/routes/googleAdsAnalyticsRoutes.js`
**Base:** `GET /api/v1/google-ads/analytics`
**Auth:** `authenticate` (standard JWT) | **Cache:** 120 s minimum

### Credential DB Query

```sql
-- File: src/integrations/shared/platformCredentials.js
SELECT id, brand_id, company_id, account_id, access_token,
       refresh_token, token_expiry, extra_data, is_active
FROM core.brand_envs
WHERE platform = 'google-ads'
  AND brand_id = $1 AND company_id = $2
  AND is_active = true
LIMIT 1
```

Token refresh SQL (if `token_expiry < now + 5 min`):

```sql
UPDATE core.brand_envs
SET access_token = $1, token_expiry = $2, updated_at = NOW()
WHERE id = $3
```

Customer ID: stored in `account_id`. Normalized (dashes removed): `123-456-7890` → `1234567890`
Optional `customer_id`/`account_id` query param overrides stored value.

### Data Source

Google Ads API v22 via GAQL:

```
POST https://googleads.googleapis.com/v22/customers/{customer_id}/googleAds:search
Headers:
  Authorization: Bearer {access_token}
  developer-token: {GOOGLE_ADS_DEVELOPER_TOKEN}
Body: { "query": "SELECT ... FROM resource WHERE segments.date BETWEEN ..." }
```

### API Endpoints


| Path                          | Handler                     | Notes                             |
| ----------------------------- | --------------------------- | --------------------------------- |
| GET `/campaigns`              | `getCampaignPerformance()`  | Campaign-level metrics            |
| GET `/campaigns/:id/adgroups` | `getCampaignAdGroups()`     | Ad groups for one campaign (lazy) |
| GET `/ad-groups`              | `getAdGroupPerformance()`   | All ad groups                     |
| GET `/adgroups/:id/ads`       | `getAdGroupAds()`           | Ads for one ad group (lazy)       |
| GET `/ads`                    | `getAdPerformance()`        | All ads                           |
| GET `/keywords`               | `getKeywordPerformance()`   | Keyword data                      |
| GET `/search-terms`           | `getSearchTermAnalysis()`   | Search terms report               |
| GET `/demographics`           | `getDemographicAnalytics()` | Age / gender                      |
| GET `/geographic`             | `getGeographicAnalytics()`  | Location performance              |
| GET `/devices`                | `getDeviceAnalytics()`      | Device breakdown                  |
| GET `/spend`                  | `getSpendAnalytics()`       | Daily spend                       |
| GET `/time-based`             | `getTimeBasedPerformance()` | Hourly/daily patterns             |
| GET `/quality-score`          | `getQualityScoreMetrics()`  | Keyword quality scores (1-10)     |
| GET `/competitive`            | `getCompetitiveMetrics()`   | Impression share                  |
| GET `/shopping`               | `getShoppingPerformance()`  | Shopping campaigns                |
| GET `/audiences`              | `getAudiencePerformance()`  | Audience segments                 |
| GET `/budget-pacing`          | `getBudgetPacing()`         | Budget utilization                |
| GET `/account-info`           | inline GAQL                 | Account status                    |
| GET `/dashboard`              | `getGoogleAdsDashboard()`   | Combined all                      |


### Core Fields (GAQL → Response)


| Response Field           | GAQL Column                       | Notes                             |
| ------------------------ | --------------------------------- | --------------------------------- |
| `total_cost`             | `metrics.cost_micros / 1,000,000` | Stored in micros internally       |
| `total_impressions`      | `metrics.impressions`             |                                   |
| `total_clicks`           | `metrics.clicks`                  |                                   |
| `total_conversions`      | `metrics.conversions`             | All configured conversion actions |
| `total_conversion_value` | `metrics.conversions_value`       |                                   |


> ⚠️ **DO NOT use `total_conversions` or `total_conversion_value` from Google Ads API for actual order counts or attributed revenue.**  
> Google's conversion tracking uses its own attribution model (last-click, data-driven, etc.) with configurable lookback windows, which diverges from actual Shopify orders.  
> **Source of truth for order counts:** Use Shopify attributed data (UTM-tagged Shopify orders where `utm_source = 'google'` or `utm_medium = 'cpc'`). Available via channel attribution routes (`/api/v1/channel-attribution`).  
> Google API fields like `cost_micros`, `impressions`, `clicks`, `impression_share`, `quality_score` ARE reliable and should come from the Google Ads API.
> | `view_through_conversions` | `metrics.view_through_conversions` | Seen but not clicked |
> | `quality_score` | `ad_group_criterion.quality_info.quality_score` | Keyword level only |
> | `impression_share` | `metrics.search_impression_share` | |

### Derived Metrics

```
overall_roas        = total_conversion_value / total_cost
average_ctr         = total_clicks / total_impressions × 100
average_cpc         = total_cost / total_clicks
average_cpm         = (total_cost / total_impressions) × 1000
conversion_rate     = total_conversions / total_clicks × 100
cost_per_conversion = total_cost / total_conversions
profit              = total_conversion_value - total_cost
profit_margin       = (profit / total_conversion_value) × 100
```

### Example GAQL Query

```sql
SELECT
  campaign.id, campaign.name, campaign.status,
  metrics.impressions, metrics.clicks, metrics.cost_micros,
  metrics.conversions, metrics.conversions_value,
  segments.date
FROM campaign
WHERE segments.date BETWEEN '2025-12-01' AND '2025-12-31'
  AND campaign.status != 'REMOVED'
ORDER BY metrics.cost_micros DESC
```

### Segmentation Dimensions


| Dimension | GAQL Column                         | Example Values                       |
| --------- | ----------------------------------- | ------------------------------------ |
| Device    | `segments.device`                   | `MOBILE`, `DESKTOP`, `TABLET`        |
| Gender    | `ad_group_criterion.gender.type`    | `MALE`, `FEMALE`, `UNDETERMINED`     |
| Age Range | `ad_group_criterion.age_range.type` | `AGE_RANGE_25_34`, `AGE_RANGE_35_44` |


---

## 9. Google Attribution — Historical (ClickHouse)

### Key Distinction

- `/api/v1/google-ads/analytics` = **Real-time Google Ads API** (GAQL)
- `/api/v1/google-attribution` = **ClickHouse historical tables** (`fct_google_ads_daily`, `fct_google_campaigns_hourly`, `fct_order_attribution`)

### Route File

`src/routes/googleAttributionRoutes.js`
**Base:** `GET /api/v1/google-attribution`
**Auth:** `authenticate` (standard JWT) | **Cache:** 300 s fixed

### Data Sources (ClickHouse)


| Table                           | Used When                                      |
| ------------------------------- | ---------------------------------------------- |
| `fct_google_campaigns_hourly`   | Single-day range or `time_aggregation=hourly`  |
| `fct_google_ads_daily`          | Multi-day range or `time_aggregation=daily`    |
| `fct_google_ads_status_history` | Campaign/ad status changes                     |
| `fct_order_attribution`         | Attribution linking (`lt_platform = 'google'`) |
| `fct_orders`                    | Order details                                  |
| `fct_order_items`               | Line items                                     |


### API Endpoints


| Path    | Params                                                                                           | Description                        |
| ------- | ------------------------------------------------------------------------------------------------ | ---------------------------------- |
| GET `/` | `brand_id`, `start_date`, `end_date`, `time_aggregation`?, `campaign_id`?, `adset_id`?, `ad_id`? | Campaign → ad group → ad hierarchy |


Same response shape as Meta Attribution.

---

## 10. GA4 Analytics

### For Users

Google Analytics 4 data — sessions, users, page views, events, and ecommerce conversions. Requires a GA4 property connected via OAuth.

### Route File

`src/routes/ga4AnalyticsRoutes.js`
**Base:** `GET /api/v1/ga4/analytics`
**Auth:** `authenticate` (standard JWT)

### Credential DB Query

```sql
SELECT id, platform, account_id, access_token, refresh_token, token_expiry, extra_data, is_active
FROM core.brand_envs
WHERE platform = 'ga4'
  AND brand_id = $1 AND company_id = $2
  AND is_active = true
LIMIT 1
```

Property ID resolution (from `extra_data`):

1. `req.query.property_id` (override)
2. `extra_data.preferred_property_id` or `extra_data.preferredPropertyId`
3. `row.account_id` (fallback)

Token refresh: same logic as Google Ads. If `token_expiry IS NULL`, forces `refreshGoogleToken()`.
`extra_data.all_properties[]` stores all linked GA4 properties.

### Data Source

GA4 Data API (real-time — not ClickHouse):

```
POST https://analyticsdata.googleapis.com/v1beta/{property_id}:runReport
Headers: Authorization: Bearer {access_token}
```

### API Endpoints


| Path                   | Handler                         | Description                       |
| ---------------------- | ------------------------------- | --------------------------------- |
| GET `/overview`        | `analytics.getOverview()`       | Users, sessions, events, revenue  |
| GET `/traffic-sources` | `analytics.getTrafficSources()` | Traffic by source/medium          |
| GET `/pages`           | `analytics.getPages()`          | Top pages, page views, engagement |
| GET `/events`          | `analytics.getEvents()`         | Event counts                      |
| GET `/ecommerce`       | `analytics.getEcommerce()`      | Purchase/revenue events           |
| GET `/realtime`        | `analytics.getRealtime()`       | Active users now                  |
| GET `/demographics`    | `analytics.getDemographics()`   | Age / gender / location           |
| GET `/devices`         | `analytics.getDevices()`        | Device categories                 |
| GET `/conversions`     | `analytics.getConversions()`    | Conversion events                 |
| GET `/retention`       | `analytics.getRetention()`      | User retention                    |
| GET `/dashboard`       | `analytics.getDashboard()`      | Combined metrics                  |


### Core Fields (GA4 → Response)


| Field                | GA4 Dimension/Metric     | Notes     |
| -------------------- | ------------------------ | --------- |
| `sessions`           | `sessions`               |           |
| `users`              | `totalUsers`             |           |
| `newUsers`           | `newUsers`               |           |
| `eventCount`         | `eventCount`             |           |
| `purchases`          | `transactions`           | Ecommerce |
| `revenue`            | `purchaseRevenue`        | Ecommerce |
| `bounceRate`         | `bounceRate`             |           |
| `avgSessionDuration` | `averageSessionDuration` |           |
| `engagedSessions`    | `engagedSessions`        |           |
| `engagementRate`     | `engagementRate`         |           |


GA4 uses `conversions` (not `purchases`) for generic conversion events. Ecommerce uses `transactions`.

---

## 11. Historical Dashboard (ClickHouse Gold Layer)

### For Users

Aggregated P&L dashboard from pre-computed ClickHouse gold tables. Combines data from all channels into a single view. Faster than real-time endpoints.

### Route File

`src/routes/historicalAnalyticsRoutes.js`
**Base:** `GET /api/v1/historical`
**Auth:** `authenticate` (standard JWT) | **Cache:** 300 s dynamic

### Data Sources (ClickHouse Gold Layer)

Tables: gold-layer fact tables (exact names in `src/integrations/historicalAnalytics/analytics.js`)

### API Endpoints


| Path             | Handler                              | Returns                                                                                            |
| ---------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------- |
| GET `/dashboard` | `analytics.getHistoricalDashboard()` | Net Profit, Total Sales, Total Ad Spend, Total COGS, Total Orders, Returns/Cancels, Total Payments |


### Parameters


| Param        | Required             | Notes                  |
| ------------ | -------------------- | ---------------------- |
| `brand_id`   | No (defaults to JWT) |                        |
| `company_id` | No (defaults to JWT) |                        |
| `start_date` | Yes                  | ISO 8601 or YYYY-MM-DD |
| `end_date`   | Yes                  | ISO 8601 or YYYY-MM-DD |


### Metric Formula Breakdown (from `gold.fct_daily_pnl`)

All metrics are aggregated from the daily P&L gold table using `argMax(..., _gold_created_at)` deduplication per `(brand_id, date, channel)`.

#### Three COGS Figures in the System

#### Three COGS Figures in the System

| | `gross_cogs` | `net_cogs` / `total_cogs` | P&L `cogs` |
|---|---|---|---|
| **Source** | `fct_order_items.gross_cogs` | `fct_order_items.net_cogs` = `fct_order_items.total_cogs` (identical formula, two aliases) | `gold.fct_daily_pnl.product_cost` |
| **`pnl_refund_class = 'ACTIVE'`** | full gross_cogs (product + ship + pack + gateway) | product + ship + pack + gateway | product cost only |
| **`pnl_refund_class = 'CANCELLATION'`** | full gross_cogs (product incl.) | gateway fees ONLY — no product, no ship, no pack | product cost only |
| **`pnl_refund_class = 'RETURN'`** | full gross_cogs (product incl.) | `placed_shipping_cost` + `placed_packaging_cost` + `placed_gateway_fee` (online only) + `rto_cost` (return-leg shipping). **NO product cost.** | product cost only |
| **Amazon** | product + all fees, all statuses | CANCELLATION+NONE→0; NONE (active)→product_cost; RETURN→algebraic net; else→product+fees | product_cost column |
| **Used in** | Attribution models (`grossCogsSumExpr()`) | Historical analytics dashboard (`analyticsClickhouse.js → netCogsSumExpr()`) | P&L routes (`/api/v1/pnl/cogs`) |
| **API field name** | `gross_cogs` | `total_cogs` (JS: `lineItemTotals.total_cogs + amazonCogs`) | `cogs` |

#### What `pnl_refund_class = 'ACTIVE'` means

`pnl_refund_class` is a computed classification on `fct_order_items`, not Shopify's `financial_status` or `fulfillment_status`. Its values are:

| `pnl_refund_class` | What Shopify orders it covers |
|---|---|
| `'ACTIVE'` | **Every placed order that is not a cancellation or return** — includes paid, pending, payment_pending, unfulfilled, partially_fulfilled, processing, authorized, partially_paid, unpaid. All contribute to both COGS and revenue. |
| `'CANCELLATION'` | Orders with `cancelled_at IS NOT NULL` — costed on the cancel event date |
| `'RETURN'` | Orders with a return/refund — costed on the return event date |

> `pnl_refund_class = 'ACTIVE'` is **not** the same as `financial_status = 'paid'`. Pending, unfulfilled, and unpaid orders all fall under `'ACTIVE'` and all contribute to both COGS and revenue. The `is_placement_gross_eligible = 1` flag further controls which rows enter the placement CTE.

> **`revenue_eligible` flag:** This is an internal line-item flag used for specific sub-calculations (e.g., attribution eligibility filters). It does **not** control inclusion in `total_revenue` or `net_revenue` — payment_pending orders are included in both revenue and COGS regardless of this flag. All `pnl_refund_class = 'ACTIVE'` orders contribute to revenue and COGS.

> **`total_cogs` = `net_cogs`** — in `lineItemHistoricalSql.js` both aliases use the exact same SQL expression. The dashboard exposes `total_cogs` as the headline COGS figure; `net_cogs` and `gross_cogs` are also returned for drill-down.

> **P&L service `cogs` ≠ historical `total_cogs`** — the `/pnl/cogs` endpoint maps `gold.fct_daily_pnl.product_cost` to `cogs`, which is product cost only. The historical dashboard `total_cogs` also includes shipping, packaging, gateway fees, RTO, and return logistics costs. These two figures will not reconcile.

#### Net COGS / Total COGS — Full Composition (from `lineItemHistoricalSql.js`)

```
total_cogs = net_cogs =

  ── pnl_refund_class = 'ACTIVE' (placement date axis) ──
  │  Covers: paid + pending + payment_pending + unfulfilled + partially_fulfilled
  │  + processing + authorized + partially_paid + unpaid — ALL non-cancel/non-return orders
  + SUM(oi.total_cost)             WHERE pnl_refund_class = 'ACTIVE'   ← product cost
  + SUM(oi.placed_shipping_cost)   WHERE pnl_refund_class = 'ACTIVE'
  + SUM(oi.placed_packaging_cost)  WHERE pnl_refund_class = 'ACTIVE'
  + SUM(oi.placed_gateway_fee)     WHERE pnl_refund_class = 'ACTIVE' AND is_online_payment
  (Filter: is_placement_gross_eligible = 1 AND is_gift_card = 0)

  ── pnl_refund_class = 'CANCELLATION' (cancel event date axis) ──
  + SUM(oi.placed_gateway_fee)     WHERE is_cancelled_line = 1 AND is_online_payment
    (gateway fee only — product cost = 0, no shipping, no packaging)

  ── pnl_refund_class = 'RETURN' (return event date axis) ──
  + SUM(oi.placed_shipping_cost)   WHERE is_return_line = 1
  + SUM(oi.placed_packaging_cost)  WHERE is_return_line = 1
  + SUM(oi.placed_gateway_fee)     WHERE is_return_line = 1 AND is_online_payment
  + SUM(oi.rto_cost)               WHERE is_return_line = 1   ← return-to-origin shipping (return leg)
    (NO product cost — product came back)

  ── Amazon (CASE on pnl_refund_status + payout_basis; covers ACTIVE + ESTIMATED + RETURN + ADJUSTMENT) ──
  + CASE
      WHEN payout_basis = 'NONE' AND pnl_refund_status = 'CANCELLATION' → 0
      WHEN payout_basis = 'NONE'  (non-cancel)                          → product_cost
      WHEN pnl_refund_status = 'RETURN'
        → item_revenue + item_refunds + item_commission + item_closing
          + item_shipping + item_tax_withheld + item_other_fees  (algebraic net)
      ELSE (ACTIVE / ESTIMATED / ADJUSTMENT)
        → product_cost + ABS(item_commission) + ABS(item_closing)
          + ABS(item_shipping) + ABS(item_tax_withheld) + ABS(item_other_fees)
    END

  Gift cards excluded in all arms.
```

#### Gross COGS — Composition (from `lineItemHistoricalSql.js`)

```
gross_cogs =
  + SUM(oi.gross_cogs) WHERE pnl_refund_class = 'ACTIVE'
    (all placed orders: paid + pending + unfulfilled + payment_pending + all non-cancel/non-return)
  + SUM(oi.gross_cogs) WHERE is_cancelled_line = 1   ← full gross_cogs incl. product
  + SUM(oi.gross_cogs) WHERE is_return_line = 1       ← full gross_cogs incl. product
  + SUM(oi.rto_cost)   WHERE is_return_line = 1       ← RTO added on top
  + amazon_gross_cogs  = product_cost + ABS(all fees), ALL non-cancellation statuses
```

#### Dashboard JS Aggregation (`analyticsClickhouse.js`)

```javascript
// Historical dashboard — line-item buckets already merge Shopify + Amazon.
// Do NOT add amazonSpSales totals again (would double-count).
const totalCogs = parseFloat(lineItemTotals.total_cogs || 0);
const grossSales = parseFloat(lineItemTotals.gross_sales || 0);
// amazonSales from getAmazonSpSales() is used for breakdown objects only.
```

Amazon slice inside `lineItemHistoricalSql.js` (`amazon_daily` CTE):

```
amazon_gross_revenue = SUM(item_revenue + item_tax_withheld)   -- matches §4 gross_sales per item
amazon_net_revenue   = SUM(item_revenue + item_refunds + item_tax_withheld)
amazon_platform_fees = SUM(|commission| + |closing| + |shipping| + |tax_withheld| + |other|)
```

| Metric                | Formula                                     | Source Column(s)                               | Notes                                                                       |
| --------------------- | ------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------- |
| **Total Sales**       | `SUM(gross_revenue)`                        | `fct_daily_pnl.gross_revenue`                  | Pre-aggregated gross revenue per day per channel                            |
| **Returns / Cancels** | `SUM(refund_amount) + SUM(cancel_amount)`   | `fct_daily_pnl.refund_amount`, `cancel_amount` | Separate columns for returns vs cancellations                               |
| **Net Revenue**       | `Total Sales - Returns - Cancels`           | derived                                        | Equivalent to Shopify `total_revenue - refunded_amount - cancelled_revenue` |
| **Total COGS**        | `SUM(net_cogs)` = `SUM(total_cogs)` via `netCogsSumExpr()` | `fct_order_items.net_cogs` | Net COGS: active=product+ship+pack+gateway; cancelled=gateway only (online); returns=`rto_cost`+gateway only (`rto_cost` IS the return shipping cost — no placed_shipping_cost, no placed_packaging_cost, no product cost). |
| **Gross COGS**        | `SUM(gross_cogs)` via `grossCogsSumExpr()`  | `fct_order_items.gross_cogs`                   | All placement costs incl. cancelled/returned. Used in attribution only.     |
| **Total Ad Spend**    | `SUM(total_spend)`                          | `fct_daily_pnl.total_spend`                    | Combined spend across Meta + Google + Amazon Ads                            |
| **Total Orders**      | `SUM(total_orders)`                         | `fct_daily_pnl.total_orders`                   | All orders in period                                                        |
| **Total Payments**    | `SUM(net_payout)`                           | `fct_daily_pnl.net_payout`                     | Actual money received (after platform fees deducted)                        |
| **Gross Profit**      | `Net Revenue - Total COGS`                  | derived                                        | Uses net_cogs for COGS deduction                                            |
| **Net Profit**        | `Net Revenue - Total COGS - Ad Spend`       | derived                                        | Bottom-line profitability                                                   |
| **Gross Margin %**    | `(Gross Profit / Net Revenue) × 100`        | derived                                        |                                                                             |
| **Net Margin %**      | `(Net Profit / Net Revenue) × 100`          | derived                                        |                                                                             |
| **Total ROAS**        | `Net Revenue / Total Ad Spend`              | derived                                        | Blended across all paid channels                                            |
| **Gross ROAS**        | `Total Sales / Total Ad Spend`              | derived                                        | Before deducting returns/cancels                                            |

#### /pnl Route COGS (Different — Product Cost Only)

> The `/api/v1/pnl/cogs` endpoint reads `gold.fct_daily_pnl.product_cost` and returns it as `cogs`. This is product cost only — it does NOT include shipping, packaging, gateway fees, or RTO costs. Do not compare this directly to the historical dashboard `total_cogs` figure; they measure different things.

#### SQL Pattern (Historical Dashboard — Net COGS from `fct_order_items`)

```sql
-- analyticsClickhouse.js getTotalCOGS(): net_cogs aggregated by channel
WITH order_channel AS (
  SELECT a.brand_id, a.order_id, any(channel_expr) AS channel
  FROM fct_order_attribution AS a
  WHERE a.brand_id = {brandId}
    AND a.order_date BETWEEN toDate({startDate}) AND toDate({endDate})
    AND {attributionOrderFilter}          -- excludes test + voided
  GROUP BY a.brand_id, a.order_id
)
SELECT
  coalesce(oc.channel, 'other')           AS channel,
  sum(toFloat64(coalesce(i.net_cogs, 0))) AS channel_cogs   -- netCogsSumExpr()
FROM fct_order_items AS i
LEFT JOIN order_channel AS oc ON oc.brand_id = i.brand_id AND oc.order_id = i.order_id
WHERE i.brand_id = {brandId}
  AND i.order_date BETWEEN toDate({startDate}) AND toDate({endDate})
  AND coalesce(i.is_gift_card, 0) = 0
GROUP BY channel
```

#### SQL Pattern (Attribution — Gross COGS from `fct_order_items`)

```sql
-- analyticsChannelClickhouse.js: gross_cogs per order
SELECT
  oi.order_id,
  sum(toFloat64(coalesce(oi.gross_cogs, 0))) AS total_cogs   -- grossCogsSumExpr()
FROM fct_order_items AS oi
WHERE oi.brand_id = {brandId}
  AND toString(oi.order_id) IN {orderIds}
  AND coalesce(oi.is_gift_card, 0) = 0
  AND oi.is_test = 0
GROUP BY oi.order_id
```

---

## 12. Channel Attribution (ClickHouse)

### For Users

Organic channel attribution — which marketing channel drove each order, tracked via UTM parameters. Covers organic traffic; other platforms use their own attribution routes.

### Route File

`src/routes/organicAttributionRoutes.js`
**Base:** `GET /api/v1/channel-attribution`
**Auth:** `authenticate` (standard JWT) | **Cache:** 300 s fixed

### Data Sources (ClickHouse)


| Table                   | Description                                     |
| ----------------------- | ----------------------------------------------- |
| `fct_order_attribution` | Attribution rows with `lt_platform = 'organic'` |
| `fct_orders`            | Order details                                   |
| `fct_order_items`       | Line items                                      |
| `dim_sku`               | SKU master                                      |

> **COGS in Attribution:** Uses **`gross_cogs`** (`grossCogsSumExpr()`) — cost at placement time for ALL orders regardless of cancellation or return status. This is intentional: attribution models allocate costs as they were incurred at order placement, not adjusted for post-placement events.

### API Endpoints


| Path    | Params                                           | Description                                     |
| ------- | ------------------------------------------------ | ----------------------------------------------- |
| GET `/` | `brand_id`, `start_date`, `end_date`, `channel`? | Orders attributed to organic / specific channel |


---

## 13. P&L Aggregated Routes

### For Users

Pre-computed P&L metrics from `gold.fct_daily_pnl` ClickHouse table. Each endpoint returns one specific metric slice. Faster than assembling across platforms.

### Route File

`src/routes/pnlRoutes.js`
**Base:** `GET /api/v1/pnl`
**Auth:** `authenticate` (standard JWT) | **Cache:** None

### Core Service

`src/services/pnlService.js → getPnlMetrics(brandId, companyId, startDate, endDate)`
Reads from: `gold.fct_daily_pnl` ClickHouse table.

### API Endpoints


| Path                         | Response Field                   | Source Column                          |
| ---------------------------- | -------------------------------- | -------------------------------------- |
| GET `/summary`               | Full metrics + marketing objects | All columns combined                   |
| GET `/metrics`               | `buildMetricsSection()` output   | Core P&L metrics                       |
| GET `/marketing`             | `buildMarketingSection()` output | Marketing spend breakdown              |
| GET `/total_sales`           | `totalSales`                     |                                        |
| GET `/net_sales`             | `netSales`                       |                                        |
| GET `/return_leakage_refund` | `returns`                        | `gold.fct_daily_pnl.returns_excl_tax`  |
| GET `/cancelled`             | `cancelled`                      | `gold.fct_daily_pnl.cancelled_revenue` |
| GET `/meta_ads_cost`         | `metaAdsCost`                    |                                        |
| GET `/google_ads_cost`       | `googleAdsCost`                  |                                        |
| GET `/packaging_cost`        | `packagingCost`                  | `gold.fct_daily_pnl.packaging_cost`    |


### Parameters


| Param        | Required             | Notes      |
| ------------ | -------------------- | ---------- |
| `brand_id`   | No (defaults to JWT) |            |
| `company_id` | No (defaults to JWT) |            |
| `startDate`  | Yes                  | YYYY-MM-DD |
| `endDate`    | Yes                  | YYYY-MM-DD |


---

## 14. Cross-Platform Derived Metrics

These require assembling data from multiple platform routes.

### 14.1 Dashboard ROAS Cards

#### Gross ROAS

```
Gross ROAS = total_revenue / total_ad_spend

total_revenue:
  Shopify → total_revenue (all orders incl. cancelled)
  Amazon SP → total_order_total

total_ad_spend:
  Meta    → GET /api/v1/meta/analytics/spend  → spend
  Google  → GET /api/v1/google-ads/analytics/spend → total_cost
  Amazon  → ClickHouse fct_amazon_ads_campaigns_daily → SUM(cost)
```

#### Net ROAS

```
Net ROAS = net_revenue / total_ad_spend

net_revenue:
  Shopify   → total_revenue - refunded_amount
              (total_revenue = ALL orders: paid + unpaid + pending, NOT just paid)
  Amazon SP → effective_net_payout (after all Amazon fees)
```

#### Break-Even ROAS (BE ROAS)

```
BE ROAS = (total_revenue - COGS) / total_ad_spend

COGS:
  Shopify → cost_of_goods_sold (GraphQL ProductVariant.inventoryItem.unitCost.amount)
  Amazon SP → cogs_product (from fct_amazon_sp_order_pnl)
              Note: total_cogs = amazon_fees + cogs_product — this is NOT the COGS; use cogs_product only
```

### 14.2 Net Profit Per Platform


| Platform | Formula                                                               |
| -------- | --------------------------------------------------------------------- |
| Shopify  | `net_revenue - cost_of_goods_sold - (meta.spend + google.total_cost)` |
| Amazon   | `effective_net_payout - cogs_product - total_spend`                   |
| Meta     | `conversionValue - spend`                                             |
| Google   | `total_conversion_value - total_cost`                                 |


### 14.3 Total Ad Spend Assembly

```
total_ad_spend = meta.spend + google.total_cost + amazon.total_spend

Sources:
  Meta:   GET /api/v1/meta/analytics/spend
  Google: GET /api/v1/google-ads/analytics/spend
  Amazon: ClickHouse fct_amazon_ads_campaigns_daily → SUM(cost)
```

---

## 15. Terminology Cross-Reference

Different platforms use different words for the same concepts:


| Concept          | Shopify              | Amazon SP                       | Amazon Ads               | Meta Ads           | Google Ads               | GA4               |
| ---------------- | -------------------- | ------------------------------- | ------------------------ | ------------------ | ------------------------ | ----------------- |
| Ad spend         | N/A                  | N/A                             | `spend` (= `cost` in CH) | `spend`            | `total_cost`             | N/A               |
| Revenue          | `total_revenue`      | `order_total` / `gross_revenue` / `total_gross_revenue` | `sales` (attributed)     | `conversionValue`  | `total_conversion_value` | `purchaseRevenue` |
| Net revenue      | `net_revenue`        | `effective_net_payout`          | `total_net_payout`       | `profit` (partial) | `profit` (partial)       | N/A               |
| Gross Sales (UI) | N/A                  | `total_gross_revenue`           | same as SP               | N/A                | N/A                      | N/A               |
| Amazon fees      | N/A                  | `total_amazon_fees` (per-order sum) | `total_amazon_fees`  | N/A                | N/A                      | N/A               |
| Product cost     | `cost_of_goods_sold` | `cogs_product`                  | `total_product_cost`     | N/A                | N/A                      | N/A               |
| Orders/Purchases | `total_orders`       | `total_orders`                  | `attributed_orders`      | `purchases`        | `total_conversions`      | `transactions`    |
| Sessions         | N/A                  | N/A                             | N/A                      | N/A                | N/A                      | `sessions`        |
| Click rate       | N/A                  | N/A                             | `ctr`                    | `ctr`              | `average_ctr`            | N/A               |
| Cost per click   | N/A                  | N/A                             | `cpc`                    | `cpc`              | `average_cpc`            | N/A               |
| ROAS             | derived              | derived                         | `ads_roas`               | `roas`             | `overall_roas`           | N/A               |
| ACoS             | N/A                  | N/A                             | `acos` / `ads_acos`      | N/A                | N/A                      | N/A               |
| COGS             | `cost_of_goods_sold` | `total_cogs` (= fees + `cogs_product`) | `total_cogs`             | N/A                | N/A                      | N/A               |
| Date axis        | `created_at`         | `purchase_date`                 | `report_date`            | date range         | `segments.date`          | date range        |


### Real-time vs ClickHouse Routes


| Platform   | Real-time Route                | ClickHouse Route                |
| ---------- | ------------------------------ | ------------------------------- |
| Meta       | `/api/v1/meta/analytics`       | `/api/v1/meta-attribution`      |
| Google     | `/api/v1/google-ads/analytics` | `/api/v1/google-attribution`    |
| Amazon SP  | N/A                            | `/api/v1/amazon-organic-orders` |
| Amazon Ads | N/A                            | `/api/v1/amazon-attribution`    |
| GA4        | `/api/v1/ga4/analytics`        | N/A                             |
| Shopify    | `/api/v1/shopify/analytics`    | N/A                             |


---

## 16. Date Handling


| Platform                | Date Field             | Timezone                                 | API Format                                        |
| ----------------------- | ---------------------- | ---------------------------------------- | ------------------------------------------------- |
| Shopify                 | `created_at` on orders | JWT → extra_data → brands.timezone → IST | ISO 8601 (`2025-12-01T00:00:00Z`) or `YYYY-MM-DD` |
| Amazon SP Organic       | `purchase_date`        | UTC stored in ClickHouse                 | `YYYY-MM-DD`                                      |
| Amazon Ads              | `report_date`          | UTC stored in ClickHouse                 | `YYYY-MM-DD`                                      |
| Meta Ads (real-time)    | date range             | Account timezone (`extra_data.timezone`) | `YYYY-MM-DD`                                      |
| Google Ads (real-time)  | `segments.date`        | Account timezone                         | `YYYY-MM-DD`                                      |
| Meta Attribution (CH)   | depends on table       | ClickHouse stored                        | `YYYY-MM-DD` or ISO 8601                          |
| Google Attribution (CH) | depends on table       | ClickHouse stored                        | `YYYY-MM-DD` or ISO 8601                          |
| GA4                     | date dimension         | Account timezone                         | `YYYY-MM-DD`                                      |
| Historical / P&L        | depends on table       | ClickHouse stored                        | `YYYY-MM-DD` or ISO 8601                          |


### ClickHouse Date Filter Pattern

```sql
toDate(purchase_date) >= toDate({startDate:String})
AND toDate(purchase_date) <= toDate({endDate:String})
```

### Date Param Validation

All routes validate:

- Both `start_date` and `end_date` must be present
- Both must parse as valid dates
- `start_date` must be ≤ `end_date`

Exception: `/shopify/analytics/orders/last` and `/shopify/analytics/customer-journey` skip date validation.

---

## 17. Order Exclusion, Cancellation & Refund Rules

### 17.1 Cancellation Rules


| Platform           | Field / Condition                                              | Effect                                                                                                                                                                                                   |
| ------------------ | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Shopify            | `cancelled_at IS NOT NULL`                                     | Order excluded from `paid_revenue`, `net_revenue`, AOV. Still counted in `total_orders` until filtered.                                                                                                  |
| Shopify            | `financial_status = 'voided'`                                  | Also excluded — voided = payment reversed before capture                                                                                                                                                 |
| Amazon SP          | `order_status IN ('Cancelled', 'canceled')` (case-insensitive) | Excluded from all P&L; counted in `canceled_orders` counter                                                                                                                                              |
| Amazon SP          | `payout_basis = 'NONE'` (non-cancel) | Revenue/fees/net_payout: aggregate all `effective_*` columns normally (§17.1). **COGS only:** `net_cogs` uses `product_cost` when `payout_basis = 'NONE'` and status ≠ `CANCELLATION` — see `lineItemHistoricalSql.js`. |
| Amazon Attribution | Same SP exclusions apply to the joined SP orders               |                                                                                                                                                                                                          |
| Meta / Google      | Platform does not expose individual order data                 | N/A — only aggregate spend/conversion totals                                                                                                                                                             |
| GA4                | Platform does not expose refund events by default              | N/A — `purchaseRevenue` is gross                                                                                                                                                                         |


### 17.2 Refund / Return Rules

Refunds reduce `net_revenue`. They are handled differently per platform:

#### Shopify Refunds

```
Refund object fields:
  refund.created_at      ← Date the refund was processed
  refund.refund_amount   ← Amount returned to customer
  refund.transactions[]  ← Each transaction that was refunded

Shopify return filter (ClickHouse or API):
  WHERE returned_at >= start_date AND returned_at <= end_date
  ↑ Use returned_at (when the return/refund was received back), NOT refunded_at

Key distinction:
  - The ORDER is created_at = order date
  - The RETURN is returned_at = when the return was received/processed (can be days/weeks later)
  - net_revenue counts refund_amount deducted from total_revenue on the order's date axis (created_at)
    NOT on the return date. This matches Shopify's native reports.
  - Field name is returned_at, not refunded_at — do not confuse these.

Formula reminder:
  total_revenue   = SUM(order_total) for ALL orders (paid + unpaid + pending) in the date range
  refunded_amount = SUM(refund.refund_amount) for refunds on those orders
  net_revenue     = total_revenue - refunded_amount
```

#### Amazon SP Refunds

```
Amazon refund columns in fct_amazon_sp_order_pnl:
  effective_refunds          ← Negative value (refund deduction from payout)
  fee_refund_commission      ← Negative (commission taken on refunded item)
  fee_refund_commission_returned ← Positive (Amazon returns this commission on refund)
  refund_principal           ← Positive (item value returned)
  total_refund_amount        ← Positive (total outflow for the refund)

Net payout after refunds:
  effective_net_payout already accounts for refunds — no separate deduction needed.
```

#### Meta / Google Ads Refunds

```
Neither Meta Graph API nor Google Ads API surfaces refunds.
conversionValue (Meta) and total_conversion_value (Google) are gross.
Cross-check against Shopify net_revenue for true net ROI.
```

### 17.3 Order Status Reference

#### Shopify `financial_status` Values


| Status               | Included in `total_revenue` | Included in `paid_revenue` | In `net_revenue`                                |
| -------------------- | --------------------------- | -------------------------- | ----------------------------------------------- |
| `paid`               | ✅                           | ✅                          | ✅                                               |
| `pending`            | ✅                           | ❌                          | ✅                                               |
| `partially_paid`     | ✅                           | ❌ (partial)                | ✅                                               |
| `refunded`           | ✅                           | ❌                          | ✅ minus refund                                  |
| `partially_refunded` | ✅                           | ❌                          | ✅ minus partial refund                          |
| `voided`             | ✅                           | ❌                          | ✅ (voided treated as cancelled; no cash impact) |
| `authorized`         | ✅                           | ❌                          | ✅                                               |
| `unpaid`             | ✅                           | ❌                          | ✅                                               |


> **Key rule:** `total_revenue` includes ALL statuses. `paid_revenue` is only `financial_status = 'paid'`.

#### Amazon SP `order_status` Values


| Status                   | Included in P&L                                      |
| ------------------------ | ---------------------------------------------------- |
| `Shipped`                | ✅                                                    |
| `Pending`                | ✅ (always — payout_basis = NONE rows still included) |
| `Unshipped`              | ✅                                                    |
| `Cancelled` / `canceled` | ❌ counted in `canceled_orders` only                  |
| `InvoiceUnconfirmed`     | ✅                                                    |


---

## 18. Amazon Fee Signs Reference

Fee columns in `fct_amazon_sp_order_pnl` are stored as **negative values** (deductions from payout).
Use `Math.abs()` on each **component** when summing costs. Resolve fees **per order** first, then sum — see §4 `resolveAmazonPlatformFees`.


| Column                             | Description                    | Sign in ClickHouse                     |
| ---------------------------------- | ------------------------------ | -------------------------------------- |
| `effective_commission`             | Amazon referral fee            | Negative                               |
| `effective_closing`                | Fixed/variable closing fee     | Negative                               |
| `effective_shipping`               | FBA shipping fee               | Negative                               |
| `effective_other_service_fees`     | Other platform deductions      | Negative                               |
| `effective_tax_withheld`           | TCS/TDS withheld by Amazon     | Negative                               |
| `effective_refunds`                | Refund deductions              | Negative                               |
| `fee_commission`                   | Commission at line level       | Negative                               |
| `fee_fixed_closing`                | Fixed closing at line level    | Negative                               |
| `fee_variable_closing`             | Variable closing at line level | Negative                               |
| `fee_giftwrap_commission`          | Gift wrap fee                  | Negative                               |
| `fee_shipping_hb`                  | Amazon shipping fee            | Negative                               |
| `fee_refund_commission`            | Commission on refunded item    | Negative                               |
| `item_tds`                         | TDS on item                    | Negative                               |
| `tcs_igst`, `tcs_cgst`, `tcs_sgst` | GST components withheld        | Negative                               |
| `fee_refund_commission_returned`   | Commission returned for refund | Positive                               |
| `refund_principal`                 | Refunded item amount           | Positive                               |
| `total_refund_amount`              | Total refund outflow           | Positive                               |
| `total_amazon_fees`                | All fees combined (per order)  | **Positive absolute** when settled — use directly **only if > 0**; if ≤ 0 (net credit on refunds), use component sum (§4) |
| `total_service_fees`               | Alternative fee total          | Same rule as `total_amazon_fees` |


---

## 19. Amazon Attribution — Two P&L Views

The `GET /api/v1/amazon-attribution` endpoint returns two summary objects.
P&L per order is built in `buildAmazonAttributionPayload.js` using `amazonOrderPnl.js` (see §4).

### Attribution dashboard cards → API fields

| UI card | API field | Formula / source |
| ------- | --------- | ---------------- |
| **Gross Sales** | `total_gross_revenue` | `SUM(order_total)` = `SUM(effective_gross_revenue)` — **not** `total_gross_sales` |
| **Net Payout** | `total_net_payout` | `SUM(effective_net_payout)` |
| **Refunds** | `total_refunds` | `SUM(effective_refunds)` (negative) |
| **Amazon Fees** | `total_amazon_fees` | `SUM(per-order amazon_fees)` — includes TCS/TDS in fee total |
| **Product Cost** | `total_product_cost` | `SUM(cogs_product)` |
| **Total COGS** | `total_cogs` | `total_amazon_fees + total_product_cost` |
| **Gross Profit** | `total_gross_profit` | `SUM(net_payout - product_cost)` |
| **Net Profit** | `total_profit` | `total_net_payout - total_product_cost - total_spend` |
| **Total Orders** | `total_orders` | Non-cancelled SP orders in `purchase_date` range |
| **Ad Spend** | `total_spend` | Amazon Ads `SUM(cost)` |

Frontend: `fe-dashboard/src/app/attribution/shared/amazonSummaryMetrics.js` → `amazonDisplayGrossSales()` maps Gross Sales to `total_gross_revenue`.

### `summary` — Channel Level (All SP Orders)

```json
{
  "total_spend":        50000,
  "total_orders":       300,
  "total_order_total":  400000,
  "attributed_orders":  120,
  "organic_orders":     180,
  "attributed_sales":   160000,
  "roas":               8.0,
  "acos":               12.5,
  "ads_roas":           3.2,
  "ads_acos":           31.25,
  "average_cpc":        25.0,
  "average_cpm":        100.0,
  "total_gross_revenue": 380000,
  "total_gross_sales":   375000,
  "total_refunds":       -12000,
  "total_net_payout":   320000,
  "total_amazon_fees":   60000,
  "total_product_cost":  20000,
  "total_cogs":          80000,
  "total_gross_profit": 300000,
  "total_profit":       190000
}
```

- `total_gross_revenue` = sum of order totals — **Gross Sales card**
- `total_gross_sales` = settlement-adjusted (`gross_revenue + tax_withheld`) — internal / not the Gross Sales card
- `total_cogs` = `total_amazon_fees + total_product_cost` (60,000 + 20,000 in example)
- `roas = total_order_total / total_spend` — **TACOS**: all SP sales ÷ ad spend
- `acos = (total_spend / total_order_total) × 100` — channel ACoS
- `ads_roas = attributed_sales / total_spend` — ads-only ROAS
- `total_profit = total_net_payout - total_product_cost - total_spend`

### `attributed_summary` — Ads Level (Ad-Linked Orders Only)

```json
{
  "total_spend":       50000,
  "attributed_orders": 120,
  "attributed_sales":  160000,
  "ads_roas":          3.2,
  "ads_acos":          31.25,
  "total_net_payout":  130000,
  "total_cogs":        40000,
  "total_profit":      90000
}
```

- `ads_roas = attributed_sales / total_spend`
- Only includes SP orders whose `amazon_order_id` appears in campaign `amazon_order_ids`

### When to Use Each View


| Use Case                                                     | View                          |
| ------------------------------------------------------------ | ----------------------------- |
| Channel health — how much of all Amazon revenue costs in ads | `summary.roas` (TACOS)        |
| Campaign ROI — did these specific ads pay off                | `attributed_summary.ads_roas` |
| Total P&L for the brand                                      | `summary.total_profit`        |
| Ad campaign bidding decisions                                | `attributed_summary`          |


---

## 20. COGS, Margin & Derivative Formulas

### 20.1 COGS (Cost of Goods Sold)


| Platform            | COGS Field / Source                                   | Notes                                                                                                                                                               |
| ------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Shopify             | `cost_of_goods_sold`                                  | From Shopify GraphQL: `ProductVariant.inventoryItem.unitCost.amount` × quantity sold                                                                                |
| Amazon SP           | `cogs_product` + `amazon_fees` → `total_cogs` | Product: `cogs_product` only. **Total COGS card** = `amazon_fees + cogs_product`. Do **not** use CH `total_cogs` column on the PnL row as product cost. |
| Amazon Attribution  | `total_cogs` in summary payload               | `SUM(amazon_fees + cogs_product)` for orders in range (or attributed subset) |
| Meta / Google / GA4 | Not available directly                                | Must join with Shopify/Amazon COGS for cross-channel margin                                                                                                         |


### 20.2 Shopify Discount Fields

Shopify exposes discounts as separate fields — these reduce net revenue further if you model pre-discount pricing:

```
Shopify order discount fields:
  total_discounts            ← Sum of all discount codes applied to the order
  discount_applications[]    ← Array of each discount (type, code, amount, target)

If included in net revenue formula:
  adjusted_net_revenue = net_revenue - total_discounts
  (default: net_revenue does NOT deduct discounts — they're already baked into order totals)
```

> **Note:** `order_total` in Shopify already reflects discounts. `total_discounts` is for reporting breakdown only, not double-counted.

### 20.3 Margin Formulas

#### Gross Margin

```
Gross Margin (₹) = net_revenue - COGS
Gross Margin (%) = (Gross Margin ₹ / net_revenue) × 100

Shopify:
  Gross Margin (₹) = net_revenue - cost_of_goods_sold

Amazon SP:
  Gross Margin (₹) = effective_net_payout - cogs_product
                     ↑ Use cogs_product (product cost only), NOT total_cogs
                       total_cogs = amazon_fees + cogs_product — that's the combined total, not just product cost
```

#### Net / Contribution Margin

```
Net Margin (₹) = Gross Margin (₹) - total_ad_spend
Net Margin (%) = (Net Margin ₹ / net_revenue) × 100

Contribution Margin = net_revenue - COGS - total_ad_spend - operating_expenses

Per platform:
  Shopify:  net_revenue - cost_of_goods_sold - (meta.spend + google.total_cost)
  Amazon:   effective_net_payout - cogs_product - amazon.total_spend
            ↑ cogs_product = product cost only; total_cogs (= fees+product) is NOT used here
```

#### Break-Even ROAS

The minimum ROAS at which ad spend breaks even against gross margin:

```
BE ROAS = 1 / Gross_Margin%
        = 1 / (1 - COGS%)

Where:
  Gross_Margin% = (net_revenue - COGS) / net_revenue
  COGS%         = COGS / net_revenue

Example:
  COGS = 400, net_revenue = 1000  →  COGS% = 40%,  GM% = 60%
  BE ROAS = 1 / 0.60 = 1.67
  (every ₹1 of ad spend must return ≥₹1.67 to cover product cost)

Note: Section 14.1 also shows:
  BE ROAS = (total_revenue - COGS) / total_ad_spend
  ← This is "Gross Profit ROAS" (actual achieved ratio), NOT the break-even threshold.
     Use §20.3 formula for threshold; use §14.1 formula to compare actual vs threshold.
```

### 20.4 Period-over-Period (PoP) Comparisons

The API doesn't compute PoP automatically — call the same endpoint twice with different date ranges and compute in the frontend:

```
delta_absolute = current_period_value - prior_period_value
delta_percent  = ((current - prior) / prior) × 100

Common comparisons:
  Day over Day (DoD):   today vs. yesterday
  Week over Week (WoW): this week vs. last week
  Month over Month (MoM): this month vs. last month
  Year over Year (YoY): this period vs. same period last year
```

### 20.5 Amazon P&L Chain (Full Derivation)

```
gross_revenue         = SUM(effective_gross_revenue) for non-cancelled orders
                        = SUM(order_total)  ← Gross Sales card (total_gross_revenue)

gross_sales           = gross_revenue + effective_tax_withheld   ← settlement field only

amazon_fees (per order) =
  total_amazon_fees                    if total_amazon_fees > 0
  OR |effective_commission| + |effective_closing| + |effective_shipping|
     + |effective_other_service_fees| + |effective_tax_withheld|
  → SUM per order for total_amazon_fees

effective_net_payout  = gross_revenue + effective_refunds + all signed fee columns
                        (prefer stored effective_net_payout from CH)

product_cost          = SUM(cogs_product)

total_cogs            = SUM(amazon_fees) + SUM(cogs_product)

gross_profit          = effective_net_payout - cogs_product   (per order, then sum)

total_profit          = SUM(effective_net_payout) - SUM(cogs_product) - total_spend
```

**Do not use:** `ABS(SUM(total_amazon_fees))` across orders, or `total_cogs` on the PnL row as product cost.

### 20.6 Shopify AOV (Average Order Value)

```
AOV = net_revenue / (total_orders - cancelled_orders - fully_refunded_orders)

Where:
  total_orders          = COUNT all orders in the date range
  cancelled_orders      = COUNT WHERE cancelled_at IS NOT NULL
  fully_refunded_orders = COUNT WHERE financial_status = 'refunded'
  net_revenue           = total_revenue - refunded_amount   (all statuses, per §17)

This denominator = "effective orders" — placed and not cancelled or fully refunded.
```

### 20.7 Meta Derived Metrics

```
CPM      = (spend / impressions) × 1000
CTR      = (clicks / impressions) × 100
CPC      = spend / clicks
CPP      = spend / purchases            (Cost Per Purchase)
ROAS     = conversionValue / spend
Profit   = conversionValue - spend      (gross; does not deduct COGS)

Frequency = impressions / reach         (avg. times each person saw the ad)
```

### 20.8 Google Ads Derived Metrics

```
All amounts from GAQL are in MICROS — divide by 1,000,000:
  cost         = cost_micros / 1,000,000
  average_cpc  = average_cpc / 1,000,000
  conversion_value = metrics.conversions_value (already in currency units)

Derived:
  ROAS         = total_conversion_value / total_cost
  CPM          = (total_cost / impressions) × 1000
  CTR          = (clicks / impressions) × 100
  CPA          = total_cost / total_conversions   (Cost Per Acquisition)
```

---

## Appendix: File Path Index (for AI Agents)


| File                                                                  | Purpose                                                               |
| --------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `src/routes/shopifyAnalyticsRoutes.js`                                | Shopify analytics route definitions + `getRequestLocale()`            |
| `src/routes/metaAnalyticsRoutes.js`                                   | Meta real-time analytics routes                                       |
| `src/routes/googleAdsAnalyticsRoutes.js`                              | Google Ads real-time analytics routes                                 |
| `src/routes/ga4AnalyticsRoutes.js`                                    | GA4 analytics routes + token refresh                                  |
| `src/routes/amazonOrganicOrdersRoutes.js`                             | Amazon SP organic orders routes                                       |
| `src/routes/amazonAttributionRoutes.js`                               | Amazon Ads attribution routes                                         |
| `src/routes/metaAttributionRoutes.js`                                 | Meta attribution (ClickHouse) routes                                  |
| `src/routes/googleAttributionRoutes.js`                               | Google attribution (ClickHouse) routes                                |
| `src/routes/historicalAnalyticsRoutes.js`                             | Historical gold-layer dashboard route                                 |
| `src/routes/organicAttributionRoutes.js`                              | Channel/organic attribution routes                                    |
| `src/routes/pnlRoutes.js`                                             | P&L aggregated endpoints                                              |
| `src/routes/index.js`                                                 | Master route mounting with base paths                                 |
| `src/integrations/shared/shopifyCredentials.js`                       | Shopify credential DB query + cache                                   |
| `src/integrations/shared/platformCredentials.js`                      | Meta + Google Ads credential DB query + token refresh                 |
| `src/integrations/amazonShared/amazonOrderPnl.js`                     | P&L formula chain + `computeAmazonNetProfit()`                        |
| `src/integrations/amazonAttribution/buildAmazonAttributionPayload.js` | `deriveMetrics()` + two ROAS views                                    |
| `src/integrations/amazonOrganicOrders/analyticsClickhouse.js`         | ClickHouse SQL for organic orders                                     |
| `src/integrations/amazonAttribution/analyticsClickhouse.js`           | ClickHouse SQL for ads attribution                                    |
| `src/services/pnlService.js`                                          | `getPnlMetrics()`, `buildMetricsSection()`, `buildMarketingSection()` |
| `src/utils/tokenRefresh.js`                                           | `refreshTokenIfNeeded()`, `refreshGoogleToken()`                      |
| `src/utils/apiOptimization.js`                                        | `getCacheTTL(endDate, platform)`                                      |
| `src/middleware/responseCache.js`                                     | In-memory response cache middleware                                   |
| `src/utils/requestDeduplication.js`                                   | In-flight request deduplication                                       |


---

**Last updated:** 2026-06-25