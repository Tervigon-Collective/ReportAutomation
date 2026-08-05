"""Reconcile report plot sources vs Historical dashboard for sample days."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd

from api_data_fetcher import (
    fetch_historical_dashboard,
    fetch_net_profit_series_from_api,
)
from channel_performance import (
    MIN_SPEND_FOR_ROAS,
    fetch_channel_performance,
    _prepare_daily_roas,
)


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _amz_spend(raw) -> float:
    if isinstance(raw, dict):
        return _f(raw.get("total"))
    return _f(raw)


def dashboard_day(d: str) -> dict:
    dash = fetch_historical_dashboard(d, d) or {}
    ns = dash.get("net_sales_breakdown") or {}
    cogs = dash.get("cogs_breakdown") or {}
    ad = dash.get("ad_spend_breakdown") or {}
    amz = dash.get("amazon") or {}
    meta_spend = _f(ad.get("meta"))
    google_spend = _f(ad.get("google"))
    amazon_spend = _amz_spend(ad.get("amazon"))
    meta_rev = _f(ns.get("meta"))
    google_rev = _f(ns.get("google"))
    amazon_rev = _f(amz.get("net_sales") or amz.get("gross_sales"))
    meta_cogs = _f(cogs.get("meta"))
    google_cogs = _f(cogs.get("google"))
    amazon_cogs = _f(amz.get("cogs"))
    out = {
        "net_sales_total": _f(dash.get("net_sales")),
        "net_profit_total": _f(dash.get("net_profit")),
        "ad_spend_total": _f(dash.get("total_ad_spend") or dash.get("ad_spend")),
        "net_roas_total": _f(dash.get("net_roas")),
        "meta": {
            "spend": meta_spend,
            "revenue": meta_rev,
            "cogs": meta_cogs,
            "net_profit": meta_rev - meta_cogs - meta_spend,
            "net_roas": (
                (meta_rev - meta_cogs) / meta_spend
                if meta_spend >= MIN_SPEND_FOR_ROAS
                else None
            ),
        },
        "google": {
            "spend": google_spend,
            "revenue": google_rev,
            "cogs": google_cogs,
            "net_profit": google_rev - google_cogs - google_spend,
            "net_roas": (
                (google_rev - google_cogs) / google_spend
                if google_spend >= MIN_SPEND_FOR_ROAS
                else None
            ),
        },
        "amazon": {
            "spend": amazon_spend,
            "revenue": amazon_rev,
            "cogs": amazon_cogs,
            "net_profit": amazon_rev - amazon_cogs - amazon_spend,
            "net_roas": (
                (amazon_rev - amazon_cogs) / amazon_spend
                if amazon_spend >= MIN_SPEND_FOR_ROAS
                else None
            ),
            "gross_roas": (
                amazon_rev / amazon_spend if amazon_spend >= MIN_SPEND_FOR_ROAS else None
            ),
        },
    }
    return out


def plot_channel_day(d: str) -> dict:
    df = fetch_channel_performance(d, d, brand_id=20)
    out = {}
    if df is None or df.empty:
        return out
    for platform in ("meta", "google", "amazon"):
        sub = df[df["platform"] == platform]
        if sub.empty:
            continue
        spend = float(sub["ad_spend"].sum())
        rev = float(sub["gross_revenue_excl_gst"].sum())
        cogs = float(sub["cogs"].sum())
        np = float(sub["net_profit"].sum())
        out[platform] = {
            "spend": spend,
            "revenue": rev,
            "cogs": cogs,
            "net_profit": np,
            "net_roas": (
                (rev - cogs) / spend if spend >= MIN_SPEND_FOR_ROAS else None
            ),
            "gross_roas": (
                rev / spend if spend >= MIN_SPEND_FOR_ROAS else None
            ),
        }
    return out


def asis_day(d: str) -> dict:
    # Uses Historical dashboard per-day series (same path as as-is NP plot).
    ser = fetch_net_profit_series_from_api(d, d)
    if ser is None or ser.empty:
        return {}
    row = ser.iloc[0]
    return {
        "revenue": float(row["revenue"]),
        "cogs": float(row["cogs"]),
        "total_ad_spend": float(row["total_ad_spend"]),
        "net_profit": float(row["net_profit"]),
    }


def amazon_roas_prep(d: str) -> float | None:
    # pull a short window so prepare has context
    start = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    raw = fetch_channel_performance(start, d, brand_id=20)
    daily = _prepare_daily_roas(raw, min_plot_date=d)
    amz = daily[daily["platform"] == "amazon"]
    if amz.empty:
        return None
    v = amz.iloc[0].get("gross_roas")
    if pd.isna(v):
        return None
    return float(v)


def cmp(label: str, a, b, tol: float = 1.0) -> str:
    if a is None and b is None:
        return f"  {label}: both None OK"
    if a is None or b is None:
        return f"  {label}: MISMATCH plot={a} dash={b}"
    diff = abs(float(a) - float(b))
    ok = diff <= tol
    mark = "OK" if ok else "MISMATCH"
    return f"  {label}: {mark} plot={float(a):.2f} dash={float(b):.2f} Δ={diff:.2f}"


def main():
    days = ["2026-07-28", "2026-07-30", "2026-08-03"]
    print(f"MIN_SPEND_FOR_ROAS={MIN_SPEND_FOR_ROAS}")
    print(f"USE_API_ONLY={os.getenv('USE_API_ONLY')}")
    print(f"CHANNEL_FROM_ATTRIBUTION={os.getenv('CHANNEL_FROM_ATTRIBUTION')}")
    mismatches = 0
    for d in days:
        print(f"\n=== {d} ===")
        dash = dashboard_day(d)
        plot = plot_channel_day(d)
        asis = asis_day(d)
        amz_prep = amazon_roas_prep(d)

        print("-- as-is Net Profit vs dashboard net_profit --")
        line = cmp("net_profit", asis.get("net_profit"), dash.get("net_profit_total"), tol=5)
        print(line)
        if "MISMATCH" in line:
            mismatches += 1
            print(
                f"    asis rev/cogs/spend="
                f"{asis.get('revenue')}/{asis.get('cogs')}/{asis.get('total_ad_spend')} | "
                f"dash net_sales/ad_spend/roas="
                f"{dash.get('net_sales_total')}/{dash.get('ad_spend_total')}/{dash.get('net_roas_total')}"
            )

        for ch in ("meta", "google", "amazon"):
            print(f"-- {ch} channel plot vs dashboard --")
            p = plot.get(ch) or {}
            dd = dash.get(ch) or {}
            for metric in ("spend", "revenue", "cogs", "net_profit", "net_roas"):
                line = cmp(metric, p.get(metric), dd.get(metric), tol=5 if metric != "net_roas" else 0.05)
                print(line)
                if "MISMATCH" in line:
                    mismatches += 1

        print("-- Amazon ROAS prep (gross) vs dashboard gross_roas --")
        line = cmp("amazon_gross_roas", amz_prep, (dash.get("amazon") or {}).get("gross_roas"), tol=0.05)
        print(line)
        if "MISMATCH" in line:
            mismatches += 1
        print(
            f"  note: amazon spend={ (dash.get('amazon') or {}).get('spend') } "
            f"net_roas={ (dash.get('amazon') or {}).get('net_roas') }"
        )

    print(f"\nTOTAL MISMATCHES: {mismatches}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
