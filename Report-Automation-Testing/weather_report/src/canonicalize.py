"""Phase 2 city canonicalization: raw Shopify city -> canonical ``city_id``.

Resolution order (per docs/weather_report.md):

1. Exact alias lookup in ``city_alias_map.csv`` (match_method=alias).
2. Fuzzy match against known aliases + canonical names in ``city_master.csv``:
   - score >= 95            -> auto-map (fuzzy), append new alias.
   - score 85-94 + state OK -> auto-map (state_aware_fuzzy), append new alias.
   - score 70-84            -> review queue.
   - score < 70             -> unresolved.

Newly auto-mapped aliases are persisted back to ``city_alias_map.csv`` so the
same raw value is resolved instantly next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

try:
    from .normalize import normalize_city, normalize_state
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from normalize import normalize_city, normalize_state  # type: ignore

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
MASTER_PATH = DATA_DIR / "city_master.csv"
ALIAS_PATH = DATA_DIR / "city_alias_map.csv"

AUTO_MAP_GTE = 95
STATE_MATCH_GTE = 85
REVIEW_GTE = 70

ALIAS_COLUMNS = [
    "alias_id", "raw_city_normalized", "raw_state_normalized", "city_id",
    "canonical_city", "match_method", "confidence_score", "reviewed", "created_at",
]


@dataclass
class Resolution:
    city_id: int | None
    canonical_city: str | None
    match_method: str          # alias | fuzzy | state_aware_fuzzy | review | unresolved | unknown
    confidence_score: int
    resolved: bool             # True only when city_id is assigned


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class CityResolver:
    """Stateful resolver that caches lookups and accumulates new aliases."""

    def __init__(self, master_path: Path = MASTER_PATH, alias_path: Path = ALIAS_PATH):
        self.master_path = master_path
        self.alias_path = alias_path
        self.master = pd.read_csv(master_path)
        self.alias = (
            pd.read_csv(alias_path) if alias_path.exists()
            else pd.DataFrame(columns=ALIAS_COLUMNS)
        )

        # Fast exact-alias index: normalized raw city -> (city_id, canonical).
        self._alias_index: dict[str, tuple[int, str]] = {
            str(r.raw_city_normalized): (int(r.city_id), str(r.canonical_city))
            for r in self.alias.itertuples(index=False)
        }

        # Candidate strings for fuzzy matching: alias keys + canonical names.
        self._candidates: list[tuple[str, int, str]] = []  # (norm_text, city_id, canonical)
        for r in self.alias.itertuples(index=False):
            self._candidates.append(
                (str(r.raw_city_normalized), int(r.city_id), str(r.canonical_city))
            )
        for r in self.master.itertuples(index=False):
            self._candidates.append(
                (normalize_city(r.canonical_city), int(r.city_id), str(r.canonical_city))
            )

        self._city_state_norm: dict[int, str] = {
            int(r.city_id): normalize_state(r.state)
            for r in self.master.itertuples(index=False)
        }

        self._cache: dict[tuple[str, str], Resolution] = {}
        self._new_aliases: list[dict] = []
        self._next_alias_id = (
            int(self.alias["alias_id"].max()) + 1 if len(self.alias) else 1
        )

    # -- public API -------------------------------------------------------
    def resolve(self, raw_city: str | None, raw_state: str | None = "") -> Resolution:
        ncity = normalize_city(raw_city)
        nstate = normalize_state(raw_state)
        key = (ncity, nstate)
        if key in self._cache:
            return self._cache[key]

        res = self._resolve_uncached(ncity, nstate)
        self._cache[key] = res
        return res

    def canonical_for_id(self, city_id: int) -> str | None:
        row = self.master.loc[self.master["city_id"] == int(city_id), "canonical_city"]
        return None if row.empty else str(row.iloc[0])

    def add_alias(self, raw_city, raw_state, city_id, method, score,
                  reviewed: bool = False) -> bool:
        """Public upsert of a resolved alias (e.g. from LLM or manual override).

        Returns True if a new alias was registered, False if it already existed.
        """
        ncity = normalize_city(raw_city)
        nstate = normalize_state(raw_state)
        if not ncity or ncity in self._alias_index:
            return False
        canon = self.canonical_for_id(city_id)
        if canon is None:
            return False
        self._register_alias(ncity, nstate, int(city_id), canon, method, int(score),
                             reviewed=reviewed)
        return True

    @property
    def new_alias_count(self) -> int:
        return len(self._new_aliases)

    def flush_new_aliases(self) -> int:
        """Append auto-discovered aliases to city_alias_map.csv. Returns count."""
        if not self._new_aliases:
            return 0
        new_df = pd.DataFrame(self._new_aliases, columns=ALIAS_COLUMNS)
        combined = pd.concat([self.alias, new_df], ignore_index=True)
        combined.to_csv(self.alias_path, index=False)
        self.alias = combined
        n = len(self._new_aliases)
        self._new_aliases = []
        return n

    # -- internals --------------------------------------------------------
    def _resolve_uncached(self, ncity: str, nstate: str) -> Resolution:
        if not ncity or ncity == "unknown":
            return Resolution(None, None, "unknown", 0, False)

        # 1. exact alias
        if ncity in self._alias_index:
            cid, canon = self._alias_index[ncity]
            return Resolution(cid, canon, "alias", 100, True)

        # 2. fuzzy
        best_score = 0.0
        best_cid: int | None = None
        best_canon: str | None = None
        for cand_text, cid, canon in self._candidates:
            score = _ratio(ncity, cand_text)
            if score > best_score:
                best_score, best_cid, best_canon = score, cid, canon

        pct = int(round(best_score * 100))
        state_ok = bool(nstate) and best_cid is not None and (
            nstate == self._city_state_norm.get(best_cid, "")
            or nstate in self._city_state_norm.get(best_cid, "")
            or self._city_state_norm.get(best_cid, "") in nstate
        )

        if pct >= AUTO_MAP_GTE:
            self._register_alias(ncity, nstate, best_cid, best_canon, "fuzzy", pct)
            return Resolution(best_cid, best_canon, "fuzzy", pct, True)
        if pct >= STATE_MATCH_GTE and state_ok:
            self._register_alias(ncity, nstate, best_cid, best_canon,
                                 "state_aware_fuzzy", pct)
            return Resolution(best_cid, best_canon, "state_aware_fuzzy", pct, True)
        if pct >= REVIEW_GTE:
            return Resolution(best_cid, best_canon, "review", pct, False)
        return Resolution(best_cid, best_canon, "unresolved", pct, False)

    def _register_alias(self, ncity, nstate, cid, canon, method, pct,
                        reviewed: bool = False) -> None:
        if ncity in self._alias_index:
            return
        self._alias_index[ncity] = (int(cid), str(canon))
        self._candidates.append((ncity, int(cid), str(canon)))
        self._new_aliases.append({
            "alias_id": self._next_alias_id,
            "raw_city_normalized": ncity,
            "raw_state_normalized": nstate,
            "city_id": int(cid),
            "canonical_city": canon,
            "match_method": method,
            "confidence_score": pct,
            "reviewed": "true" if reviewed else "false",
            "created_at": date.today().isoformat(),
        })
        self._next_alias_id += 1
