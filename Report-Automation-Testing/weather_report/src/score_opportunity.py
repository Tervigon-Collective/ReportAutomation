"""Phase 5: opportunity scoring.

Joins the Phase 4 city-level weather classification with Shopify sales windows
(from ``city_sales_daily.csv``) and city tier (``city_master.csv``), then scores
every active city:

    opportunity_score = weather_score      * 0.50
                      + sales_score        * 0.25
                      + market_size_score  * 0.15
                      + trend_score        * 0.10

Sub-scores:
  * weather_score      additive from rain-now / 72h prob / 3d rainfall / 3d
                       precip-hours (capped at 100). Drives opportunity.
  * sales_score        0.5*norm(orders_30d) + 0.3*norm(revenue_30d)
                       + 0.2*norm(sales_momentum), min-max normalized 0-100.
  * market_size_score  by city_tier (Tier1=100, Tier2=75, Tier3=50, else 30).
  * trend_score        normalized sales momentum (last-7d rate vs 30d rate).

All weights / thresholds come from ``config/scoring_rules.json``. Cities with no
sales still score (weather + market size) -- these are the *opportunity* cities.

Writes ``data/weather/scored/{report_date}.csv`` (consumed by Phase 6).

Usage (from weather_report/):
    py src/score_opportunity.py
    py src/score_opportunity.py --report-date 2026-06-26
    py src/score_opportunity.py --weather-date 2026-06-26   # classified file to use
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("score_opportunity")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
MASTER_PATH = DATA_DIR / "city_master.csv"
SALES_PATH = DATA_DIR / "city_sales_daily.csv"
CLASSIFIED_DIR = DATA_DIR / "weather" / "classified"
SCORED_DIR = DATA_DIR / "scored"
SCORING_RULES_PATH = DATA_DIR / "config" / "scoring_rules.json"

IST = timezone(timedelta(hours=5, minutes=30))

# Momentum ratios above this are clipped before normalization (tiny baselines
# otherwise produce extreme outliers that flatten everyone else's score).
MOMENTUM_CLIP = 3.0

SCORED_COLUMNS = [
    "report_date", "city_id", "city", "state", "region", "city_tier",
    "weather_status", "weather_bucket", "rain_now",
    "max_rain_probability_next_72h", "rainfall_next_3d_mm",
    "precipitation_hours_next_3d",
    "orders_7d", "orders_30d", "orders_90d", "revenue_30d", "sales_momentum",
    "weather_score", "sales_score", "market_size_score", "trend_score",
    "opportunity_score", "existing_sales_city", "new_opportunity_city",
    "created_at",
]


def load_rules(path: Path = SCORING_RULES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _num(v, default=0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _tiered_points(value: float, rules: list[dict]) -> float:
    """Apply the first matching {gte, points} rule (rules ordered high->low)."""
    for r in rules:
        if value >= r["gte"]:
            return float(r["points"])
    return 0.0


def weather_score(row, cfg: dict) -> float:
    score = 0.0
    if bool(row["rain_now"]):
        score += cfg.get("rain_now", 30)
    score += _tiered_points(_num(row["max_rain_probability_next_72h"]),
                            cfg.get("probability_next_72h", []))
    score += _tiered_points(_num(row["rainfall_next_3d_mm"]),
                            cfg.get("rainfall_next_3d_mm", []))
    score += _tiered_points(_num(row["precipitation_hours_next_3d"]),
                            cfg.get("precipitation_hours_next_3d", []))
    return min(score, cfg.get("cap", 100))


def _minmax(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").fillna(0.0)
    lo, hi = s.min(), s.max()
    if hi <= lo:
        return pd.Series([0.0] * len(s), index=s.index)
    return (s - lo) / (hi - lo) * 100.0


def _window_sum(g: pd.DataFrame, col: str, start: str, end: str) -> float:
    mask = (g["date"] >= start) & (g["date"] <= end)
    return float(g.loc[mask, col].sum())


def compute_sales_windows(sales: pd.DataFrame, report_date: str) -> pd.DataFrame:
    rd = datetime.strptime(report_date, "%Y-%m-%d")
    d7 = (rd - timedelta(days=6)).strftime("%Y-%m-%d")
    d30 = (rd - timedelta(days=29)).strftime("%Y-%m-%d")
    d90 = (rd - timedelta(days=89)).strftime("%Y-%m-%d")
    # previous 7-day block, immediately before the last 7 days
    p7_start = (rd - timedelta(days=13)).strftime("%Y-%m-%d")
    p7_end = (rd - timedelta(days=7)).strftime("%Y-%m-%d")

    if sales.empty:
        return pd.DataFrame(columns=["city_id", "orders_7d", "orders_30d",
                                     "orders_90d", "revenue_30d", "sales_momentum"])

    sales = sales[sales["date"] <= report_date]
    rows = []
    for cid, g in sales.groupby("city_id"):
        o7 = _window_sum(g, "orders", d7, report_date)
        o30 = _window_sum(g, "orders", d30, report_date)
        o90 = _window_sum(g, "orders", d90, report_date)
        rev30 = _window_sum(g, "revenue", d30, report_date)
        prev7 = _window_sum(g, "orders", p7_start, p7_end)
        # Momentum = last-7d daily rate vs prior-7d daily rate, as % change.
        rate_now = o7 / 7.0
        rate_prev = prev7 / 7.0
        if rate_prev > 0:
            momentum = (rate_now / rate_prev - 1.0) * 100.0
        elif rate_now > 0:
            momentum = 100.0  # new/returning demand with no prior baseline
        else:
            momentum = 0.0
        rows.append({
            "city_id": int(cid), "orders_7d": o7, "orders_30d": o30,
            "orders_90d": o90, "revenue_30d": round(rev30, 2),
            "sales_momentum": round(momentum, 1),
        })
    return pd.DataFrame(rows)


def run(report_date: str, weather_date: str) -> None:
    rules = load_rules()
    weights = rules.get("opportunity_weights", {})
    w_weather = weights.get("weather_score", 0.50)
    w_sales = weights.get("sales_score", 0.25)
    w_market = weights.get("market_size_score", 0.15)
    w_trend = weights.get("trend_score", 0.10)
    ws_cfg = rules.get("weather_score", {})
    sales_cfg = rules.get("sales_score", {})
    market_cfg = rules.get("market_size_score", {})

    classified_path = CLASSIFIED_DIR / f"{weather_date}.csv"
    if not classified_path.exists():
        raise SystemExit(f"No classified weather for {weather_date}: {classified_path}\n"
                         f"Run classify_weather.py first.")
    weather = pd.read_csv(classified_path)
    master = pd.read_csv(MASTER_PATH)
    sales = pd.read_csv(SALES_PATH) if SALES_PATH.exists() else pd.DataFrame()

    # Base = all classified (active) cities, enriched with tier/state/region.
    base = weather.merge(
        master[["city_id", "state", "region", "city_tier"]], on="city_id", how="left"
    )
    windows = compute_sales_windows(sales, report_date)
    base = base.merge(windows, on="city_id", how="left")
    for col in ("orders_7d", "orders_30d", "orders_90d", "revenue_30d", "sales_momentum"):
        base[col] = pd.to_numeric(base.get(col), errors="coerce").fillna(0.0)

    # --- sub-scores ---
    base["weather_score"] = base.apply(lambda r: weather_score(r, ws_cfg), axis=1).round(1)
    base["market_size_score"] = base["city_tier"].map(
        lambda t: market_cfg.get(str(t), market_cfg.get("default", 30))
    ).fillna(market_cfg.get("default", 30)).astype(float)

    norm_orders = _minmax(base["orders_30d"])
    norm_revenue = _minmax(base["revenue_30d"])
    momentum_clipped = base["sales_momentum"].clip(lower=-100, upper=MOMENTUM_CLIP * 100)
    norm_momentum = _minmax(momentum_clipped)
    base["sales_score"] = (
        sales_cfg.get("normalized_orders_30d", 0.5) * norm_orders
        + sales_cfg.get("normalized_revenue_30d", 0.3) * norm_revenue
        + sales_cfg.get("normalized_sales_momentum", 0.2) * norm_momentum
    ).round(1)
    base["trend_score"] = norm_momentum.round(1)

    base["opportunity_score"] = (
        w_weather * base["weather_score"]
        + w_sales * base["sales_score"]
        + w_market * base["market_size_score"]
        + w_trend * base["trend_score"]
    ).round(1)

    base["existing_sales_city"] = base["orders_90d"] > 0
    base["new_opportunity_city"] = base["orders_90d"] <= 0
    base["report_date"] = report_date
    base["city"] = base["canonical_city"]
    base["created_at"] = datetime.now(IST).isoformat(timespec="seconds")

    scored = base[SCORED_COLUMNS].sort_values(
        "opportunity_score", ascending=False
    ).reset_index(drop=True)

    SCORED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCORED_DIR / f"{report_date}.csv"
    scored.to_csv(out_path, index=False)
    logger.info("Scored %d cities -> %s", len(scored), out_path)
    logger.info("Top 10 by opportunity_score:\n%s",
                scored.head(10)[["city", "city_tier", "weather_status",
                                 "orders_30d", "weather_score", "sales_score",
                                 "opportunity_score"]].to_string(index=False))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report-date", default=None, help="Report date YYYY-MM-DD (default today IST).")
    p.add_argument("--weather-date", default=None,
                   help="Classified weather date to use (default = report-date).")
    args = p.parse_args()
    report_date = args.report_date or datetime.now(IST).strftime("%Y-%m-%d")
    weather_date = args.weather_date or report_date
    run(report_date, weather_date)


if __name__ == "__main__":
    main()
