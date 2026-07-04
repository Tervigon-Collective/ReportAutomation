WITH
    orders_dedup_inline AS (
      SELECT
        brand_id, order_id,
        argMax(order_date, _loaded_at) AS order_date,
        argMax(order_created_at, _loaded_at) AS order_created_at,
        argMax(order_status, _loaded_at) AS order_status,
        argMax(is_test, _loaded_at) AS is_test,
        argMax(is_revenue_adjustment, _loaded_at) AS is_revenue_adjustment,
        toFloat64(argMax(net_revenue, _loaded_at)) AS net_revenue,
        toFloat64(argMax(net_revenue_excl_tax, _loaded_at)) AS net_revenue_excl_tax,
        toFloat64(argMax(gross_revenue, _loaded_at)) AS gross_revenue,
        toFloat64(argMax(gross_revenue_excl_tax, _loaded_at)) AS gross_revenue_excl_tax,
        toFloat64(argMax(total_discounts, _loaded_at)) AS total_discounts,
        toFloat64(argMax(total_tax, _loaded_at)) AS total_tax
      FROM fct_orders
      WHERE brand_id = {brandId:Int64}
      GROUP BY brand_id, order_id
    ),
    spine AS (
      SELECT {brandId:Int64} AS brand_id,
        addDays(toDate({startDate:String}), number) AS report_date,
        toUInt8(0) AS hour_of_day
      FROM numbers(toUInt64(toDate({endDate:String}) - toDate({startDate:String}) + 1))
    ),
    placement_order_revenue AS (
      SELECT od.brand_id AS brand_id, od.order_date AS report_date, toUInt8(0) AS hour_of_day,
        count() AS total_orders,
        sum(toFloat64(coalesce(od.gross_revenue, 0)) + toFloat64(coalesce(od.total_discounts, 0))) AS total_sales,
        sum((if(toFloat64(coalesce(od.net_revenue, 0)) > 0, toFloat64(coalesce(od.gross_revenue, 0)) - (toFloat64(coalesce(od.gross_revenue, 0)) * (toFloat64(coalesce(od.net_revenue, 0)) - toFloat64(coalesce(od.net_revenue_excl_tax, 0))) / toFloat64(coalesce(od.net_revenue, 0))), toFloat64(coalesce(od.gross_revenue_excl_tax, 0)))) + (if(toFloat64(coalesce(od.net_revenue, 0)) > 0 AND toFloat64(coalesce(od.gross_revenue_excl_tax, 0)) > toFloat64(coalesce(od.net_revenue_excl_tax, 0)), toFloat64(coalesce(od.gross_revenue_excl_tax, 0)) - toFloat64(coalesce(od.net_revenue_excl_tax, 0)), if(toFloat64(coalesce(od.total_tax, 0)) > 0 AND toFloat64(coalesce(od.gross_revenue, 0)) > 0, toFloat64(coalesce(od.total_discounts, 0)) * ((toFloat64(coalesce(od.gross_revenue, 0)) - toFloat64(coalesce(od.total_tax, 0))) / toFloat64(coalesce(od.gross_revenue, 0))), toFloat64(coalesce(od.total_discounts, 0)))))) AS gross_sales,
        sum(if(toFloat64(coalesce(od.net_revenue, 0)) > 0 AND toFloat64(coalesce(od.gross_revenue_excl_tax, 0)) > toFloat64(coalesce(od.net_revenue_excl_tax, 0)), toFloat64(coalesce(od.gross_revenue_excl_tax, 0)) - toFloat64(coalesce(od.net_revenue_excl_tax, 0)), if(toFloat64(coalesce(od.total_tax, 0)) > 0 AND toFloat64(coalesce(od.gross_revenue, 0)) > 0, toFloat64(coalesce(od.total_discounts, 0)) * ((toFloat64(coalesce(od.gross_revenue, 0)) - toFloat64(coalesce(od.total_tax, 0))) / toFloat64(coalesce(od.gross_revenue, 0))), toFloat64(coalesce(od.total_discounts, 0))))) AS discounts
      FROM orders_dedup_inline AS od
      WHERE od.brand_id = {brandId:Int64} AND od.order_date >= toDate({startDate:String}) AND od.order_date <= toDate({endDate:String})
        AND coalesce(od.is_test, 0) = 0 AND lowerUTF8(trimBoth(coalesce(od.order_status, ''))) != 'voided' AND coalesce(od.is_revenue_adjustment, 0) = 0
      GROUP BY od.brand_id, od.order_date
    ),
    placement AS (
      SELECT oi.brand_id AS brand_id, toDate(toTimeZone(oi.created_at, 'Asia/Kolkata')) AS report_date, toUInt8(0) AS hour_of_day,
        countDistinctIf(oi.order_id, oi.pnl_refund_class = 'ACTIVE') AS line_item_orders,
        sumIf(toFloat64(oi.total_cost), oi.pnl_refund_class = 'ACTIVE') AS product_cost,
        sumIf(toFloat64(coalesce(oi.placed_shipping_cost, 0)), oi.pnl_refund_class = 'ACTIVE') AS shipping_cost,
        sumIf(toFloat64(coalesce(oi.placed_packaging_cost, 0)), oi.pnl_refund_class = 'ACTIVE') AS packaging_cost,
        sumIf(toFloat64(coalesce(oi.placed_gateway_fee, 0)), oi.pnl_refund_class = 'ACTIVE' AND coalesce(oi.is_cod, 0) = 0 AND coalesce(oi.is_online_payment, 0) = 1) AS payment_gateway_fees,
        sumIf(toFloat64(coalesce(oi.gross_cogs, 0)), oi.pnl_refund_class = 'ACTIVE') AS gross_cogs_all,
        toFloat64(0) AS rto_cost
      FROM fct_order_items AS oi
      WHERE oi.brand_id = {brandId:Int64} AND toDate(toTimeZone(oi.created_at, 'Asia/Kolkata')) >= toDate({startDate:String}) AND toDate(toTimeZone(oi.created_at, 'Asia/Kolkata')) <= toDate({endDate:String})
        AND coalesce(oi.is_placement_gross_eligible, 0) = 1 AND coalesce(oi.is_gift_card, 0) = 0
      GROUP BY oi.brand_id, toDate(toTimeZone(oi.created_at, 'Asia/Kolkata'))
    ),
    order_pnl_global AS (
      SELECT oi.brand_id AS brand_id, oi.order_id AS order_id,
        maxIf(toUInt8(1), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN') AS has_return,
        maxIf(toUInt8(1), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'CANCELLATION') AS has_cancel
      FROM fct_order_items AS oi
      WHERE oi.brand_id = {brandId:Int64} AND coalesce(oi.is_gift_card, 0) = 0
      GROUP BY oi.brand_id, oi.order_id
    ),
    cancelled AS (
      SELECT oi.brand_id AS brand_id,
        toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata')) AS report_date, toUInt8(0) AS hour_of_day,
        countDistinctIf(oi.order_id, g.has_return = 0 AND g.has_cancel = 1) AS cancelled_orders,
        sumIf(if(toFloat64(coalesce(oi.cancelled_revenue_excl_gst, 0)) > 0, toFloat64(oi.cancelled_revenue_excl_gst), toFloat64(coalesce(oi.net_pre_refund_excl_gst, 0)) + toFloat64(coalesce(oi.discount_excl_gst, 0))), g.has_return = 0 AND g.has_cancel = 1) AS cancelled_revenue_excl,
        sumIf(toFloat64(oi.cancelled_revenue_incl_gst), g.has_return = 0 AND g.has_cancel = 1) AS cancelled_revenue_incl
      FROM fct_order_items AS oi
      INNER JOIN order_pnl_global AS g ON g.brand_id = oi.brand_id AND g.order_id = oi.order_id
      WHERE oi.brand_id = {brandId:Int64} AND oi.is_cancelled_line = 1 AND coalesce(oi.is_gift_card, 0) = 0
        AND coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)) IS NOT NULL
        AND toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata')) >= toDate({startDate:String})
        AND toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata')) <= toDate({endDate:String})
      GROUP BY oi.brand_id, toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata'))
    ),
    returned AS (
      SELECT oi.brand_id AS brand_id,
        toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata')) AS report_date, toUInt8(0) AS hour_of_day,
        countDistinctIf(oi.order_id, g.has_return = 1) AS returned_orders,
        sumIf(if(toFloat64(coalesce(oi.returned_revenue_excl_gst, 0)) > 0, toFloat64(oi.returned_revenue_excl_gst), toFloat64(coalesce(oi.net_pre_refund_excl_gst, 0)) + toFloat64(coalesce(oi.discount_excl_gst, 0))), g.has_return = 1) AS returned_revenue_excl,
        sumIf(toFloat64(oi.returned_revenue_incl_gst), g.has_return = 1) AS returned_revenue_incl
      FROM fct_order_items AS oi
      INNER JOIN order_pnl_global AS g ON g.brand_id = oi.brand_id AND g.order_id = oi.order_id
      WHERE oi.brand_id = {brandId:Int64} AND coalesce(oi.is_gift_card, 0) = 0 AND g.has_return = 1
        AND coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)) IS NOT NULL
        AND toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata')) >= toDate({startDate:String})
        AND toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata')) <= toDate({endDate:String})
      GROUP BY oi.brand_id, toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata'))
    ),
    return_cogs AS (
      SELECT oi.brand_id AS brand_id,
        toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata')) AS report_date, toUInt8(0) AS hour_of_day,
        sumIf(toFloat64(coalesce(oi.rto_cost, 0)), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN' AND coalesce(oi.is_gift_card, 0) = 0 AND (coalesce(oi.is_return_line, 0) = 1 OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS'))) AS return_rto_cost,
        sumIf(toFloat64(coalesce(oi.placed_shipping_cost, 0)), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN' AND coalesce(oi.is_gift_card, 0) = 0 AND (coalesce(oi.is_return_line, 0) = 1 OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS'))) AS return_shipping_cost,
        sumIf(toFloat64(coalesce(oi.placed_packaging_cost, 0)), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN' AND coalesce(oi.is_gift_card, 0) = 0 AND (coalesce(oi.is_return_line, 0) = 1 OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS'))) AS return_packaging_cost,
        sumIf(toFloat64(coalesce(oi.placed_gateway_fee, 0)), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN' AND coalesce(oi.is_gift_card, 0) = 0 AND (coalesce(oi.is_return_line, 0) = 1 OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS')) AND coalesce(oi.is_cod, 0) = 0 AND coalesce(oi.is_online_payment, 0) = 1) AS return_gateway_fees,
        sum(toFloat64(coalesce(oi.gross_cogs, 0))) AS return_gross_cogs
      FROM fct_order_items AS oi
      WHERE oi.brand_id = {brandId:Int64} AND upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'RETURN' AND coalesce(oi.is_gift_card, 0) = 0 AND (coalesce(oi.is_return_line, 0) = 1 OR upperUTF8(trimBoth(coalesce(oi.return_status, ''))) IN ('RETURNED', 'IN_PROGRESS'))
        AND coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)) IS NOT NULL
        AND toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata')) >= toDate({startDate:String})
        AND toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata')) <= toDate({endDate:String})
      GROUP BY oi.brand_id, toDate(toTimeZone(coalesce(oi.returned_at, if(oi.order_status IN ('refunded', 'partially_refunded') AND oi.return_status = 'NO_RETURN' AND oi.refunded_quantity > 0, oi.refunded_at, NULL)), 'Asia/Kolkata'))
    ),
    cancel_cogs AS (
      SELECT oi.brand_id AS brand_id,
        toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata')) AS report_date, toUInt8(0) AS hour_of_day,
        sumIf(toFloat64(coalesce(oi.placed_gateway_fee, 0)), upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'CANCELLATION' AND coalesce(oi.is_gift_card, 0) = 0 AND coalesce(oi.is_cancelled_line, 0) = 1 AND coalesce(oi.is_cod, 0) = 0 AND coalesce(oi.is_online_payment, 0) = 1) AS cancel_gateway_fees,
        sum(toFloat64(coalesce(oi.gross_cogs, 0))) AS cancel_gross_cogs
      FROM fct_order_items AS oi
      WHERE oi.brand_id = {brandId:Int64} AND upperUTF8(trimBoth(coalesce(oi.pnl_refund_class, ''))) = 'CANCELLATION' AND coalesce(oi.is_gift_card, 0) = 0 AND coalesce(oi.is_cancelled_line, 0) = 1
        AND coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)) IS NOT NULL
        AND toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata')) >= toDate({startDate:String})
        AND toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata')) <= toDate({endDate:String})
      GROUP BY oi.brand_id, toDate(toTimeZone(coalesce(oi.cancelled_at, if(oi.order_status = 'voided', oi.voided_at, NULL)), 'Asia/Kolkata'))
    ),
    meta_ad_spend AS (
      SELECT m.brand_id AS brand_id, toDate(m.report_date) AS report_date, toUInt8(0) AS hour_of_day, sum(toFloat64(m.spend)) AS meta_spend
      FROM fct_meta_ads_daily AS m
      WHERE m.brand_id = {brandId:Int64} AND m.report_date >= toDate({startDate:String}) AND m.report_date <= toDate({endDate:String})
      GROUP BY m.brand_id, report_date
    ),
    google_ad_spend AS (
      SELECT g.brand_id AS brand_id, toDate(g.report_date) AS report_date, toUInt8(0) AS hour_of_day, sum(toFloat64(g.spend)) AS google_spend
      FROM fct_google_ads_daily AS g
      WHERE g.brand_id = {brandId:Int64} AND g.report_date >= toDate({startDate:String}) AND g.report_date <= toDate({endDate:String})
      GROUP BY g.brand_id, report_date
    ),
    amazon_ad_spend AS (
      -- Amazon Ads reports with a 1-day delay that is already baked into the source: the row
      -- dated D contains the previous day's actual spend. Use it as-is (no date arithmetic) to
      -- match the dashboard, which shows the row dated D and labels it as the prior day's data.
      SELECT a.brand_id AS brand_id, toDate(a.report_date) AS report_date, toUInt8(0) AS hour_of_day, sum(toFloat64(a.cost)) AS amazon_spend
      FROM fct_amazon_ads_campaigns_daily AS a
      WHERE a.brand_id = {brandId:Int64} AND a.report_date >= toDate({startDate:String}) AND a.report_date <= toDate({endDate:String})
      GROUP BY a.brand_id, report_date
    ),
    amz_items AS (
      SELECT oi.brand_id, toDate(oi.purchase_date) AS report_date, oi.amazon_order_id, oi.order_item_id, oi.pnl_refund_status,
        toFloat64(oi.item_price_amount) * toFloat64(oi.quantity_ordered) AS item_gross, toFloat64(coalesce(oi.total_cogs, 0)) AS product_cost
      FROM fct_amazon_order_items AS oi
      WHERE oi.brand_id = {brandId:Int64} AND oi.purchase_date IS NOT NULL AND toDate(oi.purchase_date) >= toDate({startDate:String}) AND toDate(oi.purchase_date) <= toDate({endDate:String})
    ),
    amz_order_gross AS (
      SELECT brand_id, amazon_order_id, sum(item_gross) AS order_gross_total FROM amz_items GROUP BY brand_id, amazon_order_id
    ),
    amz_pnl AS (
      SELECT brand_id, amazon_order_id,
        argMax(payout_basis, _gold_created_at) AS payout_basis,
        toFloat64(argMax(effective_gross_revenue, _gold_created_at)) AS gross_revenue,
        toFloat64(argMax(effective_refunds, _gold_created_at)) AS refund_amount,
        toFloat64(argMax(effective_commission, _gold_created_at)) AS commission,
        toFloat64(argMax(effective_closing, _gold_created_at)) AS closing,
        toFloat64(argMax(effective_shipping, _gold_created_at)) AS shipping,
        toFloat64(argMax(effective_tax_withheld, _gold_created_at)) AS tax_withheld,
        toFloat64(argMax(effective_other_service_fees, _gold_created_at)) AS other_fees
      FROM fct_amazon_sp_order_pnl
      WHERE brand_id = {brandId:Int64} AND purchase_date IS NOT NULL AND toDate(purchase_date) >= toDate({startDate:String}) AND toDate(purchase_date) <= toDate({endDate:String})
      GROUP BY brand_id, amazon_order_id
    ),
    amz_item_pnl AS (
      SELECT ai.brand_id AS brand_id, ai.report_date AS report_date, ai.amazon_order_id AS amazon_order_id, ai.pnl_refund_status AS pnl_refund_status, ai.product_cost AS product_cost,
        coalesce(ap.payout_basis, 'NONE') AS payout_basis,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.gross_revenue, 0) * (ai.item_gross / og.order_gross_total), if(ap.amazon_order_id IS NULL, ai.item_gross, toFloat64(0)))) AS item_revenue,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.refund_amount, 0) * (ai.item_gross / og.order_gross_total), toFloat64(0))) AS item_refunds,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.commission, 0) * (ai.item_gross / og.order_gross_total), toFloat64(0))) AS item_commission,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.closing, 0) * (ai.item_gross / og.order_gross_total), toFloat64(0))) AS item_closing,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.shipping, 0) * (ai.item_gross / og.order_gross_total), toFloat64(0))) AS item_shipping,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.tax_withheld, 0) * (ai.item_gross / og.order_gross_total), toFloat64(0))) AS item_tax_withheld,
        if(coalesce(ap.payout_basis, 'NONE') = 'NONE' AND ai.pnl_refund_status = 'CANCELLATION', toFloat64(0), if(coalesce(og.order_gross_total, 0) > 0 AND ap.amazon_order_id IS NOT NULL, coalesce(ap.other_fees, 0) * (ai.item_gross / og.order_gross_total), toFloat64(0))) AS item_other_fees
      FROM amz_items AS ai
      LEFT JOIN amz_order_gross AS og ON og.brand_id = ai.brand_id AND og.amazon_order_id = ai.amazon_order_id
      LEFT JOIN amz_pnl AS ap ON ap.brand_id = ai.brand_id AND ap.amazon_order_id = ai.amazon_order_id
    ),
    amazon_daily AS (
      SELECT brand_id, report_date, toUInt8(0) AS hour_of_day,
        toUInt64(countDistinctIf(amazon_order_id, pnl_refund_status != 'CANCELLATION')) AS amazon_orders,
        sum(item_revenue + item_tax_withheld) AS amazon_gross_revenue,
        sum(item_revenue + item_refunds + item_tax_withheld) AS amazon_net_revenue,
        sum(product_cost) AS amazon_product_cost,
        sum(abs(item_commission)) AS amazon_commission,
        sum(abs(item_closing)) AS amazon_closing,
        sum(abs(item_shipping)) AS amazon_shipping,
        sum(abs(item_tax_withheld)) AS amazon_tax_withheld,
        sum(abs(item_other_fees)) AS amazon_other_fees,
        sum(product_cost + abs(item_commission) + abs(item_closing) + abs(item_shipping) + abs(item_tax_withheld) + abs(item_other_fees)) AS amazon_gross_cogs,
        sum(CASE WHEN payout_basis = 'NONE' AND pnl_refund_status = 'CANCELLATION' THEN toFloat64(0) WHEN payout_basis = 'NONE' THEN product_cost WHEN pnl_refund_status = 'RETURN' THEN item_revenue + item_refunds + item_commission + item_closing + item_shipping + item_tax_withheld + item_other_fees ELSE product_cost + abs(item_commission) + abs(item_closing) + abs(item_shipping) + abs(item_tax_withheld) + abs(item_other_fees) END) AS amazon_net_cogs
      FROM amz_item_pnl GROUP BY brand_id, report_date
    ),
    rebuilt AS (
      SELECT s.brand_id AS brand_id, s.report_date AS report_date, s.hour_of_day AS hour_of_day,
        coalesce(por.total_sales, 0) + coalesce(amzd.amazon_gross_revenue, 0) AS total_sales,
        coalesce(por.gross_sales, 0) + coalesce(amzd.amazon_gross_revenue, 0) AS gross_sales,
        coalesce(por.discounts, 0) AS discounts,
        coalesce(ca.cancelled_orders, 0) AS cancelled_orders,
        coalesce(re.returned_orders, 0) AS returned_orders,
        coalesce(ca.cancelled_orders, 0) + coalesce(re.returned_orders, 0) AS returns_cancels,
        coalesce(ca.cancelled_revenue_excl, 0) AS cancelled_revenue_excl,
        coalesce(re.returned_revenue_excl, 0) AS returned_revenue_excl,
        (coalesce(por.gross_sales, 0) - coalesce(re.returned_revenue_excl, 0) - coalesce(ca.cancelled_revenue_excl, 0) - coalesce(por.discounts, 0)) + coalesce(amzd.amazon_net_revenue, 0) AS net_sales,
        coalesce(por.total_orders, 0) + coalesce(amzd.amazon_orders, 0) AS total_orders,
        coalesce(pl.product_cost, 0) + coalesce(pl.shipping_cost, 0) + coalesce(pl.packaging_cost, 0) + coalesce(pl.payment_gateway_fees, 0) + coalesce(cc.cancel_gateway_fees, 0) + coalesce(rc.return_rto_cost, 0) + coalesce(rc.return_shipping_cost, 0) + coalesce(rc.return_packaging_cost, 0) + coalesce(rc.return_gateway_fees, 0) + coalesce(amzd.amazon_net_cogs, 0) AS total_cogs,
        coalesce(ma.meta_spend, 0) AS meta_spend,
        coalesce(ga.google_spend, 0) AS google_spend,
        coalesce(az.amazon_spend, 0) AS amazon_spend,
        coalesce(ma.meta_spend, 0) + coalesce(ga.google_spend, 0) + coalesce(az.amazon_spend, 0) AS total_ad_spend,
        coalesce(amzd.amazon_orders, 0) AS amazon_orders,
        coalesce(amzd.amazon_gross_revenue, 0) AS amazon_gross_revenue,
        coalesce(amzd.amazon_net_revenue, 0) AS amazon_net_revenue,
        coalesce(amzd.amazon_net_cogs, 0) AS amazon_net_cogs
      FROM spine AS s
      LEFT JOIN placement_order_revenue AS por ON por.brand_id = s.brand_id AND por.report_date = s.report_date
      LEFT JOIN placement AS pl ON pl.brand_id = s.brand_id AND pl.report_date = s.report_date
      LEFT JOIN cancelled AS ca ON ca.brand_id = s.brand_id AND ca.report_date = s.report_date
      LEFT JOIN returned AS re ON re.brand_id = s.brand_id AND re.report_date = s.report_date
      LEFT JOIN return_cogs AS rc ON rc.brand_id = s.brand_id AND rc.report_date = s.report_date
      LEFT JOIN cancel_cogs AS cc ON cc.brand_id = s.brand_id AND cc.report_date = s.report_date
      LEFT JOIN meta_ad_spend AS ma ON ma.brand_id = s.brand_id AND ma.report_date = s.report_date
      LEFT JOIN google_ad_spend AS ga ON ga.brand_id = s.brand_id AND ga.report_date = s.report_date
      LEFT JOIN amazon_ad_spend AS az ON az.brand_id = s.brand_id AND az.report_date = s.report_date
      LEFT JOIN amazon_daily AS amzd ON amzd.brand_id = s.brand_id AND amzd.report_date = s.report_date
    )
    SELECT
      toString(report_date) AS report_date,
      total_sales, gross_sales, discounts, cancelled_orders, returned_orders, returns_cancels,
      cancelled_revenue_excl, returned_revenue_excl,
      net_sales, total_cogs, meta_spend, google_spend, amazon_spend, total_ad_spend,
      total_orders, amazon_orders, amazon_gross_revenue, amazon_net_revenue, amazon_net_cogs,
      round(net_sales - total_cogs - total_ad_spend, 2) AS net_profit
    FROM rebuilt
    ORDER BY report_date
