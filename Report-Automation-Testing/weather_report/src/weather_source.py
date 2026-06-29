"""Phase 3 data source: Open-Meteo forecast fetcher.

Batches active cities into multi-coordinate Open-Meteo forecast calls (the API
accepts comma-separated ``latitude``/``longitude`` and returns one result per
location), with timeout + retry handling. Returns per-city parsed daily rows
plus the full raw response for audit.

Config is read from ``data/weather/config/open_meteo.json``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

logger = logging.getLogger("weather_source")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
CONFIG_PATH = DATA_DIR / "config" / "open_meteo.json"

DEFAULT_BATCH = 100


@dataclass
class CityForecast:
    city_id: int
    canonical_city: str
    latitude: float
    longitude: float
    current: dict = field(default_factory=dict)
    daily: list[dict] = field(default_factory=list)  # one dict per forecast day
    raw: dict = field(default_factory=dict)
    error: str | None = None


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _request_batch(base_url: str, params: dict, req_cfg: dict) -> list | dict:
    timeout = req_cfg.get("timeout_seconds", 30)
    retries = req_cfg.get("max_retries", 3)
    backoff = req_cfg.get("retry_backoff_seconds", 2)
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(base_url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - want to retry any transient error
            last_exc = exc
            logger.warning("Open-Meteo request attempt %d/%d failed: %s",
                           attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Open-Meteo request failed after {retries} attempts: {last_exc}")


def _parse_daily(payload: dict) -> list[dict]:
    """Flatten Open-Meteo's column-oriented ``daily`` block into row dicts."""
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    rows = []
    for i, d in enumerate(dates):
        def col(name):
            arr = daily.get(name) or []
            return arr[i] if i < len(arr) else None
        rows.append({
            "forecast_date": d,
            "precipitation_probability_max": col("precipitation_probability_max"),
            "precipitation_sum_mm": col("precipitation_sum"),
            "rain_sum_mm": col("rain_sum"),
            "precipitation_hours": col("precipitation_hours"),
            "weather_code": col("weather_code"),
        })
    return rows


def fetch_forecasts(cities: list[dict], config: dict | None = None,
                    batch_size: int = DEFAULT_BATCH) -> list[CityForecast]:
    """Fetch forecasts for ``cities`` (each: city_id, canonical_city, latitude,
    longitude). Returns a CityForecast per input city (errors captured inline).
    """
    config = config or load_config()
    base_url = config["base_url"]
    params_cfg = dict(config.get("params", {}))
    req_cfg = config.get("request", {})

    out: list[CityForecast] = []
    for start in range(0, len(cities), batch_size):
        chunk = cities[start:start + batch_size]
        lats = ",".join(str(c["latitude"]) for c in chunk)
        lons = ",".join(str(c["longitude"]) for c in chunk)
        params = dict(params_cfg)
        params["latitude"] = lats
        params["longitude"] = lons

        try:
            data = _request_batch(base_url, params, req_cfg)
        except Exception as exc:  # whole batch failed
            logger.error("Batch %d-%d failed: %s", start, start + len(chunk) - 1, exc)
            for c in chunk:
                out.append(CityForecast(int(c["city_id"]), c["canonical_city"],
                                        float(c["latitude"]), float(c["longitude"]),
                                        error=str(exc)))
            continue

        # Single-coordinate calls return a dict; multi returns a list.
        results = data if isinstance(data, list) else [data]
        if len(results) != len(chunk):
            logger.warning("Batch %d: expected %d results, got %d",
                           start, len(chunk), len(results))
        for c, payload in zip(chunk, results):
            out.append(CityForecast(
                city_id=int(c["city_id"]),
                canonical_city=c["canonical_city"],
                latitude=float(c["latitude"]),
                longitude=float(c["longitude"]),
                current=payload.get("current") or {},
                daily=_parse_daily(payload),
                raw=payload,
            ))
        logger.info("Fetched batch %d-%d (%d cities)",
                    start, start + len(chunk) - 1, len(chunk))
    return out
