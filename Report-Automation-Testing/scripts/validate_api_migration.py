#!/usr/bin/env python3
"""
Validate API migration wiring (no live API required).

Checks:
  - Core modules import
  - v1 client functions exist
  - USE_API_ONLY respected in key modules
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

REQUIRED_V1_FUNCS = [
    "fetch_v1",
    "fetch_historical_dashboard",
    "fetch_meta_attribution",
    "fetch_google_attribution",
    "fetch_channel_attribution",
    "fetch_amazon_attribution",
    "fetch_historical_time_patterns",
    "fetch_marketing_from_api",
]

MODULES = [
    "metric_calculators",
    "api_response_transformers",
    "api_data_fetcher",
    "amazon_entity_report",
    "clickhouse_report",
    "channel_performance",
    "dashboard_stats",
]


def main():
    errors = []
    for mod in MODULES:
        try:
            __import__(mod)
        except Exception as e:
            errors.append(f"import {mod}: {e}")

    import api_data_fetcher as adf
    for fn in REQUIRED_V1_FUNCS:
        if not hasattr(adf, fn):
            errors.append(f"missing api_data_fetcher.{fn}")

    if not hasattr(adf, "USE_API_ONLY"):
        errors.append("missing USE_API_ONLY flag")

    for path in (
        "clickhouse_report.py",
        "channel_performance.py",
        "api_data_fetcher.py",
    ):
        full = os.path.join(PROJECT_ROOT, path)
        text = open(full, encoding="utf-8").read()
        if "USE_API_ONLY" not in text:
            errors.append(f"{path} does not reference USE_API_ONLY")

    amazon_entity = os.path.join(PROJECT_ROOT, "amazon_entity_report.py")
    amazon_text = open(amazon_entity, encoding="utf-8").read()
    if "AMAZON_ENTITY_CLICKHOUSE_PRIMARY" not in amazon_text:
        errors.append("amazon_entity_report.py missing AMAZON_ENTITY_CLICKHOUSE_PRIMARY")

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("API migration validation passed")
    print(f"  modules: {len(MODULES)}")
    print(f"  v1 functions: {len(REQUIRED_V1_FUNCS)}")
    print("  Set USE_API_ONLY=true after live API smoke tests pass")


if __name__ == "__main__":
    main()
