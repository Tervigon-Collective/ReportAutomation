"""Apply curated manual alias overrides into ``city_alias_map.csv``.

Reads ``config/city_alias_overrides.csv`` (``raw_city, canonical_city[, note]``)
and upserts each row into ``city_alias_map.csv`` with ``match_method=manual``,
``confidence_score=100``, ``reviewed=true``. Manual overrides win over any
existing auto-discovered (fuzzy/llm) mapping for the same normalized city.

Idempotent: re-running makes no further changes once applied.

Usage (from weather_report/):  py src/apply_overrides.py
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

try:
    from .normalize import normalize_city, normalize_state
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from normalize import normalize_city, normalize_state  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("apply_overrides")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
MASTER_PATH = DATA_DIR / "city_master.csv"
ALIAS_PATH = DATA_DIR / "city_alias_map.csv"
OVERRIDES_PATH = DATA_DIR / "config" / "city_alias_overrides.csv"

ALIAS_COLUMNS = [
    "alias_id", "raw_city_normalized", "raw_state_normalized", "city_id",
    "canonical_city", "match_method", "confidence_score", "reviewed", "created_at",
]


def apply_overrides(master_path: Path = MASTER_PATH,
                    alias_path: Path = ALIAS_PATH,
                    overrides_path: Path = OVERRIDES_PATH) -> int:
    """Upsert manual overrides into the alias map. Returns rows added/updated."""
    if not overrides_path.exists():
        logger.info("No overrides file at %s; nothing to do.", overrides_path)
        return 0

    master = pd.read_csv(master_path)
    canon_to_id = {str(c).strip().lower(): int(i)
                   for c, i in zip(master["canonical_city"], master["city_id"])}

    alias = (pd.read_csv(alias_path) if alias_path.exists()
             else pd.DataFrame(columns=ALIAS_COLUMNS))
    by_key = {str(r.raw_city_normalized): idx
              for idx, r in zip(alias.index, alias.itertuples(index=False))}
    next_id = int(alias["alias_id"].max()) + 1 if len(alias) else 1

    overrides = pd.read_csv(overrides_path)
    today = date.today().isoformat()
    changed = 0

    for r in overrides.itertuples(index=False):
        canon = str(r.canonical_city).strip()
        cid = canon_to_id.get(canon.lower())
        if cid is None:
            logger.warning("Override canonical not in master, skipped: %r", canon)
            continue
        ncity = normalize_city(r.raw_city)
        nstate = normalize_state(getattr(r, "raw_state", "") or "")
        if not ncity:
            continue
        record = {
            "raw_city_normalized": ncity,
            "raw_state_normalized": nstate,
            "city_id": cid,
            "canonical_city": canon,
            "match_method": "manual",
            "confidence_score": 100,
            "reviewed": "true",
            "created_at": today,
        }
        if ncity in by_key:
            idx = by_key[ncity]
            existing = alias.loc[idx]
            if existing["match_method"] == "manual" and int(existing["city_id"]) == cid:
                continue  # already applied
            record["alias_id"] = int(existing["alias_id"])
            for k, v in record.items():
                alias.loc[idx, k] = v
            changed += 1
        else:
            record["alias_id"] = next_id
            alias = pd.concat([alias, pd.DataFrame([record], columns=ALIAS_COLUMNS)],
                              ignore_index=True)
            by_key[ncity] = alias.index[-1]
            next_id += 1
            changed += 1

    if changed:
        alias.to_csv(alias_path, index=False)
    logger.info("Applied %d override(s) -> %s", changed, alias_path)
    return changed


if __name__ == "__main__":
    apply_overrides()
