"""Meta / Google / Organic channel sheets sourced from ClickHouse gold.

Why this module exists
----------------------
The channel sheets used to take revenue and COGS from the marketing API's
``attributed_orders_revenue`` / ``attributed_orders_cogs``. Those disagree with
``gold.fct_ad_channel_pnl_daily``, which is what the dashboard reconciles
against, and the disagreement is large: for Sep 2026 the API understated Meta
revenue by 28% and Google by 22%, overstating the reported loss by 2.02L and
1.09L respectively.

``fct_ad_channel_pnl_daily`` is the trustworthy source on three counts:
  * its ``ad_spend`` ties to ``fct_meta_ads_daily`` / ``fct_google_ads_daily``
    to the cent (Sep 1-22: 616344.41 and 283406.16);
  * ``net_sales - net_cogs - ad_spend == net_profit`` holds exactly per row;
  * its order-to-channel split agrees with ``fct_order_items`` joined through
    ``fct_order_attribution`` (meta 408/407, google 226/226, organic 15/15).

SKU lines come from ``fct_order_items`` via ``fct_order_attribution``. Their own
``net_revenue`` sums to ~7.5% above the ad-level ``net_sales`` (order-level
shipping and adjustments are not line items), so rather than showing an
independent figure -- the very defect this replaces -- each ad's ``net_sales``
and ``net_cogs`` are **allocated** across its SKU lines by revenue share. The
parts then sum to the whole exactly.

Organic and unattributed are kept apart, as the DB keeps them; folding
unattributed into organic was what made the old organic revenue read 24% high.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

from excel_formatting import apply_sheet_formatting, section_format

CHANNEL_SHEET_KEYS = {
    "meta_ads": "meta",
    "google_ads": "google",
    "organic": "organic",
    "unattributed": "unattributed",
}

AD_LEVEL_COLS = [
    "campaign_name", "adset_name", "ad_name", "ad_id",
    "impressions", "clicks", "ctr", "spend",
    "orders", "returned_orders", "cancelled_orders",
    "gross_sales", "discounts", "return_revenue", "cancel_revenue", "net_sales",
    "product_cost", "shipping_cost", "packaging_cost", "payment_gateway_fees",
    "rto_cost", "net_cogs", "net_profit", "net_roas",
]
SKU_LEVEL_COLS = [
    "sku", "product_title", "quantity",
    "sku_net_sales", "sku_net_cogs", "sku_net_profit",
]
SHEET_COLS = ["date"] + AD_LEVEL_COLS + SKU_LEVEL_COLS

GRAND_TOTAL_LABEL = "Grand Total"
UNATTRIBUTED_AD_LABEL = "(No ad / channel-level)"

_SUM_COLS = [
    "impressions", "clicks", "spend", "orders", "returned_orders",
    "cancelled_orders", "gross_sales", "discounts", "return_revenue",
    "cancel_revenue", "net_sales", "product_cost", "shipping_cost",
    "packaging_cost", "payment_gateway_fees", "rto_cost", "net_cogs",
    "net_profit", "quantity", "sku_net_sales", "sku_net_cogs", "sku_net_profit",
]


def _d(value: str | date | datetime) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _num(value) -> float:
    try:
        if value is None or (isinstance(value, float) and value != value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _brand_clause(brand_id: Optional[int], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"AND {prefix}brand_id = %(brand_id)s" if brand_id is not None else ""


def fetch_ad_channel_pnl(
    start_date, end_date, brand_id: Optional[int] = None, client=None
) -> pd.DataFrame:
    """Ad-grain channel P&L -- the dashboard's source of truth."""
    from amazon_entity_report import get_clickhouse_client
    client = client or get_clickhouse_client()
    query = f"""
        SELECT
            report_date,
            channel,
            ifNull(campaign_name, '') AS campaign_name,
            ifNull(adset_name, '')    AS adset_name,
            ifNull(ad_name, '')       AS ad_name,
            ad_id,
            ifNull(is_unattributed, 0) AS is_unattributed,
            toFloat64(ifNull(impressions, 0)) AS impressions,
            toFloat64(ifNull(clicks, 0))      AS clicks,
            round(toFloat64(ifNull(ad_spend, 0)), 2)          AS spend,
            toInt64(ifNull(orders, 0))            AS orders,
            toInt64(ifNull(returned_orders, 0))   AS returned_orders,
            toInt64(ifNull(cancelled_orders, 0))  AS cancelled_orders,
            round(toFloat64(ifNull(gross_sales, 0)), 2)    AS gross_sales,
            round(toFloat64(ifNull(discounts, 0)), 2)      AS discounts,
            round(toFloat64(ifNull(return_revenue, 0)), 2) AS return_revenue,
            round(toFloat64(ifNull(cancel_revenue, 0)), 2) AS cancel_revenue,
            round(toFloat64(ifNull(net_sales, 0)), 2)      AS net_sales,
            round(toFloat64(ifNull(product_cost, 0)), 2)   AS product_cost,
            round(toFloat64(ifNull(shipping_cost, 0)), 2)  AS shipping_cost,
            round(toFloat64(ifNull(packaging_cost, 0)), 2) AS packaging_cost,
            round(toFloat64(ifNull(payment_gateway_fees, 0)), 2) AS payment_gateway_fees,
            round(toFloat64(ifNull(rto_cost, 0)), 2)       AS rto_cost,
            round(toFloat64(ifNull(net_cogs, 0)), 2)       AS net_cogs,
            round(toFloat64(ifNull(net_profit, 0)), 2)     AS net_profit
        FROM gold.fct_ad_channel_pnl_daily
        WHERE report_date BETWEEN %(start)s AND %(end)s
            {_brand_clause(brand_id)}
        ORDER BY channel, report_date, campaign_name, adset_name, ad_name
    """
    params = {"start": _d(start_date), "end": _d(end_date)}
    if brand_id is not None:
        params["brand_id"] = brand_id
    try:
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        print(
            f"[ClickHouse Channel P&L] {len(df)} ad-day rows for "
            f"{params['start']} to {params['end']}"
            + (f" (brand_id={brand_id})" if brand_id is not None else "")
            + (f", spend={df['spend'].sum():,.2f}, net_sales={df['net_sales'].sum():,.2f}"
               if not df.empty else "")
        )
        return df
    except Exception as e:
        print(f"[ClickHouse Channel P&L] failed ({e})")
        return pd.DataFrame()


def fetch_channel_sku_lines(
    start_date, end_date, brand_id: Optional[int] = None, client=None
) -> pd.DataFrame:
    """Order line items with the platform / ad their order is attributed to.

    Keyed on (order_date, lt_ad_id), which lines up with the P&L's
    (report_date, ad_id) -- verified 182/182 keys for Sep 2026 Meta, blank ad_id
    included (both sides carry a blank-ad bucket for channel-level orders).
    """
    from amazon_entity_report import get_clickhouse_client
    client = client or get_clickhouse_client()
    query = f"""
        SELECT
            oi.order_date AS report_date,
            ifNull(oa.lt_platform, 'unattributed') AS platform,
            ifNull(oa.lt_ad_id, '') AS ad_id,
            ifNull(oi.sku, '')      AS sku,
            ifNull(oi.product_title, '') AS product_title,
            toInt64(ifNull(oi.net_quantity, 0)) AS quantity,
            round(toFloat64(ifNull(oi.net_revenue, 0)), 2) AS line_net_revenue,
            round(toFloat64(ifNull(oi.net_cost, 0)), 2)    AS line_net_cost
        FROM gold.fct_order_items AS oi
        INNER JOIN gold.fct_order_attribution AS oa
            ON oa.order_id = oi.order_id AND oa.brand_id = oi.brand_id
        WHERE oi.order_date BETWEEN %(start)s AND %(end)s
            {_brand_clause(brand_id, 'oi')}
        ORDER BY platform, report_date, ad_id, sku
    """
    params = {"start": _d(start_date), "end": _d(end_date)}
    if brand_id is not None:
        params["brand_id"] = brand_id
    try:
        result = client.query(query, parameters=params)
        df = pd.DataFrame(result.result_rows, columns=result.column_names)
        print(
            f"[ClickHouse Channel SKU lines] {len(df)} lines for "
            f"{params['start']} to {params['end']}"
            + (f", platforms={sorted(df['platform'].unique())}" if not df.empty else "")
        )
        return df
    except Exception as e:
        print(f"[ClickHouse Channel SKU lines] failed ({e})")
        return pd.DataFrame()


def _sku_rows_for_ad(ad: pd.Series, lines: pd.DataFrame) -> list[dict]:
    """SKU rows for one ad-day, with the ad's money allocated by revenue share.

    Allocating (rather than carrying the line's own net_revenue) is what makes
    the SKU columns sum to the ad-level figures; the old sheets computed SKU
    revenue independently and never reconciled.
    """
    if lines is None or lines.empty:
        return []
    grouped = (
        lines.groupby(["sku", "product_title"], dropna=False)
        .agg(quantity=("quantity", "sum"),
             line_net_revenue=("line_net_revenue", "sum"),
             line_net_cost=("line_net_cost", "sum"))
        .reset_index()
    )
    basis = float(grouped["line_net_revenue"].sum())
    net_sales = _num(ad.get("net_sales"))
    net_cogs = _num(ad.get("net_cogs"))
    n = len(grouped)

    rows = []
    for i, (_, line) in enumerate(grouped.iterrows()):
        share = (float(line["line_net_revenue"]) / basis) if basis else (1.0 / n)
        row = {c: None for c in SHEET_COLS}
        row["sku"] = line["sku"]
        row["product_title"] = line["product_title"]
        row["quantity"] = int(line["quantity"])
        row["sku_net_sales"] = round(net_sales * share, 2)
        row["sku_net_cogs"] = round(net_cogs * share, 2)
        row["sku_net_profit"] = round(row["sku_net_sales"] - row["sku_net_cogs"], 2)
        rows.append(row)

    # Push rounding residue onto the largest row so the block ties exactly.
    if rows:
        for col, target in (("sku_net_sales", net_sales), ("sku_net_cogs", net_cogs)):
            drift = round(target - sum(_num(r[col]) for r in rows), 2)
            if drift:
                biggest = max(rows, key=lambda r: abs(_num(r["sku_net_sales"])))
                biggest[col] = round(_num(biggest[col]) + drift, 2)
        for r in rows:
            r["sku_net_profit"] = round(_num(r["sku_net_sales"]) - _num(r["sku_net_cogs"]), 2)
    return rows


def build_channel_sheet(
    pnl_df: pd.DataFrame, sku_df: pd.DataFrame, channel: str
) -> tuple[pd.DataFrame, list[tuple[int, int]]]:
    """Assemble one channel's sheet: Grand Total first, then ad blocks.

    Ad-level values are written once per block (on its first row) so every
    numeric column sums correctly down the sheet.
    """
    if pnl_df is None or pnl_df.empty:
        return pd.DataFrame(columns=SHEET_COLS), []
    ads = pnl_df[pnl_df["channel"] == channel].copy()
    if ads.empty:
        return pd.DataFrame(columns=SHEET_COLS), []

    lines = pd.DataFrame()
    if sku_df is not None and not sku_df.empty:
        lines = sku_df[sku_df["platform"] == channel].copy()
        if lines.empty and channel == "organic":
            # fct_order_attribution labels organic orders 'other'
            lines = sku_df[sku_df["platform"] == "other"].copy()

    rows: list[dict] = []
    ad_spans: list[tuple[int, int]] = []

    # (report_date, ad_id) is NOT unique in the P&L -- Google in particular
    # splits one ad_id across many campaign rows (130 rows over 4 ad_ids), and
    # blank ad_id is a shared channel-level bucket. Attaching the SKU lines to
    # every matching row would count their quantity several times (Google read
    # 1433 units against 338 actual), so each (date, ad_id) group hands its
    # lines to just one row: the one carrying the most net_sales. The other rows
    # keep their own ad-level money, so the column totals are unaffected.
    ads = ads.reset_index(drop=True)
    sku_owner: set = set()
    if not lines.empty:
        key = list(zip(ads["report_date"], ads["ad_id"].astype(str)))
        ads["_key"] = key
        sku_owner = set(
            ads.groupby("_key")["net_sales"].idxmax().tolist()
        )
        ads = ads.drop(columns=["_key"])

    for pos, ad in ads.iterrows():
        block = pd.DataFrame()
        if not lines.empty and pos in sku_owner:
            block = lines[(lines["report_date"] == ad["report_date"])
                          & (lines["ad_id"].astype(str) == str(ad["ad_id"]))]
        child_rows = _sku_rows_for_ad(ad, block)

        head = {c: None for c in SHEET_COLS}
        head["date"] = ad["report_date"]
        for col in AD_LEVEL_COLS:
            if col in ads.columns:
                head[col] = ad[col]
        if not str(head.get("ad_name") or "").strip():
            head["ad_name"] = UNATTRIBUTED_AD_LABEL
        impressions, clicks = _num(ad.get("impressions")), _num(ad.get("clicks"))
        head["ctr"] = round(clicks / impressions * 100, 2) if impressions else 0.0
        spend = _num(ad.get("spend"))
        head["net_roas"] = round(
            (_num(ad.get("net_sales")) - _num(ad.get("net_cogs"))) / spend, 2
        ) if spend else 0.0

        if child_rows:
            for col in ["date"] + AD_LEVEL_COLS:
                child_rows[0][col] = head[col]
            start = len(rows)
            rows.extend(child_rows)
            ad_spans.append((start, len(rows) - 1))
        else:
            rows.append(head)

    df = pd.DataFrame(rows, columns=SHEET_COLS)

    total = {c: None for c in SHEET_COLS}
    total["campaign_name"] = GRAND_TOTAL_LABEL
    for col in _SUM_COLS:
        total[col] = round(float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum()), 2)
    imp, clk = _num(total["impressions"]), _num(total["clicks"])
    total["ctr"] = round(clk / imp * 100, 2) if imp else 0.0
    spend = _num(total["spend"])
    total["net_roas"] = round(
        (_num(total["net_sales"]) - _num(total["net_cogs"])) / spend, 2) if spend else 0.0

    df = pd.concat([pd.DataFrame([total], columns=SHEET_COLS), df], ignore_index=True)
    return df, [(a + 1, b + 1) for a, b in ad_spans]


def write_channel_sheet(
    writer, sheet_name: str, df: pd.DataFrame, ad_spans: list[tuple[int, int]]
) -> None:
    """Write a channel sheet with ad-level cells merged down their block."""
    if df is None or df.empty:
        pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
        return
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    apply_sheet_formatting(
        writer, sheet_name, df,
        total_rows=1,
        heatmap_cols=("spend", "net_sales"),
        sign_cols=("net_profit", "sku_net_profit"),
        threshold_cols=("net_roas",),
    )

    worksheet = writer.sheets.get(sheet_name)
    if worksheet is None:
        return
    workbook = writer.book
    merge_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})
    date_fmt = workbook.add_format({"align": "center", "valign": "vcenter",
                                    "num_format": "dd-mm-yyyy"})
    money_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                     "num_format": "#,##0.00"})
    int_fmt = workbook.add_format({"align": "right", "valign": "vcenter",
                                   "num_format": "#,##0"})
    money_cols = {"spend", "gross_sales", "discounts", "return_revenue",
                  "cancel_revenue", "net_sales", "product_cost", "shipping_cost",
                  "packaging_cost", "payment_gateway_fees", "rto_cost",
                  "net_cogs", "net_profit"}
    int_cols = {"impressions", "clicks", "orders", "returned_orders", "cancelled_orders"}

    def _fmt(col):
        if col == "date":
            return date_fmt
        if col in money_cols:
            return money_fmt
        if col in int_cols:
            return int_fmt
        return merge_fmt

    for start, end in ad_spans:
        if end <= start:
            continue
        for col in ["date"] + AD_LEVEL_COLS:
            if col not in df.columns:
                continue
            idx = df.columns.get_loc(col)
            value = df.iloc[start][col]
            if value is None or (isinstance(value, float) and value != value):
                value = ""
            worksheet.merge_range(start + 1, idx, end + 1, idx, value, _fmt(col))


def channel_summary(df: pd.DataFrame) -> dict:
    """Headline numbers for a built channel sheet, taken from its total row."""
    if df is None or df.empty:
        return {"revenue": 0.0, "cogs": 0.0, "spend": 0.0, "orders": 0,
                "quantity": 0, "net_profit": 0.0, "net_roas": 0.0}
    t = df.iloc[0]
    return {
        "revenue": _num(t.get("net_sales")),
        "cogs": _num(t.get("net_cogs")),
        "spend": _num(t.get("spend")),
        "orders": int(_num(t.get("orders"))),
        "quantity": int(_num(t.get("quantity"))),
        "net_profit": _num(t.get("net_profit")),
        "net_roas": _num(t.get("net_roas")),
    }


SHEET_KEY_BY_CHANNEL = {"meta": "meta_ads", "google": "google_ads",
                        "organic": "organic", "unattributed": "unattributed"}
DISPLAY_NAME = {"meta_ads": "Meta Ads", "google_ads": "Google Ads",
                "organic": "Organic", "unattributed": "Unattributed"}


def _campaign_ranking(ads: pd.DataFrame, limit: int = 5) -> tuple[list, list]:
    """Top / bottom campaigns by net profit, for the email body."""
    if ads is None or ads.empty or "campaign_name" not in ads.columns:
        return [], []
    agg = (
        ads.groupby("campaign_name", dropna=False)
        .agg(revenue=("net_sales", "sum"), spend=("spend", "sum"),
             cogs=("net_cogs", "sum"), net_profit=("net_profit", "sum"),
             orders=("orders", "sum"))
        .reset_index()
    )
    agg = agg[agg["campaign_name"].astype(str).str.strip() != ""]
    if agg.empty:
        return [], []
    agg["net_roas"] = np.where(
        agg["spend"] > 0, (agg["revenue"] - agg["cogs"]) / agg["spend"], 0.0)
    ranked = agg.sort_values("net_profit", ascending=False)

    def _rows(frame):
        return [{
            "name": str(r["campaign_name"]),
            "revenue": round(float(r["revenue"]), 2),
            "spend": round(float(r["spend"]), 2),
            "net_profit": round(float(r["net_profit"]), 2),
            "net_roas": round(float(r["net_roas"]), 2),
            "orders": int(r["orders"]),
        } for _, r in frame.iterrows()]

    return _rows(ranked.head(limit)), _rows(ranked.tail(limit).iloc[::-1])


def channel_summaries_from_pnl(
    pnl_df: pd.DataFrame, sku_df: pd.DataFrame
) -> dict[str, dict]:
    """Per-channel KPI dicts for the email, in the shape the email expects.

    Same source as the sheets, so the email body and the attachments agree --
    they did not while the email ran off the marketing API.
    """
    out: dict[str, dict] = {}
    if pnl_df is None or pnl_df.empty:
        return out

    qty_by_channel = {}
    if sku_df is not None and not sku_df.empty:
        tmp = sku_df.copy()
        tmp["platform"] = tmp["platform"].replace({"other": "organic"})
        qty_by_channel = tmp.groupby("platform")["quantity"].sum().to_dict()

    for channel, sheet_key in SHEET_KEY_BY_CHANNEL.items():
        ads = pnl_df[pnl_df["channel"] == channel]
        if ads.empty:
            continue
        revenue = float(ads["net_sales"].sum())
        cogs = float(ads["net_cogs"].sum())
        spend = float(ads["spend"].sum())
        orders = int(ads["orders"].sum())
        quantity = int(qty_by_channel.get(channel, 0))
        net_profit = float(ads["net_profit"].sum())
        top, bottom = _campaign_ranking(ads)
        out[sheet_key] = {
            "revenue": round(revenue, 2),
            "cogs": round(cogs, 2),
            "spend": round(spend, 2),
            "orders": orders,
            "order_count": orders,
            "quantity": quantity,
            "net_roas": round((revenue - cogs) / spend, 2) if spend else 0.0,
            "net_profit": round(net_profit, 2),
            "cost_per_order": round(spend / orders, 2) if orders else 0.0,
            "cost_per_unit": round(spend / quantity, 2) if quantity else 0.0,
            "avg_order_value": round(revenue / orders, 2) if orders else 0.0,
            "top_campaigns": top,
            "bottom_campaigns": bottom,
        }
    return out


def dashboard_rows_from_summaries(
    summaries: dict[str, dict], amazon: Optional[dict] = None
) -> tuple[list, dict, dict]:
    """(channel_rows, total, canonical_totals) for the email, from the same source.

    Amazon is passed through untouched -- it is not in fct_ad_channel_pnl_daily
    and keeps its own settlement-based P&L.
    """
    rows = []
    for sheet_key in ("meta_ads", "google_ads", "organic", "unattributed"):
        s = summaries.get(sheet_key)
        if not s:
            continue
        rows.append((DISPLAY_NAME[sheet_key], {
            "sales": s["revenue"], "cogs": s["cogs"], "ad_spend": s["spend"],
            "net_profit": s["net_profit"], "net_roas": s["net_roas"],
            "order_count": s["orders"], "units": s["quantity"],
        }))
    if amazon:
        rows.append(("Amazon", {
            "sales": float(amazon.get("revenue", 0) or 0),
            "cogs": float(amazon.get("cogs", 0) or 0),
            "ad_spend": float(amazon.get("spend", 0) or 0),
            "net_profit": float(amazon.get("net_profit", 0) or 0),
            "net_roas": float(amazon.get("net_roas", 0) or 0),
            "order_count": int(amazon.get("orders", 0) or 0),
            "units": int(amazon.get("quantity", 0) or 0),
        }))

    total = {k: 0.0 for k in ("sales", "cogs", "ad_spend", "net_profit")}
    total.update({"order_count": 0, "units": 0})
    for _, r in rows:
        for k in ("sales", "cogs", "ad_spend", "net_profit"):
            total[k] = round(total[k] + float(r.get(k, 0) or 0), 2)
        total["order_count"] += int(r.get("order_count", 0) or 0)
        total["units"] += int(r.get("units", 0) or 0)
    margin = total["sales"] - total["cogs"]
    total["net_roas"] = round(margin / total["ad_spend"], 2) if total["ad_spend"] else 0.0
    total["quantity"] = total["units"]

    canonical = {
        "revenue": total["sales"], "cogs": total["cogs"],
        "ad_spend": total["ad_spend"], "net_profit": total["net_profit"],
        "orders": total["order_count"],
    }
    return rows, total, canonical


CAMPAIGN_COLS = ["campaign_name", "impressions", "clicks", "ctr", "spend",
                 "orders", "revenue", "cogs", "net_profit", "net_roas"]


def build_campaign_rollup(pnl_df: pd.DataFrame, channel: str) -> pd.DataFrame:
    """Campaign-grain rollup with Grand Total first.

    Column names stay ``revenue`` / ``cogs`` / ``orders`` because the daily
    report's own readers key off them: extract_daily_campaign_performers reads
    meta_campaigns / google_campaigns, and extract_daily_efficiency_metrics
    parses their 'Grand Total' row by campaign_name.
    """
    if pnl_df is None or pnl_df.empty:
        return pd.DataFrame(columns=CAMPAIGN_COLS)
    ads = pnl_df[pnl_df["channel"] == channel]
    if ads.empty:
        return pd.DataFrame(columns=CAMPAIGN_COLS)

    agg = (
        ads.groupby("campaign_name", dropna=False)
        .agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"),
             spend=("spend", "sum"), orders=("orders", "sum"),
             revenue=("net_sales", "sum"), cogs=("net_cogs", "sum"),
             net_profit=("net_profit", "sum"))
        .reset_index()
    )
    agg["campaign_name"] = agg["campaign_name"].replace("", UNATTRIBUTED_AD_LABEL)
    agg["ctr"] = np.where(agg["impressions"] > 0,
                          agg["clicks"] / agg["impressions"] * 100, 0.0).round(2)
    agg["net_roas"] = np.where(agg["spend"] > 0,
                               (agg["revenue"] - agg["cogs"]) / agg["spend"], 0.0).round(2)
    agg = agg.sort_values("spend", ascending=False).reset_index(drop=True)

    total = {"campaign_name": GRAND_TOTAL_LABEL}
    for col in ("impressions", "clicks", "spend", "orders", "revenue", "cogs", "net_profit"):
        total[col] = round(float(agg[col].sum()), 2)
    total["ctr"] = round(total["clicks"] / total["impressions"] * 100, 2) if total["impressions"] else 0.0
    total["net_roas"] = round(
        (total["revenue"] - total["cogs"]) / total["spend"], 2) if total["spend"] else 0.0

    out = pd.concat([pd.DataFrame([total]), agg], ignore_index=True)
    return out[[c for c in CAMPAIGN_COLS if c in out.columns]]
