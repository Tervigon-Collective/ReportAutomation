#!/usr/bin/env python3
"""
Smoke-test Node-Backend v1 reporting endpoints.

Usage (from Report-Automation-Testing/):
    python scripts/test_api_endpoints.py
    python scripts/test_api_endpoints.py --start 2026-03-01 --end 2026-03-07
    python scripts/test_api_endpoints.py --json report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Any, Callable, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from api_data_fetcher import (  # noqa: E402
    BASE_URL,
    _api_root,
    fetch_amazon_attribution,
    fetch_channel_attribution,
    fetch_google_attribution,
    fetch_historical_amazon_ads,
    fetch_historical_amazon_dashboard,
    fetch_historical_amazon_sp_sales,
    fetch_historical_dashboard,
    fetch_historical_google_ads,
    fetch_historical_meta_ads,
    fetch_historical_sales_by_region,
    fetch_historical_time_patterns,
    fetch_meta_attribution,
    fetch_meta_funnel,
    fetch_pnl_summary,
    get_api_brand_id,
    get_api_company_id,
    get_firebase_token,
    make_authenticated_request,
)


def _default_range() -> tuple[str, str]:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _has_keys(obj: Any, keys: list[str]) -> list[str]:
    missing = []
    if not isinstance(obj, dict):
        return keys
    for k in keys:
        if k not in obj:
            missing.append(k)
    return missing


EndpointSpec = tuple[str, Callable[..., Optional[dict]], dict, list[str]]


def _specs(start: str, end: str) -> list[EndpointSpec]:
    return [
        ("historical/dashboard", fetch_historical_dashboard, {}, [
            "net_sales", "total_ad_spend", "total_cogs", "net_profit",
        ]),
        ("historical/time-patterns", fetch_historical_time_patterns, {}, []),
        ("historical/sales-by-region", fetch_historical_sales_by_region, {}, []),
        ("historical/meta/ads", fetch_historical_meta_ads, {}, []),
        ("historical/google/ads", fetch_historical_google_ads, {}, []),
        ("historical/amazon/dashboard", fetch_historical_amazon_dashboard, {}, ["summary"]),
        ("historical/amazon/ads", fetch_historical_amazon_ads, {}, ["campaigns"]),
        ("historical/amazon-sp-sales", fetch_historical_amazon_sp_sales, {}, []),
        ("meta-attribution", fetch_meta_attribution, {"time_aggregation": "daily"}, ["summary"]),
        ("google-attribution", fetch_google_attribution, {"time_aggregation": "daily"}, ["summary"]),
        ("channel-attribution", fetch_channel_attribution, {"channel": "organic"}, []),
        ("amazon-attribution", fetch_amazon_attribution, {}, ["summary"]),
        ("meta-funnel", fetch_meta_funnel, {}, ["summary"]),
        ("pnl/summary", fetch_pnl_summary, {}, []),
    ]


def _call_fetch(fn: Callable, start: str, end: str, extra: dict) -> Optional[dict]:
    if fn is fetch_channel_attribution:
        return fn(start, end, channel=extra.get("channel", "organic"))
    if fn is fetch_meta_attribution or fn is fetch_google_attribution:
        return fn(start, end, time_aggregation=extra.get("time_aggregation"))
    return fn(start, end)


def _probe_endpoint(name: str, start: str, end: str, extra: dict) -> tuple[Optional[dict], Optional[int], Optional[str]]:
    """Raw HTTP probe returning (data, status_code, error_message)."""
    params = {
        "brand_id": get_api_brand_id(),
        "company_id": get_api_company_id(),
        "start_date": start,
        "end_date": end,
        **{k: v for k, v in extra.items() if k not in ("time_aggregation", "channel")},
    }
    if "time_aggregation" in extra:
        params["time_aggregation"] = extra["time_aggregation"]
    if "channel" in extra:
        params["channel"] = extra["channel"]
    if name == "pnl/summary":
        from api_data_fetcher import pnl_end_exclusive
        params = {
            "brand_id": get_api_brand_id(),
            "company_id": get_api_company_id(),
            "startDate": start,
            "endDate": pnl_end_exclusive(end),
        }
    url = f"{_api_root()}/v1/{name.lstrip('/')}"
    try:
        resp = make_authenticated_request("GET", url, params=params, timeout=120)
        if resp.status_code != 200:
            return None, resp.status_code, resp.text[:200]
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            return None, resp.status_code, str(payload.get("error"))
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return data if isinstance(data, dict) else {"data": data}, resp.status_code, None
    except Exception as exc:
        return None, None, str(exc)


def run_tests(start: str, end: str) -> list[dict]:
    results = []
    token = get_firebase_token()
    if not token:
        print("WARNING: No bearer token — set JWT_ACCESS_TOKEN or BACKEND_EMAIL/BACKEND_PASSWORD")

    print(f"Base URL: {BASE_URL}")
    print(f"brand_id={get_api_brand_id()} company_id={get_api_company_id()}")
    print(f"Auth token: {'yes' if token else 'NO'} (len={len(token) if token else 0})")
    print(f"Date range: {start} to {end}\n")

    for name, fn, extra, required in _specs(start, end):
        t0 = time.perf_counter()
        err = None
        data = None
        missing = []
        http_status = None
        try:
            data, http_status, probe_err = _probe_endpoint(name, start, end, extra)
            if probe_err:
                err = probe_err
            elif data is None:
                err = "null response"
            else:
                missing = _has_keys(data, required)
        except Exception as exc:
            err = str(exc)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ok = data is not None and not err and not missing
        status = "PASS" if ok else "FAIL"
        row = {
            "endpoint": name,
            "status": status,
            "http_status": http_status,
            "latency_ms": round(elapsed_ms, 1),
            "error": err,
            "missing_fields": missing,
            "has_data": data is not None,
        }
        results.append(row)
        extra_msg = f" missing={missing}" if missing else ""
        http_msg = f" HTTP {http_status}" if http_status else ""
        err_msg = f" error={err}" if err else ""
        print(f"[{status}] {name:40s} {elapsed_ms:7.1f}ms{http_msg}{err_msg}{extra_msg}")

    passed = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n{passed}/{len(results)} endpoints passed")
    return results


def main():
    parser = argparse.ArgumentParser(description="Smoke-test v1 reporting APIs")
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--json", metavar="PATH", help="Write JSON report to file")
    args = parser.parse_args()

    start, end = args.start, args.end
    if not start or not end:
        start, end = _default_range()

    results = run_tests(start, end)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"start": start, "end": end, "results": results}, f, indent=2)
        print(f"Wrote {args.json}")

    sys.exit(0 if all(r["status"] == "PASS" for r in results) else 1)


if __name__ == "__main__":
    main()
