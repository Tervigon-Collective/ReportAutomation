# ClickHouse Reporting & Reconciliation Queries

Canonical, copy‑pasteable ClickHouse queries for dashboard reporting, derived from the
backend metric definitions:

- `src/integrations/historicalAnalytics/historicalQueryHelpers.js`
- `src/integrations/attributionQueryHelpers.js`
- `src/integrations/attributionPnLHelpers.js`
- `src/integrations/historicalAnalytics/amazonHistoricalHelpers.js`
- `src/services/pnlService.js`

**Params (all queries):** `{brandId:Int64}`, `{startDate:String}` (YYYY‑MM‑DD), `{endDate:String}` (inclusive).

**Tables used:** `fct_orders`, `fct_order_items`, `fct_order_attribution`,
`fct_meta_ads_daily`, `fct_google_ads_daily`, `fct_amazon_ads_campaigns_daily`,
`fct_amazon_order_items`, `fct_amazon_sp_order_pnl`, `gold.fct_daily_pnl`.

---

## 0. Shared definitions (rules every query follows)

**Net Attributed Sales** (per order, on deduped `fct_orders`):
- `cancelled` → 0
- revenue‑adjustment / RTO (`is_revenue_adjustment=1`) → 0
- paid (`net_revenue > 0`) → `net_revenue_excl_tax`
- unpaid/pending → `gross_revenue_excl_tax − discount_excl_tax`
- exclude `is_test=1` and `voided`

**Net COGS:** pre‑computed `net_cogs` column on `fct_order_items`
(ACTIVE = product+ship+pkg+gateway; RETURN = rto+ship+pkg+gateway; CANCEL = gateway only), `is_gift_card=0`.

**Channel bucket:** `lt_platform` → meta / google / organic / other.

**Ad spend:** `fct_meta_ads_daily.spend` + `fct_google_ads_daily.spend` + `fct_amazon_ads_campaigns_daily.cost`.

> Dedup note: `fct_orders` may carry multiple loads per `order_id`; always reduce with
> `argMax(col, _loaded_at)` (or `FINAL`). All queries below do this.

---

## 1. Headline KPIs — Net Attributed Sales, Order Count, Ad Spend, Net COGS

```sql
WITH
orders_dedup AS (
    SELECT
        brand_id,
        order_id,
        argMax(order_date, _loaded_at)               AS order_date,
        argMax(order_status, _loaded_at)             AS order_status,
        argMax(is_test, _loaded_at)                  AS is_test,
        argMax(is_revenue_adjustment, _loaded_at)    AS is_revenue_adjustment,
        toFloat64(argMax(net_revenue, _loaded_at))            AS net_revenue,
        toFloat64(argMax(net_revenue_excl_tax, _loaded_at))   AS net_revenue_excl_tax,
        toFloat64(argMax(gross_revenue, _loaded_at))          AS gross_revenue,
        toFloat64(argMax(gross_revenue_excl_tax, _loaded_at)) AS gross_revenue_excl_tax,
        toFloat64(argMax(total_discounts, _loaded_at))        AS total_discounts,
        toFloat64(argMax(total_tax, _loaded_at))              AS total_tax
    FROM fct_orders
    WHERE brand_id = {brandId:Int64}
    GROUP BY brand_id, order_id
),
orders_in_range AS (
    SELECT *,
        if(net_revenue > 0 AND gross_revenue_excl_tax > net_revenue_excl_tax,
           gross_revenue_excl_tax - net_revenue_excl_tax,
           if(total_tax > 0 AND gross_revenue > 0,
              total_discounts * ((gross_revenue - total_tax) / gross_revenue),
              total_discounts)) AS discount_excl_tax
    FROM orders_dedup
    WHERE order_date >= toDate({startDate:String})
      AND order_date <= toDate({endDate:String})
      AND coalesce(is_test, 0) = 0
      AND lowerUTF8(trimBoth(coalesce(order_status, ''))) != 'voided'
)
SELECT
    round(sum(
        if(lowerUTF8(trimBoth(coalesce(order_status,''))) = 'cancelled', 0,
        if(is_revenue_adjustment = 1, 0,
        if(net_revenue > 0, net_revenue_excl_tax,
           greatest(0, gross_revenue_excl_tax - discount_excl_tax))))
    ), 2)                                                       AS net_sales,

    toInt64(count())                                            AS order_count,

    round((
        SELECT sum(toFloat64(coalesce(i.net_cogs, 0)))
        FROM fct_order_items AS i
        WHERE i.brand_id = {brandId:Int64}
          AND i.order_date >= toDate({startDate:String})
          AND i.order_date <= toDate({endDate:String})
          AND coalesce(i.is_gift_card, 0) = 0
    ), 2)                                                       AS net_cogs,

    round((
        (SELECT sum(toFloat64(coalesce(spend,0))) FROM fct_meta_ads_daily
          WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String}))
      + (SELECT sum(toFloat64(coalesce(spend,0))) FROM fct_google_ads_daily
          WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String}))
      + (SELECT sum(toFloat64(coalesce(cost,0)))  FROM fct_amazon_ads_campaigns_daily
          WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String}))
    ), 2)                                                       AS ad_spend
FROM orders_in_range;
```

---

## 2. Campaign‑level attribution (Meta example)

Joins `fct_order_attribution` → deduped `fct_orders` for net sales, plus `fct_meta_ads_daily` for spend, grouped by campaign.

```sql
WITH
meta_attr AS (
    SELECT
        brand_id, order_id,
        argMax(lt_campaign_id, _loaded_at)   AS campaign_id,
        argMax(lt_campaign_name, _loaded_at) AS campaign_name,
        argMax(order_status, _loaded_at)     AS order_status,
        argMax(is_test, _loaded_at)          AS is_test
    FROM fct_order_attribution
    WHERE brand_id = {brandId:Int64}
      AND order_date >= toDate({startDate:String})
      AND order_date <= toDate({endDate:String})
      AND lowerUTF8(trimBoth(coalesce(lt_platform,''))) IN ('meta','facebook','instagram','fb','ig')
      AND coalesce(is_test,0)=0
      AND lowerUTF8(trimBoth(coalesce(order_status,''))) != 'voided'
    GROUP BY brand_id, order_id
),
orders_dedup AS (
    SELECT brand_id, order_id,
        argMax(order_status, _loaded_at)             AS order_status,
        argMax(is_revenue_adjustment, _loaded_at)    AS is_revenue_adjustment,
        toFloat64(argMax(net_revenue, _loaded_at))            AS net_revenue,
        toFloat64(argMax(net_revenue_excl_tax, _loaded_at))   AS net_revenue_excl_tax,
        toFloat64(argMax(gross_revenue, _loaded_at))          AS gross_revenue,
        toFloat64(argMax(gross_revenue_excl_tax, _loaded_at)) AS gross_revenue_excl_tax,
        toFloat64(argMax(total_discounts, _loaded_at))        AS total_discounts,
        toFloat64(argMax(total_tax, _loaded_at))              AS total_tax
    FROM fct_orders WHERE brand_id = {brandId:Int64}
    GROUP BY brand_id, order_id
),
attributed AS (
    SELECT
        a.campaign_id   AS campaign_id,
        any(a.campaign_name) AS campaign_name,
        toInt64(count())     AS attributed_orders,
        round(sum(
            if(lowerUTF8(trimBoth(coalesce(a.order_status,'')))='cancelled',0,
            if(o.is_revenue_adjustment=1,0,
            if(o.net_revenue>0, o.net_revenue_excl_tax,
               greatest(0, o.gross_revenue_excl_tax -
                 if(o.net_revenue>0 AND o.gross_revenue_excl_tax>o.net_revenue_excl_tax,
                    o.gross_revenue_excl_tax-o.net_revenue_excl_tax,
                    if(o.total_tax>0 AND o.gross_revenue>0,
                       o.total_discounts*((o.gross_revenue-o.total_tax)/o.gross_revenue),
                       o.total_discounts))))))
        ),2)                 AS net_attributed_sales
    FROM meta_attr AS a
    INNER JOIN orders_dedup AS o ON o.brand_id=a.brand_id AND o.order_id=a.order_id
    GROUP BY a.campaign_id
),
spend AS (
    SELECT toString(campaign_id) AS campaign_id,
           max(campaign_name)    AS campaign_name,
           sum(toFloat64(coalesce(spend,0)))       AS spend,
           sum(toFloat64(coalesce(impressions,0))) AS impressions,
           sum(toFloat64(coalesce(clicks,0)))      AS clicks
    FROM fct_meta_ads_daily
    WHERE brand_id={brandId:Int64}
      AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String})
    GROUP BY campaign_id
)
SELECT
    coalesce(toString(at.campaign_id), s.campaign_id)        AS campaign_id,
    coalesce(at.campaign_name, s.campaign_name)              AS campaign_name,
    coalesce(s.spend, 0)                                     AS ad_spend,
    coalesce(s.impressions, 0)                               AS impressions,
    coalesce(s.clicks, 0)                                    AS clicks,
    coalesce(at.attributed_orders, 0)                        AS orders,
    coalesce(at.net_attributed_sales, 0)                     AS net_attributed_sales,
    if(s.spend>0, round(at.net_attributed_sales/s.spend,2),0) AS roas
FROM attributed AS at
FULL OUTER JOIN spend AS s ON toString(at.campaign_id)=s.campaign_id
ORDER BY ad_spend DESC;
```

- **Google:** swap platform filter to `lt_platform = 'google'`, spend source to `fct_google_ads_daily`.
- **Amazon:** campaign spend from `fct_amazon_ads_campaigns_daily` (group by `campaign_type`/`campaign_id`, cost column = `cost`). See §4.

---

## 3. Reconciliation — channel totals must equal Shopify order totals

LEFT JOIN `fct_orders` → attribution keeps every order; unattributed → `other`.
Per‑channel sums equal the all‑orders total.

```sql
WITH
order_channel AS (
    SELECT brand_id, order_id,
        any(multiIf(
            lowerUTF8(trimBoth(coalesce(lt_platform,''))) IN ('meta','facebook','instagram','fb','ig'),'meta',
            lowerUTF8(trimBoth(coalesce(lt_platform,'')))='google','google',
            lowerUTF8(trimBoth(coalesce(lt_platform,'')))='organic','organic',
            'other')) AS channel
    FROM fct_order_attribution
    WHERE brand_id={brandId:Int64}
      AND order_date>=toDate({startDate:String}) AND order_date<=toDate({endDate:String})
      AND coalesce(is_test,0)=0
      AND lowerUTF8(trimBoth(coalesce(order_status,''))) != 'voided'
    GROUP BY brand_id, order_id
),
orders_dedup AS (
    SELECT brand_id, order_id,
        argMax(order_date,_loaded_at) AS order_date,
        argMax(order_status,_loaded_at) AS order_status,
        argMax(is_test,_loaded_at) AS is_test,
        argMax(is_revenue_adjustment,_loaded_at) AS is_revenue_adjustment,
        toFloat64(argMax(net_revenue,_loaded_at)) AS net_revenue,
        toFloat64(argMax(net_revenue_excl_tax,_loaded_at)) AS net_revenue_excl_tax,
        toFloat64(argMax(gross_revenue,_loaded_at)) AS gross_revenue,
        toFloat64(argMax(gross_revenue_excl_tax,_loaded_at)) AS gross_revenue_excl_tax,
        toFloat64(argMax(total_discounts,_loaded_at)) AS total_discounts,
        toFloat64(argMax(total_tax,_loaded_at)) AS total_tax
    FROM fct_orders WHERE brand_id={brandId:Int64}
    GROUP BY brand_id, order_id
),
base AS (
    SELECT
        coalesce(oc.channel,'other') AS channel,
        o.order_id,
        if(lowerUTF8(trimBoth(coalesce(o.order_status,'')))='cancelled',0,
        if(o.is_revenue_adjustment=1,0,
        if(o.net_revenue>0,o.net_revenue_excl_tax,
           greatest(0,o.gross_revenue_excl_tax -
             if(o.net_revenue>0 AND o.gross_revenue_excl_tax>o.net_revenue_excl_tax,
                o.gross_revenue_excl_tax-o.net_revenue_excl_tax,
                if(o.total_tax>0 AND o.gross_revenue>0,
                   o.total_discounts*((o.gross_revenue-o.total_tax)/o.gross_revenue),
                   o.total_discounts)))))) AS net_sales
    FROM orders_dedup AS o
    LEFT JOIN order_channel AS oc ON oc.brand_id=o.brand_id AND oc.order_id=o.order_id
    WHERE o.order_date>=toDate({startDate:String}) AND o.order_date<=toDate({endDate:String})
      AND coalesce(o.is_test,0)=0
      AND lowerUTF8(trimBoth(coalesce(o.order_status,''))) != 'voided'
)
SELECT channel,
       toInt64(count())            AS order_count,
       round(sum(net_sales),2)     AS net_sales
FROM base
GROUP BY channel
WITH ROLLUP          -- channel='' row = Shopify grand total
ORDER BY channel;
```

The `channel = ''` ROLLUP row is the Shopify total; per‑channel rows sum to it by construction.
It also equals the headline query (§1) `order_count` / `net_sales` over the same window.

---

## 4. Amazon — full calculation breakdown

Item‑level allocation of order‑level Amazon P&L (matches `buildAmazonItemPnlCtes()`).

```sql
WITH
item_base AS (
    SELECT oi.brand_id, toDate(oi.purchase_date) AS purchase_date,
           oi.amazon_order_id, oi.order_item_id, oi.order_status,
           round(toFloat64(oi.item_price_amount)*toFloat64(oi.quantity_ordered),2) AS item_gross,
           round(toFloat64(coalesce(oi.item_tax_amount,0))*toFloat64(oi.quantity_ordered),2) AS item_tax,
           round(toFloat64(oi.total_cogs),2) AS product_cost
    FROM fct_amazon_order_items AS oi
    WHERE oi.brand_id={brandId:Int64}
      AND toDate(oi.purchase_date)>=toDate({startDate:String})
      AND toDate(oi.purchase_date)<=toDate({endDate:String})
      AND lowerUTF8(trimBoth(coalesce(oi.order_status,''))) NOT IN ('cancelled','canceled')
),
order_share AS (
    SELECT i.*, sum(i.item_gross) OVER (PARTITION BY i.brand_id,i.amazon_order_id) AS order_item_gross_total
    FROM item_base AS i
),
pnl_dedup AS (
    SELECT brand_id, amazon_order_id,
        toFloat64(argMax(effective_gross_revenue,_gold_created_at)) AS effective_gross_revenue,
        toFloat64(argMax(effective_refunds,_gold_created_at))       AS effective_refunds,
        toFloat64(argMax(effective_commission,_gold_created_at))    AS effective_commission,
        toFloat64(argMax(effective_closing,_gold_created_at))       AS effective_closing,
        toFloat64(argMax(effective_shipping,_gold_created_at))      AS effective_shipping,
        toFloat64(argMax(effective_tax_withheld,_gold_created_at))  AS effective_tax_withheld,
        toFloat64(argMax(effective_other_service_fees,_gold_created_at)) AS effective_other_service_fees
    FROM fct_amazon_sp_order_pnl
    WHERE brand_id={brandId:Int64}
      AND toDate(purchase_date)>=toDate({startDate:String})
      AND toDate(purchase_date)<=toDate({endDate:String})
    GROUP BY brand_id, amazon_order_id
),
item_pnl AS (
    SELECT i.amazon_order_id, i.item_tax, i.product_cost,
        if(i.order_item_gross_total=0,0,i.item_gross/i.order_item_gross_total) AS share,
        if(op.amazon_order_id IS NULL, i.item_gross,
           coalesce(op.effective_gross_revenue,i.item_gross)) AS og,
        op.effective_refunds AS r, op.effective_commission AS c, op.effective_closing AS cl,
        op.effective_shipping AS sh, op.effective_tax_withheld AS tw, op.effective_other_service_fees AS osf,
        op.amazon_order_id AS matched
    FROM order_share AS i
    LEFT JOIN pnl_dedup AS op ON op.brand_id=i.brand_id AND op.amazon_order_id=i.amazon_order_id
),
item_metrics AS (
    SELECT amazon_order_id, item_tax, product_cost,
        round(if(matched IS NULL, og, og*share),2)                            AS gross,
        round(if(matched IS NULL,0,coalesce(r,0)*share),2)                    AS refunds,
        round(if(matched IS NULL,0,coalesce(c,0)*share),2)                    AS commission,
        round(if(matched IS NULL,0,coalesce(cl,0)*share),2)                   AS closing,
        round(if(matched IS NULL,0,coalesce(sh,0)*share),2)                   AS shipping,
        round(if(matched IS NULL,0,coalesce(tw,0)*share),2)                   AS tax_withheld,
        round(if(matched IS NULL,0,coalesce(osf,0)*share),2)                  AS other_fees
    FROM item_pnl
)
SELECT
    toInt64(uniqExact(amazon_order_id))                          AS order_count,
    round(sum(gross + tax_withheld),2)                           AS gross_sales,
    round(sum(gross + refunds + tax_withheld),2)                 AS net_sales,
    round(sum(item_tax),2)                                       AS gst,
    round(sum(product_cost),2)                                   AS product_cost,
    round(sum(abs(commission)+abs(closing)+abs(shipping)+abs(other_fees)+abs(tax_withheld)),2) AS amazon_fees,
    round(sum(abs(commission)+abs(closing)+abs(shipping)+abs(other_fees)+abs(tax_withheld)+product_cost),2) AS total_cogs,
    round(sum(gross+refunds+commission+closing+shipping+tax_withheld+other_fees),2) AS net_payout,
    round(sum(gross+refunds+commission+closing+shipping+tax_withheld+other_fees-product_cost),2) AS gross_profit,
    round((SELECT sum(toFloat64(coalesce(cost,0))) FROM fct_amazon_ads_campaigns_daily
           WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String})),2) AS ad_spend,
    round(sum(gross+refunds+commission+closing+shipping+tax_withheld+other_fees-product_cost)
          - (SELECT sum(toFloat64(coalesce(cost,0))) FROM fct_amazon_ads_campaigns_daily
             WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String})),2) AS net_profit
FROM item_metrics;
```

Amazon ad spend split by campaign type:

```sql
SELECT lowerUTF8(trimBoth(coalesce(campaign_type,''))) AS campaign_type,
       round(sum(toFloat64(coalesce(cost,0))),2)        AS spend
FROM fct_amazon_ads_campaigns_daily
WHERE brand_id={brandId:Int64}
  AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String})
GROUP BY campaign_type ORDER BY spend DESC;
```

---

## 5. Net Profit + reconciliation

**Profit = Net Sales (excl GST) − Net COGS − Ad Spend.**

### 5a. From order facts

```sql
WITH
orders_dedup AS (
    SELECT brand_id, order_id,
        argMax(order_date,_loaded_at) AS order_date,
        argMax(order_status,_loaded_at) AS order_status,
        argMax(is_test,_loaded_at) AS is_test,
        argMax(is_revenue_adjustment,_loaded_at) AS is_revenue_adjustment,
        toFloat64(argMax(net_revenue,_loaded_at)) AS net_revenue,
        toFloat64(argMax(net_revenue_excl_tax,_loaded_at)) AS net_revenue_excl_tax,
        toFloat64(argMax(gross_revenue,_loaded_at)) AS gross_revenue,
        toFloat64(argMax(gross_revenue_excl_tax,_loaded_at)) AS gross_revenue_excl_tax,
        toFloat64(argMax(total_discounts,_loaded_at)) AS total_discounts,
        toFloat64(argMax(total_tax,_loaded_at)) AS total_tax
    FROM fct_orders WHERE brand_id={brandId:Int64}
    GROUP BY brand_id, order_id
),
sales AS (
    SELECT round(sum(
        if(lowerUTF8(trimBoth(coalesce(order_status,'')))='cancelled',0,
        if(is_revenue_adjustment=1,0,
        if(net_revenue>0, net_revenue_excl_tax,
           greatest(0, gross_revenue_excl_tax -
             if(net_revenue>0 AND gross_revenue_excl_tax>net_revenue_excl_tax,
                gross_revenue_excl_tax-net_revenue_excl_tax,
                if(total_tax>0 AND gross_revenue>0,
                   total_discounts*((gross_revenue-total_tax)/gross_revenue),
                   total_discounts))))))),2) AS net_sales
    FROM orders_dedup
    WHERE order_date>=toDate({startDate:String}) AND order_date<=toDate({endDate:String})
      AND coalesce(is_test,0)=0
      AND lowerUTF8(trimBoth(coalesce(order_status,''))) != 'voided'
),
cogs AS (
    SELECT round(sum(toFloat64(coalesce(net_cogs,0))),2) AS net_cogs
    FROM fct_order_items
    WHERE brand_id={brandId:Int64}
      AND order_date>=toDate({startDate:String}) AND order_date<=toDate({endDate:String})
      AND coalesce(is_gift_card,0)=0
),
spend AS (
    SELECT round(
        (SELECT sum(toFloat64(coalesce(spend,0))) FROM fct_meta_ads_daily
          WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String}))
      + (SELECT sum(toFloat64(coalesce(spend,0))) FROM fct_google_ads_daily
          WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String}))
      + (SELECT sum(toFloat64(coalesce(cost,0)))  FROM fct_amazon_ads_campaigns_daily
          WHERE brand_id={brandId:Int64} AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String})),2) AS ad_spend
)
SELECT
    sales.net_sales,
    cogs.net_cogs,
    spend.ad_spend,
    round(sales.net_sales - cogs.net_cogs - spend.ad_spend, 2) AS net_profit
FROM sales, cogs, spend;
```

### 5b. From the pre‑aggregated gold mart (current schema cross‑check)

```sql
SELECT
    round(sum(toFloat64(ifNull(net_sales_excl_tax,0))),2)    AS net_sales_excl_tax,
    round(sum(toFloat64(ifNull(net_cogs_total,0))),2)        AS net_cogs,
    round(sum(toFloat64(ifNull(total_ad_spend,0))),2)        AS ad_spend,
    round(sum(toFloat64(ifNull(contribution_margin,0))),2)   AS contribution_margin,
    round(sum(toFloat64(ifNull(net_profit,0))),2)            AS net_profit,
    round(
        sum(toFloat64(ifNull(net_sales_excl_tax,0)))
      - sum(toFloat64(ifNull(net_cogs_total,0)))
      - sum(toFloat64(ifNull(total_ad_spend,0)))
    ,2)                                                      AS recomputed_contribution_margin
FROM gold.fct_daily_pnl FINAL
WHERE brand_id={brandId:Int64}
  AND report_date>=toDate({startDate:String}) AND report_date<=toDate({endDate:String});
```

Use the mart-native totals above rather than rebuilding COGS from partial cost columns or
recomputing ad spend from only Meta/Google. `total_ad_spend` already includes Amazon spend,
and `net_cogs_total` is the current all-in net COGS field for the mart.

---

## Reconciliation checklist

| Check | Query A | Query B | Expected |
|---|---|---|---|
| Net sales total | §1 `net_sales` | §3 ROLLUP row | exact match |
| Order count total | §1 `order_count` | §3 ROLLUP row | exact match |
| Channel split sums to total | §3 per‑channel rows | §3 ROLLUP | by construction |
| Net profit / contribution | §5a | §5b (`gold.fct_daily_pnl`) | do not expect an exact match; compare as different lenses |
| Amazon profit | §4 | tracked independently | separate marketplace |

`§5a` is a fact-table, placed-order style calculation. `§5b` is a mart-native P&L view on
`gold.fct_daily_pnl`, where `report_date` can reflect refunds, returns, voids, ad spend, and
other adjustments recognized on that day. Treat `§5b` as a cross-check against the mart's own
native fields, not as a strict equality check against `§5a`.

### Two things to confirm on the cluster

1. **Database prefix** — `pnlService.js` uses `gold.fct_daily_pnl`, but attribution modules use
   unprefixed `fct_orders`. Confirm whether the default DB needs the `gold.` prefix on every table.
2. **Amazon scope** — Amazon (§4) is a separate marketplace from the Shopify channel split (§3);
   it is **not** part of the Shopify net‑sales reconciliation. Keep them in separate report sections.
