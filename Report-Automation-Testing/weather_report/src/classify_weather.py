"""Phase 4: weather bucket classification.

Turns the raw Open-Meteo payload (Phase 3) into actionable, city-level weather
signals + a primary ``weather_bucket``, combining three time windows:

  * current observation   -> rain_now
  * next 72h (hourly)      -> max_rain_probability_next_72h
  * next 3 days (daily)    -> rainfall / precipitation / precip-hours sums

Buckets (priority order; thresholds from ``config/scoring_rules.json``):

  1. active_rain             current precipitation > 0 OR current rain > 0
  2. heavy_rain_watch        rainfall_next_3d >= 20mm OR precip_next_3d >= 25mm
  3. high_rain_probability   max precip probability next 72h >= 70
  4. emerging_rain           not raining now AND max prob next 72h >= 50
  5. low_weather_opportunity max prob next 72h < 40 AND rainfall_next_3d < 5mm
  6. moderate                everything else

Writes:
  - weather/classified/{run_date}.csv   (city-level signals + bucket)
  - backfills the per-day ``weather_bucket`` column in
    weather/forecast/{run_date}.csv

Usage (from weather_report/):
    py src/classify_weather.py
    py src/classify_weather.py --run-date 2026-06-26
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("classify_weather")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
CURRENT_DIR = DATA_DIR / "weather" / "current"
FORECAST_DIR = DATA_DIR / "weather" / "forecast"
CLASSIFIED_DIR = DATA_DIR / "weather" / "classified"
SCORING_RULES_PATH = DATA_DIR / "config" / "scoring_rules.json"

IST = timezone(timedelta(hours=5, minutes=30))

CLASSIFIED_COLUMNS = [
    "run_date", "city_id", "canonical_city",
    "current_temp_c", "current_humidity", "current_precipitation_mm",
    "current_rain_mm", "current_weather_code", "rain_now",
    "max_rain_probability_next_72h", "rainfall_next_3d_mm",
    "precipitation_next_3d_mm", "precipitation_hours_next_3d",
    "weather_bucket", "weather_status", "created_at",
]

BUCKET_STATUS = {
    "active_rain": "Active Rain",
    "heavy_rain_watch": "Heavy Rain Watch",
    "high_rain_probability": "High Rain Probability",
    "emerging_rain": "Emerging Rain",
    "low_weather_opportunity": "Low Weather Opportunity",
    "moderate": "Moderate",
    "no_data": "No Data",
}


@dataclass
class Thresholds:
    high_prob_gte: float = 70
    emerging_prob_gte: float = 50
    heavy_rainfall_3d_gte: float = 20
    heavy_precip_3d_gte: float = 25
    low_prob_lt: float = 40
    low_rainfall_3d_lt: float = 5


def load_thresholds(path: Path = SCORING_RULES_PATH) -> Thresholds:
    t = Thresholds()
    try:
        wb = json.loads(path.read_text(encoding="utf-8")).get("weather_buckets", {})
        t.high_prob_gte = wb.get("high_rain_probability", {}).get("max_prob_next_72h_gte", t.high_prob_gte)
        t.emerging_prob_gte = wb.get("emerging_rain", {}).get("max_prob_next_72h_gte", t.emerging_prob_gte)
        t.heavy_rainfall_3d_gte = wb.get("heavy_rain_watch", {}).get("rainfall_next_3d_mm_gte", t.heavy_rainfall_3d_gte)
        t.heavy_precip_3d_gte = wb.get("heavy_rain_watch", {}).get("precip_next_3d_mm_gte", t.heavy_precip_3d_gte)
        t.low_prob_lt = wb.get("low_weather_opportunity", {}).get("max_prob_next_72h_lt", t.low_prob_lt)
        t.low_rainfall_3d_lt = wb.get("low_weather_opportunity", {}).get("rainfall_next_3d_mm_lt", t.low_rainfall_3d_lt)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Falling back to default thresholds: %s", exc)
    return t


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _max_prob_next_72h(hourly: dict, now_iso: str | None) -> float:
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    if not times or not probs:
        return 0.0
    # Floor "now" to the hour; ISO strings compare chronologically.
    floor = (now_iso[:13] + ":00") if now_iso and len(now_iso) >= 13 else times[0]
    vals = [_num(p) for t, p in zip(times, probs) if t >= floor][:72]
    if not vals:  # current time past the series start edge -> use first 72
        vals = [_num(p) for p in probs[:72]]
    return max(vals) if vals else 0.0


def _classify(rain_now: bool, max_prob: float, rainfall_3d: float,
              precip_3d: float, t: Thresholds) -> str:
    if rain_now:
        return "active_rain"
    if rainfall_3d >= t.heavy_rainfall_3d_gte or precip_3d >= t.heavy_precip_3d_gte:
        return "heavy_rain_watch"
    if max_prob >= t.high_prob_gte:
        return "high_rain_probability"
    if max_prob >= t.emerging_prob_gte:
        return "emerging_rain"
    if max_prob < t.low_prob_lt and rainfall_3d < t.low_rainfall_3d_lt:
        return "low_weather_opportunity"
    return "moderate"


def _classify_day(prob_max: float, rain_sum: float, precip_sum: float,
                  t: Thresholds) -> str:
    """Per-forecast-day bucket (no current obs, so no active_rain)."""
    if rain_sum >= t.heavy_rainfall_3d_gte or precip_sum >= t.heavy_precip_3d_gte:
        return "heavy_rain_watch"
    if prob_max >= t.high_prob_gte:
        return "high_rain_probability"
    if prob_max >= t.emerging_prob_gte:
        return "emerging_rain"
    if prob_max < t.low_prob_lt and precip_sum < t.low_rainfall_3d_lt:
        return "low_weather_opportunity"
    return "moderate"


def classify_cities(payload: dict, t: Thresholds, run_date: str,
                    created_at: str) -> pd.DataFrame:
    rows = []
    for cid, c in payload.get("cities", {}).items():
        raw = c.get("raw_response") or {}
        current = c.get("current") or {}
        daily = raw.get("daily") or {}
        hourly = raw.get("hourly") or {}

        if c.get("error") or not daily:
            rows.append({
                "run_date": run_date, "city_id": int(cid),
                "canonical_city": c.get("canonical_city"),
                "current_temp_c": None, "current_humidity": None,
                "current_precipitation_mm": None, "current_rain_mm": None,
                "current_weather_code": None, "rain_now": False,
                "max_rain_probability_next_72h": None, "rainfall_next_3d_mm": None,
                "precipitation_next_3d_mm": None, "precipitation_hours_next_3d": None,
                "weather_bucket": "no_data", "weather_status": BUCKET_STATUS["no_data"],
                "created_at": created_at,
            })
            continue

        cur_precip = _num(current.get("precipitation"))
        cur_rain = _num(current.get("rain"))
        rain_now = cur_precip > 0 or cur_rain > 0

        max_prob = round(_max_prob_next_72h(hourly, current.get("time")), 1)

        rain_sum = daily.get("rain_sum") or []
        precip_sum = daily.get("precipitation_sum") or []
        precip_hours = daily.get("precipitation_hours") or []
        rainfall_3d = round(sum(_num(x) for x in rain_sum[:3]), 2)
        precip_3d = round(sum(_num(x) for x in precip_sum[:3]), 2)
        hours_3d = round(sum(_num(x) for x in precip_hours[:3]), 1)

        bucket = _classify(rain_now, max_prob, rainfall_3d, precip_3d, t)
        rows.append({
            "run_date": run_date, "city_id": int(cid),
            "canonical_city": c.get("canonical_city"),
            "current_temp_c": _num(current.get("temperature_2m"), None),
            "current_humidity": current.get("relative_humidity_2m"),
            "current_precipitation_mm": cur_precip,
            "current_rain_mm": cur_rain,
            "current_weather_code": current.get("weather_code"),
            "rain_now": rain_now,
            "max_rain_probability_next_72h": max_prob,
            "rainfall_next_3d_mm": rainfall_3d,
            "precipitation_next_3d_mm": precip_3d,
            "precipitation_hours_next_3d": hours_3d,
            "weather_bucket": bucket,
            "weather_status": BUCKET_STATUS[bucket],
            "created_at": created_at,
        })
    df = pd.DataFrame(rows, columns=CLASSIFIED_COLUMNS)
    return df.sort_values("city_id").reset_index(drop=True)


def backfill_forecast_buckets(run_date: str, t: Thresholds) -> int:
    """Fill the per-day weather_bucket column in the forecast CSV."""
    path = FORECAST_DIR / f"{run_date}.csv"
    if not path.exists():
        logger.warning("Forecast CSV not found for backfill: %s", path)
        return 0
    df = pd.read_csv(path)
    if df.empty:
        return 0
    df["weather_bucket"] = [
        _classify_day(_num(p), _num(r), _num(s), t)
        for p, r, s in zip(df["precipitation_probability_max"],
                           df["rain_sum_mm"], df["precipitation_sum_mm"])
    ]
    df.to_csv(path, index=False)
    return len(df)


def run(run_date: str) -> None:
    current_path = CURRENT_DIR / f"{run_date}.json"
    if not current_path.exists():
        raise SystemExit(f"No forecast payload for {run_date}: {current_path}\n"
                         f"Run fetch_forecast.py first.")

    payload = json.loads(current_path.read_text(encoding="utf-8"))
    t = load_thresholds()
    created_at = datetime.now(IST).isoformat(timespec="seconds")

    classified = classify_cities(payload, t, run_date, created_at)
    CLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLASSIFIED_DIR / f"{run_date}.csv"
    classified.to_csv(out_path, index=False)
    logger.info("Wrote %d city classifications -> %s", len(classified), out_path)

    dist = classified["weather_bucket"].value_counts().to_dict()
    logger.info("Bucket distribution: %s", dist)

    n = backfill_forecast_buckets(run_date, t)
    logger.info("Backfilled weather_bucket on %d forecast rows.", n)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-date", default=None, help="Run date YYYY-MM-DD (default today IST).")
    args = p.parse_args()
    run_date = args.run_date or datetime.now(IST).strftime("%Y-%m-%d")
    run(run_date)


if __name__ == "__main__":
    main()
