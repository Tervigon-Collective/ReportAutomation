"""Prepare weather report assets for the daily marketing email.

Always runs the full live pipeline on each call:
  Shopify sales (Postgres) -> Open-Meteo forecast -> classify -> score -> report.

Temp graph/CSV copies are deleted by the email sender after dispatch.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

SRC = Path(__file__).resolve().parent
MODULE_ROOT = SRC.parent
PROJECT_ROOT = MODULE_ROOT.parent

sys.path.insert(0, str(SRC))

import run_report  # noqa: E402

logger = logging.getLogger("weather_email_assets")
IST = timezone(timedelta(hours=5, minutes=30))


def _load_project_env() -> None:
    try:
        from dotenv import load_dotenv
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        else:
            load_dotenv()
    except ImportError:
        pass


def _live_report_date(report_date: str | None) -> str:
    """Use explicit date or today in IST (always fresh at send time)."""
    if report_date:
        return report_date
    return datetime.now(IST).strftime("%Y-%m-%d")


def _use_llm_from_env(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.getenv("WEATHER_REPORT_USE_LLM", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _run_live_pipeline(
    report_date: str,
    *,
    days: int = 90,
    use_llm: bool = False,
    llm_min_confidence: int = 80,
    top: int = 15,
    combined_path: Path | None = None,
    csv_path: Path | None = None,
) -> dict:
    """Refresh every data source and rebuild the report. No cache shortcuts."""
    logger.info(
        "Live weather pipeline | report_date=%s | sales_window=%dd | use_llm=%s",
        report_date, days, use_llm,
    )
    return run_report.run(
        report_date,
        days,
        use_llm,
        llm_min_confidence,
        skip_sales=False,
        skip_forecast=False,
        skip_classify=False,
        plots=False,
        top=top,
        continue_on_error=True,
        combined_path=combined_path,
        csv_path=csv_path,
    )


def build_weather_email_bundle(
    report_date: str | None = None,
    output_dir: str | Path | None = None,
    *,
    top: int = 15,
    days: int = 90,
    use_llm: bool | None = None,
) -> dict | None:
    """
    Fetch live data, build the report, and stage temp email assets.

    Returns:
        {
            "combined_plot": str,
            "csv_path": str,
            "report_date": str,
        }
    or None when the report cannot be produced.
    """
    _load_project_env()
    report_date = _live_report_date(report_date)
    out_dir = Path(output_dir or (Path(os.getenv("TEMP", "/tmp")) / "weather_email"))
    out_dir.mkdir(parents=True, exist_ok=True)

    combined = out_dir / f"campaign_opportunity_combined_{report_date}.png"
    attach_csv = out_dir / f"campaign_opportunity_{report_date}.csv"
    result = _run_live_pipeline(
        report_date,
        days=days,
        use_llm=_use_llm_from_env(use_llm),
        top=top,
        combined_path=combined,
        csv_path=attach_csv,
    )

    if not result.get("success") or not attach_csv.exists():
        logger.warning("Weather report: live pipeline did not produce CSV for %s", report_date)
        return None

    if not combined.exists():
        logger.warning("Weather combined canvas was not created")
        return None

    report_df = pd.read_csv(attach_csv)

    report_time = ""
    weather_fetched_at = ""
    if not report_df.empty:
        if "report_time" in report_df.columns:
            report_time = str(report_df["report_time"].iloc[0] or "")
        if "weather_fetched_at" in report_df.columns:
            weather_fetched_at = str(report_df["weather_fetched_at"].iloc[0] or "")

    return {
        "combined_plot": str(combined),
        "csv_path": str(attach_csv),
        "report_date": report_date,
        "report_time": report_time,
        "weather_fetched_at": weather_fetched_at,
    }
