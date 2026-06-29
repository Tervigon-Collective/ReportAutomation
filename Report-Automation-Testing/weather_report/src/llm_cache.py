"""Persistent cache for LLM city-resolution results.

Stores both successful mappings and abstains (city_id=null) so repeat runs
only call Azure OpenAI for *new* unresolved cities from sales data. Cities
already in ``city_alias_map.csv`` are resolved before LLM runs and never
hit this cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

try:
    from .normalize import normalize_city, normalize_state
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from normalize import normalize_city, normalize_state  # type: ignore

MODULE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = MODULE_ROOT / "data" / "weather" / "review" / "llm_resolution_cache.csv"

CACHE_COLUMNS = [
    "raw_city_normalized",
    "raw_state_normalized",
    "city_id",
    "confidence",
    "reason",
    "created_at",
    "last_seen",
]


@dataclass(frozen=True)
class CachedLLMResult:
    city_id: int | None
    confidence: int
    reason: str


class LLMResolutionCache:
    """Disk-backed cache keyed by normalized (city, state)."""

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self._rows: dict[tuple[str, str], dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        df = pd.read_csv(self.path)
        for r in df.itertuples(index=False):
            key = (str(r.raw_city_normalized), str(r.raw_state_normalized))
            cid = r.city_id
            if pd.isna(cid):
                cid = None
            else:
                cid = int(cid)
            self._rows[key] = {
                "raw_city_normalized": key[0],
                "raw_state_normalized": key[1],
                "city_id": cid,
                "confidence": int(r.confidence or 0),
                "reason": str(r.reason or "")[:120],
                "created_at": str(r.created_at),
                "last_seen": str(r.last_seen),
            }

    def get(self, raw_city: str, raw_state: str) -> CachedLLMResult | None:
        key = (normalize_city(raw_city), normalize_state(raw_state))
        if not key[0] or key not in self._rows:
            return None
        row = self._rows[key]
        today = date.today().isoformat()
        if row["last_seen"] != today:
            row["last_seen"] = today
            self._dirty = True
        return CachedLLMResult(row["city_id"], row["confidence"], row["reason"])

    def put(self, raw_city: str, raw_state: str,
            city_id: int | None, confidence: int, reason: str) -> None:
        key = (normalize_city(raw_city), normalize_state(raw_state))
        if not key[0]:
            return
        today = date.today().isoformat()
        if key in self._rows:
            row = self._rows[key]
            row["city_id"] = city_id
            row["confidence"] = int(confidence)
            row["reason"] = str(reason or "")[:120]
            row["last_seen"] = today
        else:
            self._rows[key] = {
                "raw_city_normalized": key[0],
                "raw_state_normalized": key[1],
                "city_id": city_id,
                "confidence": int(confidence),
                "reason": str(reason or "")[:120],
                "created_at": today,
                "last_seen": today,
            }
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(list(self._rows.values()), columns=CACHE_COLUMNS)
        df = df.sort_values(["raw_city_normalized", "raw_state_normalized"])
        df.to_csv(self.path, index=False)
        self._dirty = False

    def __len__(self) -> int:
        return len(self._rows)
