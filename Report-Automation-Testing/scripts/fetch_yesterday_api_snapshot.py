#!/usr/bin/env python3
"""Fetch yesterday's reporting data via Node-Backend v1 API and print a summary."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from api_data_fetcher import (  # noqa: E402
    _decode_jwt_claims,
    _jwt_expires_at,
    fetch_amazon_attribution,
    fetch_channel_attribution,
    fetch_google_attribution,
    fetch_historical_amazon_dashboard,
    fetch_historical_dashboard,
    fetch_historical_sales_by_region,
    fetch_historical_time_patterns,
    fetch_meta_attribution,
    fetch_meta_funnel,
    get_backend_jwt_token,
)
from api_response_transformers import (  # noqa: E402
    flatten_google_attribution,
    flatten_meta_attribution,
    flatten_organic_attribution,
)


def _fmt_inr(val) -> str:
    try:
        return f"₹{float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def main() -> int:
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    d = yesterday

    token = get_backend_jwt_token()
    if not token:
        print("ERROR: Could not obtain backend JWT")
        return 1

    claims = _decode_jwt_claims(token)
    exp = _jwt_expires_at(token)
    print(f"Auth: {claims.get('email')} company={claims.get('company_id')} brand={claims.get('brand_id')}")
    print(f"JWT expires: {exp}")
    print(f"Date: {d} (yesterday)\n")

    dash = fetch_historical_dashboard(d, d) or {}
    print("=== Historical Dashboard (single day) ===")
    for key in (
        "net_sales", "total_ad_spend", "total_cogs", "net_profit",
        "net_roas", "be_roas", "total_orders", "aov",
    ):
        if key in dash:
            val = dash[key]
            print(f"  {key}: {_fmt_inr(val) if 'roas' not in key and key != 'total_orders' else val}")

    channels = dash.get("channels") or dash.get("channel_breakdown") or {}
    if channels:
        print("\n  Channel breakdown:")
        if isinstance(channels, dict):
            for ch, metrics in channels.items():
                if isinstance(metrics, dict):
                    spend = metrics.get("ad_spend") or metrics.get("spend")
                    sales = metrics.get("net_sales") or metrics.get("sales")
                    print(f"    {ch}: spend={_fmt_inr(spend)} sales={_fmt_inr(sales)}")
                else:
                    print(f"    {ch}: {metrics}")
        elif isinstance(channels, list):
            for row in channels[:8]:
                name = row.get("channel") or row.get("name")
                print(f"    {name}: {row}")

    tp = fetch_historical_time_patterns(d, d) or {}
    series = tp.get("time_series") or tp.get("series") or tp.get("data") or []
    print(f"\n=== Time patterns: {len(series) if isinstance(series, list) else 'n/a'} points ===")

    region = fetch_historical_sales_by_region(d, d) or {}
    regions = region.get("regions") or region.get("data") or region.get("by_state") or []
    print(f"=== Sales by region: {len(regions) if isinstance(regions, list) else 'n/a'} regions ===")
    if isinstance(regions, list) and regions:
        top = sorted(
            regions,
            key=lambda r: float(r.get("net_sales") or r.get("sales") or 0),
            reverse=True,
        )[:5]
        for r in top:
            st = r.get("state") or r.get("region") or r.get("name")
            sales = r.get("net_sales") or r.get("sales")
            print(f"    {st}: {_fmt_inr(sales)}")

    meta_rows = flatten_meta_attribution(fetch_meta_attribution(d, d) or {})
    google_rows = flatten_google_attribution(fetch_google_attribution(d, d) or {})
    channel_rows = flatten_organic_attribution(fetch_channel_attribution(d, d) or {})
    print(f"\n=== Attribution rows ===")
    print(f"  Meta: {len(meta_rows)} rows")
    print(f"  Google: {len(google_rows)} rows")
    print(f"  Channel (organic/etc): {len(channel_rows)} rows")

    funnel = fetch_meta_funnel(d, d) or {}
    stages = funnel.get("stages") or funnel.get("funnel") or funnel
    print(f"\n=== Meta funnel keys: {list(stages.keys()) if isinstance(stages, dict) else type(stages).__name__} ===")

    amz = fetch_historical_amazon_dashboard(d, d) or {}
    summary = amz.get("summary") or amz
    print("\n=== Amazon dashboard ===")
    if isinstance(summary, dict):
        for key in ("net_sales", "ad_spend", "net_profit", "orders", "acos", "roas"):
            if key in summary:
                print(f"  {key}: {summary[key]}")

    amz_attr = fetch_amazon_attribution(d, d) or {}
    campaigns = amz_attr.get("campaigns") or amz_attr.get("data") or []
    print(f"  Amazon attribution campaigns: {len(campaigns) if isinstance(campaigns, list) else 'n/a'}")

    print("\n=== Raw dashboard JSON (truncated) ===")
    print(json.dumps(dash, indent=2, default=str)[:2500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
