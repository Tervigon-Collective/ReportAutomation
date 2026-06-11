"""
GST adjustment helpers — convert gross revenue to net (ex-GST) for reporting.
Applies to revenue fields only; COGS and ad spend are unchanged.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Optional, Union

import pandas as pd

# Default 18% GST (India); override via GST_RATE env (e.g. 0.18)
_DEFAULT_GST_RATE = float(os.getenv("GST_RATE", "0.18"))


def _gst_divisor() -> float:
    rate = float(os.getenv("GST_RATE", str(_DEFAULT_GST_RATE)))
    return 1.0 + rate if rate > 0 else 1.0


def apply_net_revenue(amount: Union[int, float]) -> float:
    """Return gross revenue excluding GST."""
    try:
        gross = float(amount or 0)
    except (TypeError, ValueError):
        return 0.0
    if gross == 0:
        return 0.0
    return gross / _gst_divisor()


def apply_net_revenue_column(series: pd.Series) -> pd.Series:
    """Apply GST net adjustment to a pandas revenue column."""
    if series is None or len(series) == 0:
        return series
    divisor = _gst_divisor()
    return pd.to_numeric(series, errors="coerce").fillna(0) / divisor


def _adjust_day_breakdown(day: dict) -> dict:
    out = dict(day)
    if "revenue" in out:
        net_rev = apply_net_revenue(out["revenue"])
        out["revenue"] = net_rev
        cogs = float(out.get("cogs", 0) or 0)
        ad_total = 0.0
        ad_spend = out.get("adSpend")
        if isinstance(ad_spend, dict):
            ad_total = float(ad_spend.get("total", 0) or 0)
        elif ad_spend is not None:
            ad_total = float(ad_spend or 0)
        out["netProfit"] = net_rev - cogs - ad_total
    return out


def adjust_net_profit_single_day_payload(payload: Optional[dict]) -> dict:
    """Apply GST net revenue to /net_profit_single_day API response."""
    if not payload:
        return payload or {}

    out = copy.deepcopy(payload)
    data = out.get("data")
    if not isinstance(data, dict):
        return out

    breakdowns = data.get("dailyBreakdowns")
    if isinstance(breakdowns, list):
        data["dailyBreakdowns"] = [_adjust_day_breakdown(d) for d in breakdowns if isinstance(d, dict)]

    totals = data.get("totals")
    if isinstance(totals, dict):
        totals = dict(totals)
        if "revenue" in totals:
            net_rev = apply_net_revenue(totals["revenue"])
            totals["revenue"] = net_rev
            cogs = float(totals.get("cogs", 0) or 0)
            ad_total = float(totals.get("adSpend", 0) or 0)
            if isinstance(totals.get("adSpend"), dict):
                ad_total = float(totals["adSpend"].get("total", 0) or 0)
            totals["netProfit"] = net_rev - cogs - ad_total
        data["totals"] = totals

    out["data"] = data
    return out
