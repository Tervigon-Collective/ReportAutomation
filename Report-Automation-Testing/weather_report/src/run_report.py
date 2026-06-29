"""One-command daily pipeline: sales -> forecast -> classify -> score -> report.

Runs every phase in-process, in order, for a single ``--report-date``:

  1. aggregate_sales   refresh Shopify city sales + resolve cities (DB + LLM)
  2. fetch_forecast    pull Open-Meteo forecasts for all active cities
  3. classify_weather  assign city-level weather buckets
  4. score_opportunity compute opportunity scores
  5. build_report      write the ranked report CSV + heatmap/geo PNGs

Each step can be skipped to reuse existing artifacts (handy when iterating on
scoring/reporting without re-hitting the DB or weather API).

Usage (from weather_report/):
    py src/run_report.py                      # full run, today (IST)
    py src/run_report.py --use-llm            # include LLM city bucketing
    py src/run_report.py --report-date 2026-06-26
    py src/run_report.py --skip-sales --skip-forecast   # reuse cached data
    py src/run_report.py --days 30 --no-plots
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import aggregate_sales      # noqa: E402
import build_report         # noqa: E402
import classify_weather     # noqa: E402
import fetch_forecast       # noqa: E402
import score_opportunity    # noqa: E402
import weather_source       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_report")

IST = timezone(timedelta(hours=5, minutes=30))


def _step(name: str, fn, *, skip: bool, continue_on_error: bool) -> bool:
    """Run one pipeline step with a banner + timing. Returns success."""
    if skip:
        logger.info("=== %s - SKIPPED ===", name)
        return True
    logger.info("=== %s ===", name)
    t0 = time.time()
    try:
        fn()
        logger.info("--- %s done in %.1fs ---", name, time.time() - t0)
        return True
    except SystemExit as exc:           # raised by steps on missing inputs
        logger.error("%s aborted: %s", name, exc)
    except Exception as exc:            # noqa: BLE001
        logger.exception("%s failed: %s", name, exc)
    if not continue_on_error:
        raise SystemExit(f"Pipeline stopped at: {name}")
    return False


def run(report_date: str, days: int, use_llm: bool, llm_min_confidence: int,
        skip_sales: bool, skip_forecast: bool, skip_classify: bool,
        plots: bool, top: int, continue_on_error: bool,
        combined_path: Path | None = None, csv_path: Path | None = None) -> dict:
    start = (datetime.strptime(report_date, "%Y-%m-%d")
             - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    logger.info("Weather Campaign Opportunity pipeline | report_date=%s | window=%s..%s",
                report_date, start, report_date)
    overall = time.time()

    _step("1/5 aggregate_sales",
          lambda: aggregate_sales.run(start, report_date, None, False,
                                      use_llm=use_llm,
                                      llm_min_confidence=llm_min_confidence),
          skip=skip_sales, continue_on_error=continue_on_error)

    _step("2/5 fetch_forecast",
          lambda: fetch_forecast.run(report_date, False, None, weather_source.DEFAULT_BATCH),
          skip=skip_forecast, continue_on_error=continue_on_error)

    _step("3/5 classify_weather",
          lambda: classify_weather.run(report_date),
          skip=skip_classify, continue_on_error=continue_on_error)

    # Scoring + report are cheap and always run (they are the point).
    _step("4/5 score_opportunity",
          lambda: score_opportunity.run(report_date, report_date),
          skip=False, continue_on_error=continue_on_error)

    _step("5/5 build_report",
          lambda: build_report.run(report_date, top, plots,
                                   combined_path=combined_path, csv_path=csv_path),
          skip=False, continue_on_error=continue_on_error)

    report_csv = csv_path or (build_report.REPORTS_DIR
                              / f"campaign_opportunity_{report_date}.csv")
    logger.info("Pipeline finished in %.1fs", time.time() - overall)
    logger.info("Report: %s", report_csv)
    return {
        "report_date": report_date,
        "csv_path": report_csv,
        "combined": combined_path if combined_path and combined_path.exists() else None,
        "success": report_csv.exists(),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report-date", default=None,
                   help="Report date YYYY-MM-DD (default today IST).")
    p.add_argument("--days", type=int, default=90, help="Sales lookback window (default 90).")
    p.add_argument("--use-llm", action="store_true",
                   help="Use Azure OpenAI to bucket cities fuzzy can't resolve.")
    p.add_argument("--llm-min-confidence", type=int, default=80)
    p.add_argument("--skip-sales", action="store_true", help="Reuse existing city_sales_daily.csv.")
    p.add_argument("--skip-forecast", action="store_true", help="Reuse today's forecast payload.")
    p.add_argument("--skip-classify", action="store_true", help="Reuse today's classified CSV.")
    p.add_argument("--no-plots", action="store_true", help="Skip heatmap/geo PNGs.")
    p.add_argument("--top", type=int, default=25, help="Cities shown in the heatmap.")
    p.add_argument("--continue-on-error", action="store_true",
                   help="Keep going if a step fails (default: stop).")
    args = p.parse_args()

    report_date = args.report_date or datetime.now(IST).strftime("%Y-%m-%d")
    run(report_date, args.days, args.use_llm, args.llm_min_confidence,
        args.skip_sales, args.skip_forecast, args.skip_classify,
        not args.no_plots, args.top, args.continue_on_error)


if __name__ == "__main__":
    main()
