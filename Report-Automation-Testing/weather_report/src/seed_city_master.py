"""Phase 1 seeder: build ``city_master.csv`` and ``city_alias_map.csv``.

Reads the curated starter workbook
(``docs/weather/india_city_master_openmeteo_starter.xlsx``) and emits the two
foundational CSVs in the schema defined in ``docs/weather_report.md``.

This script is idempotent: re-running it regenerates the CSVs from the
workbook. It is the source of truth for the *seed* state; ongoing alias
discoveries are appended to ``city_alias_map.csv`` by later phases.

Usage (from the module root ``weather_report/``):

    py src/seed_city_master.py
    py src/seed_city_master.py --xlsx <path> --out-dir <path>
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

try:
    # When run as a module: ``py -m src.seed_city_master``
    from .normalize import normalize_city, normalize_state
except ImportError:  # When run as a script: ``py src/seed_city_master.py``
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from normalize import normalize_city, normalize_state  # type: ignore

# Module root = parent of this file's ``src`` directory.
MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MODULE_ROOT.parents[1]  # .../ReportAutomation

DEFAULT_XLSX = REPO_ROOT / "docs" / "weather" / "india_city_master_openmeteo_starter.xlsx"
DEFAULT_OUT_DIR = MODULE_ROOT / "data" / "weather"

TIMEZONE = "Asia/Kolkata"

CITY_MASTER_COLUMNS = [
    "city_id",
    "canonical_city",
    "state",
    "country",
    "region",
    "city_tier",
    "latitude",
    "longitude",
    "timezone",
    "population",
    "is_active",
]

ALIAS_COLUMNS = [
    "alias_id",
    "raw_city_normalized",
    "raw_state_normalized",
    "city_id",
    "canonical_city",
    "match_method",
    "confidence_score",
    "reviewed",
    "created_at",
]

UNRESOLVED_COLUMNS = [
    "raw_city",
    "raw_city_normalized",
    "raw_state",
    "raw_state_normalized",
    "best_match_city",
    "best_match_city_id",
    "confidence_score",
    "match_method",
    "orders",
    "first_seen",
    "last_seen",
    "status",
]


def _to_bool_str(value) -> str:
    return "true" if bool(value) else "false"


def build_city_master(xlsx_path: Path) -> pd.DataFrame:
    """Map the starter workbook's ``city_master`` sheet into the plan schema."""
    raw = pd.read_excel(xlsx_path, sheet_name="city_master")

    out = pd.DataFrame()
    out["city_id"] = raw["city_id"].astype(int)
    out["canonical_city"] = raw["canonical_city"].astype(str).str.strip()
    out["state"] = raw["state"].astype(str).str.strip()
    out["country"] = raw["country"].fillna("India").astype(str).str.strip()
    out["region"] = raw["region"].astype(str).str.strip()
    out["city_tier"] = raw["city_tier"].astype(str).str.strip()
    out["latitude"] = raw["latitude"].astype(float).round(4)
    out["longitude"] = raw["longitude"].astype(float).round(4)
    out["timezone"] = TIMEZONE
    # Population is intentionally blank in the starter; keep it nullable.
    out["population"] = (
        raw["population"].astype("Int64") if "population" in raw else pd.NA
    )
    active_col = "is_active_market" if "is_active_market" in raw else "is_active"
    out["is_active"] = raw[active_col].fillna(True).map(_to_bool_str)

    out = out[CITY_MASTER_COLUMNS].sort_values("city_id").reset_index(drop=True)
    return out


def _aliases_from_workbook(xlsx_path: Path, master: pd.DataFrame) -> pd.DataFrame:
    """Prefer the exploded sheet; fall back to the JSON ``aliases`` column."""
    sheets = pd.ExcelFile(xlsx_path).sheet_names
    pairs: list[tuple[str, str]] = []  # (canonical_city, alias)

    if "aliases_exploded" in sheets:
        ex = pd.read_excel(xlsx_path, sheet_name="aliases_exploded")
        for _, row in ex.iterrows():
            pairs.append((str(row["canonical_city"]).strip(), str(row["alias"]).strip()))
    else:
        raw = pd.read_excel(xlsx_path, sheet_name="city_master")
        for _, row in raw.iterrows():
            canon = str(row["canonical_city"]).strip()
            try:
                aliases = json.loads(row.get("aliases") or "[]")
            except (TypeError, json.JSONDecodeError):
                aliases = []
            for alias in aliases:
                pairs.append((canon, str(alias).strip()))

    return pd.DataFrame(pairs, columns=["canonical_city", "alias"])


def build_alias_map(xlsx_path: Path, master: pd.DataFrame) -> pd.DataFrame:
    """Seed ``city_alias_map.csv`` from curated aliases (match_method=alias)."""
    canon_to_id = dict(zip(master["canonical_city"], master["city_id"]))
    canon_to_state = dict(zip(master["canonical_city"], master["state"]))

    pairs = _aliases_from_workbook(xlsx_path, master)

    # Guarantee each canonical city resolves to itself, even when the bucket
    # label (e.g. "Delhi NCR") isn't among the curated member aliases.
    self_pairs = pd.DataFrame(
        {"canonical_city": master["canonical_city"], "alias": master["canonical_city"]}
    )
    pairs = pd.concat([self_pairs, pairs], ignore_index=True)

    created_at = date.today().isoformat()

    seen: set[tuple[str, int]] = set()
    rows: list[dict] = []
    alias_id = 1
    skipped: list[str] = []

    for _, row in pairs.iterrows():
        canon = row["canonical_city"]
        if canon not in canon_to_id:
            skipped.append(canon)
            continue
        city_id = int(canon_to_id[canon])
        raw_city_norm = normalize_city(row["alias"])
        if not raw_city_norm:
            continue
        key = (raw_city_norm, city_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "alias_id": alias_id,
                "raw_city_normalized": raw_city_norm,
                "raw_state_normalized": normalize_state(canon_to_state.get(canon, "")),
                "city_id": city_id,
                "canonical_city": canon,
                "match_method": "alias",
                "confidence_score": 100,
                "reviewed": "true",
                "created_at": created_at,
            }
        )
        alias_id += 1

    if skipped:
        print(f"[warn] {len(skipped)} alias rows skipped (canonical not in master): "
              f"{sorted(set(skipped))}")

    return pd.DataFrame(rows, columns=ALIAS_COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX,
                        help="Path to the starter workbook.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="Output data/weather directory.")
    args = parser.parse_args()

    xlsx_path: Path = args.xlsx
    out_dir: Path = args.out_dir
    if not xlsx_path.exists():
        raise SystemExit(f"Starter workbook not found: {xlsx_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "review").mkdir(parents=True, exist_ok=True)

    master = build_city_master(xlsx_path)
    alias_map = build_alias_map(xlsx_path, master)

    master_path = out_dir / "city_master.csv"
    alias_path = out_dir / "city_alias_map.csv"
    unresolved_path = out_dir / "review" / "unresolved_cities.csv"

    master.to_csv(master_path, index=False)
    alias_map.to_csv(alias_path, index=False)
    if not unresolved_path.exists():
        pd.DataFrame(columns=UNRESOLVED_COLUMNS).to_csv(unresolved_path, index=False)

    print(f"Wrote {len(master):>4} cities  -> {master_path}")
    print(f"Wrote {len(alias_map):>4} aliases -> {alias_path}")
    print(f"Ensured empty review queue -> {unresolved_path}")

    # Fold curated manual overrides into the freshly seeded alias map.
    try:
        from .apply_overrides import apply_overrides
    except ImportError:
        from apply_overrides import apply_overrides  # type: ignore
    added = apply_overrides(master_path, alias_path)
    if added:
        print(f"Applied {added} manual override(s) -> {alias_path}")


if __name__ == "__main__":
    main()
