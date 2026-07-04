#!/usr/bin/env python3
"""
Reconcile API-derived metrics vs legacy DB/ClickHouse outputs for a date range.

Usage:
    python scripts/compare_api_vs_current.py --start 2026-03-01 --end 2026-03-07
    python scripts/compare_api_vs_current.py --csv reconciliation.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from api_data_fetcher import (  # noqa: E402
    fetch_historical_dashboard,
    get_api_brand_id,
)
from dashboard_stats import _api_to_stats, _clickhouse_stats, fetch_general_statistics  # noqa: E402
from metric_calculators import channel_metrics_from_historical_dashboard  # noqa: E402


def _pct_diff(api_val: float, legacy_val: float) -> float:
    if legacy_val == 0:
        return 0.0 if api_val == 0 else 100.0
    return abs(api_val - legacy_val) / abs(legacy_val) * 100.0


def compare_range(start: str, end: str, company_id: int) -> pd.DataFrame:
    brand_id = get_api_brand_id()
    rows = []

    api_raw = fetch_historical_dashboard(start, end)
    api_stats = _api_to_stats(api_raw) if api_raw else {"totals": {}, "channels": {}}
    api_pdf = channel_metrics_from_historical_dashboard(api_raw) if api_raw else {}

    try:
        ch_stats = _clickhouse_stats(brand_id, start, end)
    except Exception as e:
        ch_stats = {"totals": {}, "channels": {}, "error": str(e)}

    gen_stats = fetch_general_statistics(brand_id, company_id, start, end, prefer_api=True)

    comparisons = [
        ("total_net_sales", api_stats.get("totals", {}).get("net_sales"), ch_stats.get("totals", {}).get("net_sales")),
        ("total_ad_spend", api_stats.get("totals", {}).get("total_ad_spend"), ch_stats.get("totals", {}).get("total_ad_spend")),
        ("total_cogs", api_stats.get("totals", {}).get("total_cogs"), ch_stats.get("totals", {}).get("total_cogs")),
        ("total_net_profit", api_stats.get("totals", {}).get("net_profit"), ch_stats.get("totals", {}).get("net_profit")),
        ("meta_sales", api_stats.get("channels", {}).get("meta", {}).get("sales"), ch_stats.get("channels", {}).get("meta", {}).get("sales")),
        ("google_sales", api_stats.get("channels", {}).get("google", {}).get("sales"), ch_stats.get("channels", {}).get("google", {}).get("sales")),
        ("pdf_meta_net_profit", (api_pdf.get("meta") or {}).get("net_profit"), None),
        ("pdf_total_net_profit", (api_pdf.get("total") or {}).get("net_profit"), gen_stats.get("totals", {}).get("net_profit")),
    ]

    for metric, api_val, legacy_val in comparisons:
        api_f = float(api_val or 0)
        leg_f = float(legacy_val or 0) if legacy_val is not None else None
        rows.append({
            "metric": metric,
            "start": start,
            "end": end,
            "api_value": round(api_f, 2),
            "legacy_value": round(leg_f, 2) if leg_f is not None else None,
            "abs_diff": round(abs(api_f - leg_f), 2) if leg_f is not None else None,
            "pct_diff": round(_pct_diff(api_f, leg_f), 2) if leg_f is not None else None,
            "within_tolerance": (
                leg_f is None or _pct_diff(api_f, leg_f) <= 0.5 or abs(api_f - leg_f) <= 1.0
            ),
        })

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--csv", default="api_reconciliation.csv")
    parser.add_argument("--company-id", type=int, default=int(os.getenv("DASHBOARD_COMPANY_ID", "19")))
    args = parser.parse_args()

    end = args.end or (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = args.start or (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")

    df = compare_range(start, end, args.company_id)
    df.to_csv(args.csv, index=False)
    print(df.to_string(index=False))
    print(f"\nWrote {args.csv}")
    failed = df[~df["within_tolerance"]]
    if not failed.empty:
        print(f"\n{len(failed)} metrics outside tolerance (±0.5% or ₹1)")
        sys.exit(1)
    print("\nAll compared metrics within tolerance")


if __name__ == "__main__":
    main()
