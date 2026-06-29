"""Phase 3 orchestrator: fetch Open-Meteo forecasts for active cities.

Writes:
  - data/weather/weather/current/{run_date}.json  (raw payload keyed by city_id)
  - data/weather/weather/forecast/{run_date}.csv   (parsed daily forecast rows)

The ``weather_bucket`` column is present but left blank here; bucket
classification is Phase 4 (city-level, combining current + next-72h signals).

Usage (from weather_report/):
    py src/fetch_forecast.py
    py src/fetch_forecast.py --run-date 2026-06-26
    py src/fetch_forecast.py --limit 5        # smoke test first 5 cities
    py src/fetch_forecast.py --all            # include is_active=false cities
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

try:
    from . import weather_source
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import weather_source  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("fetch_forecast")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
MASTER_PATH = DATA_DIR / "city_master.csv"
CURRENT_DIR = DATA_DIR / "weather" / "current"
FORECAST_DIR = DATA_DIR / "weather" / "forecast"

IST = timezone(timedelta(hours=5, minutes=30))

FORECAST_COLUMNS = [
    "run_date", "forecast_date", "city_id", "canonical_city",
    "precipitation_probability_max", "precipitation_sum_mm", "rain_sum_mm",
    "precipitation_hours", "weather_code", "weather_bucket", "created_at",
]


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes", "y", "t"}


def load_active_cities(all_cities: bool, limit: int | None) -> list[dict]:
    df = pd.read_csv(MASTER_PATH)
    if not all_cities and "is_active" in df.columns:
        df = df[df["is_active"].map(_truthy)]
    df = df.dropna(subset=["latitude", "longitude"])
    if limit:
        df = df.head(limit)
    return df[["city_id", "canonical_city", "latitude", "longitude"]].to_dict("records")


def write_current_json(forecasts, run_date: str, fetched_at: str) -> Path:
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"run_date": run_date, "fetched_at": fetched_at, "cities": {}}
    for fc in forecasts:
        payload["cities"][str(fc.city_id)] = {
            "canonical_city": fc.canonical_city,
            "latitude": fc.latitude,
            "longitude": fc.longitude,
            "current": fc.current,
            "error": fc.error,
            "raw_response": fc.raw,
        }
    path = CURRENT_DIR / f"{run_date}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def build_forecast_rows(forecasts, run_date: str, created_at: str) -> pd.DataFrame:
    rows = []
    for fc in forecasts:
        for day in fc.daily:
            rows.append({
                "run_date": run_date,
                "forecast_date": day["forecast_date"],
                "city_id": fc.city_id,
                "canonical_city": fc.canonical_city,
                "precipitation_probability_max": day["precipitation_probability_max"],
                "precipitation_sum_mm": day["precipitation_sum_mm"],
                "rain_sum_mm": day["rain_sum_mm"],
                "precipitation_hours": day["precipitation_hours"],
                "weather_code": day["weather_code"],
                "weather_bucket": "",          # Phase 4 fills this
                "created_at": created_at,
            })
    return pd.DataFrame(rows, columns=FORECAST_COLUMNS)


def run(run_date: str, all_cities: bool, limit: int | None, batch_size: int) -> None:
    cities = load_active_cities(all_cities, limit)
    if not cities:
        logger.warning("No cities to fetch.")
        return
    logger.info("Fetching forecasts for %d cities (run_date=%s)...", len(cities), run_date)

    config = weather_source.load_config()
    forecasts = weather_source.fetch_forecasts(cities, config, batch_size=batch_size)

    errors = [fc for fc in forecasts if fc.error]
    fetched_at = datetime.now(IST).isoformat(timespec="seconds")

    json_path = write_current_json(forecasts, run_date, fetched_at)
    logger.info("Wrote raw current payload -> %s", json_path)

    df = build_forecast_rows(forecasts, run_date, fetched_at)
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = FORECAST_DIR / f"{run_date}.csv"
    df.to_csv(csv_path, index=False)
    logger.info("Wrote %d forecast rows (%d cities x days) -> %s",
                len(df), df["city_id"].nunique(), csv_path)
    if errors:
        logger.warning("%d cities had fetch errors (see error field in JSON).", len(errors))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-date", default=None, help="Run date YYYY-MM-DD (default today IST).")
    p.add_argument("--all", action="store_true", help="Include is_active=false cities.")
    p.add_argument("--limit", type=int, default=None, help="Only fetch first N cities (smoke test).")
    p.add_argument("--batch-size", type=int, default=weather_source.DEFAULT_BATCH,
                   help="Cities per Open-Meteo call (default 100).")
    args = p.parse_args()

    run_date = args.run_date or datetime.now(IST).strftime("%Y-%m-%d")
    run(run_date, args.all, args.limit, args.batch_size)


if __name__ == "__main__":
    main()
