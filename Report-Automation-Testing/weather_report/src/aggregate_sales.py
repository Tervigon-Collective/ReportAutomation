"""Phase 2 orchestrator: build ``city_sales_daily.csv`` from Shopify orders.

Flow (docs/weather_report.md, Daily Automation steps 1-4):

1. Fetch raw per-(date, city, state) order aggregates (last N days).
2. Normalize + resolve each raw city to a canonical ``city_id``.
3. Aggregate resolved rows by ``(date, city_id)`` and append to
   ``city_sales_daily.csv`` (history-preserving; reruns replace same dates).
4. Append everything that could not be auto-resolved to
   ``review/unresolved_cities.csv``.

Usage (from weather_report/):

    py src/aggregate_sales.py                 # last 90 days ending today
    py src/aggregate_sales.py --days 30
    py src/aggregate_sales.py --start-date 2026-03-01 --end-date 2026-06-26
    py src/aggregate_sales.py --sample data/weather/_sample_raw.csv   # offline
    py src/aggregate_sales.py --dry-run       # compute, print, write nothing
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

try:
    from . import sales_source
    from .canonicalize import CityResolver
    from .normalize import normalize_city, normalize_state
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sales_source  # type: ignore
    from canonicalize import CityResolver  # type: ignore
    from normalize import normalize_city, normalize_state  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("aggregate_sales")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
SALES_DAILY_PATH = DATA_DIR / "city_sales_daily.csv"
UNRESOLVED_PATH = DATA_DIR / "review" / "unresolved_cities.csv"

IST = timezone(timedelta(hours=5, minutes=30))

SALES_COLUMNS = [
    "date", "city_id", "canonical_city", "orders", "revenue", "units",
    "customers", "cancelled_orders", "returned_orders", "created_at",
]
UNRESOLVED_COLUMNS = [
    "raw_city", "raw_city_normalized", "raw_state", "raw_state_normalized",
    "best_match_city", "best_match_city_id", "confidence_score", "match_method",
    "orders", "first_seen", "last_seen", "status",
]

_METRICS = ["orders", "revenue", "units", "customers", "cancelled_orders", "returned_orders"]


def _now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def resolve_frame(raw: pd.DataFrame, resolver: CityResolver) -> pd.DataFrame:
    """Attach resolution columns to each raw (city, state) row."""
    recs = []
    for r in raw.itertuples(index=False):
        res = resolver.resolve(r.raw_city, r.raw_state)
        recs.append({
            "city_id": res.city_id,
            "canonical_city": res.canonical_city,
            "match_method": res.match_method,
            "confidence_score": res.confidence_score,
            "resolved": res.resolved,
        })
    return pd.concat([raw.reset_index(drop=True), pd.DataFrame(recs)], axis=1)


def _apply_llm_result(enriched: pd.DataFrame, idx, row, resolver: CityResolver,
                      city_id: int, confidence: int) -> bool:
    """Apply one LLM/cache mapping to a row. Returns True if alias was new."""
    canon = resolver.canonical_for_id(city_id)
    if canon is None:
        return False
    enriched.at[idx, "city_id"] = city_id
    enriched.at[idx, "canonical_city"] = canon
    enriched.at[idx, "match_method"] = "llm"
    enriched.at[idx, "confidence_score"] = confidence
    enriched.at[idx, "resolved"] = True
    return resolver.add_alias(row["raw_city"], row["raw_state"], city_id,
                              "llm", confidence, reviewed=False)


def apply_llm_fallback(enriched: pd.DataFrame, resolver: CityResolver,
                       min_confidence: int) -> int:
    """Resolve still-unresolved cities via cache + Azure OpenAI.

    Only *new* (city, state) pairs not in the LLM cache are sent to the API.
    Prior abstains and successful mappings are reused from
    ``review/llm_resolution_cache.csv``.

    Returns the number of distinct (city, state) values newly bucketed.
    """
    try:
        from .llm_resolver import LLMResolver
        from .llm_cache import LLMResolutionCache
    except ImportError:
        from llm_resolver import LLMResolver  # type: ignore
        from llm_cache import LLMResolutionCache  # type: ignore

    if not LLMResolver.available():
        logger.warning("LLM fallback requested but Azure OpenAI env is incomplete; skipping.")
        return 0

    pend = enriched[~enriched["resolved"]]
    if pend.empty:
        return 0

    # Unique unresolved (city, state) from this sales fetch.
    uniq: dict[tuple[str, str], tuple[str, str]] = {}
    for r in pend.itertuples(index=False):
        ncity = normalize_city(r.raw_city)
        nstate = normalize_state(r.raw_state)
        if ncity and (ncity, nstate) not in uniq:
            uniq[(ncity, nstate)] = (r.raw_city, r.raw_state)

    cache = LLMResolutionCache()
    llm_queue: list[tuple[str, str]] = []
    cache_hits = 0
    cache_bucketed = 0

    for key, (raw_city, raw_state) in uniq.items():
        cached = cache.get(raw_city, raw_state)
        if cached is not None:
            cache_hits += 1
            if cached.city_id is not None and cached.confidence >= min_confidence:
                for idx, row in enriched[~enriched["resolved"]].iterrows():
                    rkey = (normalize_city(row["raw_city"]), normalize_state(row["raw_state"]))
                    if rkey == key:
                        if _apply_llm_result(enriched, idx, row, resolver,
                                             cached.city_id, cached.confidence):
                            cache_bucketed += 1
            continue
        llm_queue.append((raw_city, raw_state))

    logger.info(
        "LLM cache: %d hits (%d mapped), %d new cities for API (of %d unresolved)",
        cache_hits, cache_bucketed, len(llm_queue), len(uniq),
    )

    api_bucketed = 0
    if llm_queue:
        logger.info("LLM API: classifying %d new unresolved cities...", len(llm_queue))
        llm = LLMResolver(resolver.master, min_confidence=min_confidence)
        results = llm.resolve(llm_queue)
        for raw_city, raw_state in llm_queue:
            nkey = (normalize_city(raw_city), normalize_state(raw_state))
            res = results.get(nkey)
            if res is None:
                cache.put(raw_city, raw_state, None, 0, "no response")
                continue
            cache.put(raw_city, raw_state, res.city_id, res.confidence, res.reason)
            if res.city_id is None:
                continue
            for idx, row in enriched[~enriched["resolved"]].iterrows():
                rkey = (normalize_city(row["raw_city"]), normalize_state(row["raw_state"]))
                if rkey == nkey:
                    if _apply_llm_result(enriched, idx, row, resolver,
                                         res.city_id, res.confidence):
                        api_bucketed += 1

    cache.flush()
    if api_bucketed:
        logger.info("LLM API bucketed %d new cities.", api_bucketed)
    return cache_bucketed + api_bucketed


def build_sales_daily(resolved: pd.DataFrame) -> pd.DataFrame:
    if resolved.empty:
        return pd.DataFrame(columns=SALES_COLUMNS)
    grp = (
        resolved.groupby(["sale_date", "city_id", "canonical_city"], as_index=False)[_METRICS]
        .sum()
        .rename(columns={"sale_date": "date"})
    )
    grp["city_id"] = grp["city_id"].astype(int)
    grp["revenue"] = grp["revenue"].round(2)
    grp["created_at"] = _now_ist()
    return grp[SALES_COLUMNS].sort_values(["date", "city_id"]).reset_index(drop=True)


def build_unresolved(unresolved: pd.DataFrame) -> pd.DataFrame:
    if unresolved.empty:
        return pd.DataFrame(columns=UNRESOLVED_COLUMNS)
    rows = []
    keyed = unresolved.assign(
        _ncity=unresolved["raw_city"].map(normalize_city),
        _nstate=unresolved["raw_state"].map(normalize_state),
    )
    for (ncity, nstate), g in keyed.groupby(["_ncity", "_nstate"]):
        first = g.iloc[0]
        rows.append({
            "raw_city": first["raw_city"],
            "raw_city_normalized": ncity,
            "raw_state": first["raw_state"],
            "raw_state_normalized": nstate,
            "best_match_city": first.get("canonical_city"),
            "best_match_city_id": first.get("city_id"),
            "confidence_score": int(first.get("confidence_score", 0) or 0),
            "match_method": first.get("match_method"),
            "orders": int(g["orders"].sum()),
            "first_seen": g["sale_date"].min(),
            "last_seen": g["sale_date"].max(),
            "status": "pending",
        })
    return pd.DataFrame(rows, columns=UNRESOLVED_COLUMNS)


def _merge_sales_daily(new: pd.DataFrame) -> pd.DataFrame:
    """History-preserving: drop existing rows for dates in this run, append new."""
    if not SALES_DAILY_PATH.exists() or new.empty:
        return new
    existing = pd.read_csv(SALES_DAILY_PATH)
    if existing.empty:
        return new
    run_dates = set(new["date"].unique())
    kept = existing[~existing["date"].isin(run_dates)]
    return pd.concat([kept, new], ignore_index=True).sort_values(["date", "city_id"])


def _resolved_alias_keys() -> set[str]:
    """Normalized raw-city keys that are now resolvable via the alias map."""
    alias_path = DATA_DIR / "city_alias_map.csv"
    if not alias_path.exists():
        return set()
    amap = pd.read_csv(alias_path)
    return set(amap["raw_city_normalized"].astype(str))


def _merge_unresolved(new: pd.DataFrame) -> pd.DataFrame:
    """Upsert review queue keyed by (raw_city_normalized, raw_state_normalized),
    and drop any entry that has since become resolvable (now in the alias map)."""
    key = ["raw_city_normalized", "raw_state_normalized"]
    if UNRESOLVED_PATH.exists() and UNRESOLVED_PATH.stat().st_size > 0:
        existing = pd.read_csv(UNRESOLVED_PATH)
        merged = pd.concat([existing, new], ignore_index=True) if not existing.empty else new
    else:
        merged = new
    # Keep the latest occurrence (this run) for each key.
    merged = merged.drop_duplicates(subset=key, keep="last")
    # Purge entries that are now resolved (alias added via override/fuzzy/llm).
    resolved = _resolved_alias_keys()
    merged = merged[~merged["raw_city_normalized"].astype(str).isin(resolved)]
    return merged[UNRESOLVED_COLUMNS]


def run(start_date: str, end_date: str, sample: Path | None, dry_run: bool,
        use_llm: bool = False, llm_min_confidence: int = 80) -> None:
    if sample:
        logger.info("Loading raw orders from sample: %s", sample)
        raw = pd.read_csv(sample)
        missing = set(sales_source.RAW_COLUMNS) - set(raw.columns)
        if missing:
            raise SystemExit(f"Sample missing columns: {sorted(missing)}")
    else:
        logger.info("Fetching Shopify orders %s .. %s", start_date, end_date)
        raw = sales_source.fetch_raw_city_orders(start_date, end_date)

    if raw.empty:
        logger.warning("No raw order rows returned. Nothing to do.")
        return

    logger.info("Raw (date,city,state) rows: %d  | distinct cities: %d",
                len(raw), raw["raw_city"].nunique())

    resolver = CityResolver()
    enriched = resolve_frame(raw, resolver)

    fuzzy_resolved = int(enriched["resolved"].sum())
    if use_llm:
        n_llm = apply_llm_fallback(enriched, resolver, llm_min_confidence)
        logger.info("LLM fallback bucketed %d new cities.", n_llm)

    resolved = enriched[enriched["resolved"]].copy()
    unresolved = enriched[~enriched["resolved"]].copy()

    sales_daily = build_sales_daily(resolved)
    review = build_unresolved(unresolved)

    resolved_orders = int(resolved["orders"].sum()) if not resolved.empty else 0
    total_orders = int(enriched["orders"].sum())
    pct = (100.0 * resolved_orders / total_orders) if total_orders else 0.0

    logger.info("Resolved cities: %d rows (%d orders, %.1f%% of orders) "
                "[fuzzy/alias rows: %d]",
                len(sales_daily), resolved_orders, pct, fuzzy_resolved)
    logger.info("Unresolved cities: %d rows (%d orders) | new auto-aliases: %d",
                len(review), int(unresolved["orders"].sum()) if not unresolved.empty else 0,
                resolver.new_alias_count)

    if dry_run:
        logger.info("[dry-run] city_sales_daily preview:\n%s",
                    sales_daily.head(15).to_string(index=False))
        if not review.empty:
            logger.info("[dry-run] unresolved preview:\n%s",
                        review.head(15).to_string(index=False))
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UNRESOLVED_PATH.parent.mkdir(parents=True, exist_ok=True)

    _merge_sales_daily(sales_daily).to_csv(SALES_DAILY_PATH, index=False)
    logger.info("Wrote %s", SALES_DAILY_PATH)

    # Flush new aliases first so the review-queue purge sees this run's mappings.
    flushed = resolver.flush_new_aliases()
    if flushed:
        logger.info("Appended %d new aliases to city_alias_map.csv", flushed)

    _merge_unresolved(review).to_csv(UNRESOLVED_PATH, index=False)
    logger.info("Updated %s", UNRESOLVED_PATH)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=90,
                   help="Lookback window size in days (default 90).")
    p.add_argument("--end-date", default=None, help="Inclusive end date YYYY-MM-DD (default today).")
    p.add_argument("--start-date", default=None, help="Inclusive start date; overrides --days.")
    p.add_argument("--sample", type=Path, default=None,
                   help="Read raw orders from a CSV instead of the DB (offline).")
    p.add_argument("--dry-run", action="store_true", help="Compute but do not write files.")
    p.add_argument("--use-llm", action="store_true",
                   help="Use Azure OpenAI to bucket cities still unresolved after fuzzy.")
    p.add_argument("--llm-min-confidence", type=int, default=80,
                   help="Minimum LLM confidence to accept a mapping (default 80).")
    args = p.parse_args()

    end = args.end_date or datetime.now(IST).strftime("%Y-%m-%d")
    if args.start_date:
        start = args.start_date
    else:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")

    run(start, end, args.sample, args.dry_run,
        use_llm=args.use_llm, llm_min_confidence=args.llm_min_confidence)


if __name__ == "__main__":
    main()
