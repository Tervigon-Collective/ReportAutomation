"""Text normalization helpers for city/state canonicalization.

These functions perform *pure text* normalization only (lowercase, trim,
strip punctuation, collapse whitespace). Alias -> canonical resolution is
intentionally NOT done here -- that lives in ``city_alias_map.csv`` so that
mappings are data-driven and reviewable rather than hard-coded.
"""

from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[.\-_/,]+")
_WS_RE = re.compile(r"\s+")


def normalize_city(city: str | None) -> str:
    """Normalize a raw city string for matching.

    Steps: lowercase -> strip -> replace punctuation with spaces ->
    collapse repeated whitespace.

    >>> normalize_city("  New-Delhi ")
    'new delhi'
    >>> normalize_city("Bengaluru.")
    'bengaluru'
    """
    if city is None:
        return ""
    text = str(city).lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_state(state: str | None) -> str:
    """Normalize a raw state/province string. Same rules as cities."""
    return normalize_city(state)
