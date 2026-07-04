"""LLM fallback for city bucketing (Azure OpenAI).

For raw shipping cities that alias + fuzzy matching could not confidently
resolve, ask the model whether each raw city is the *same place* as one of the
canonical cities (a district / suburb / locality / alternate spelling). If so,
return its ``city_id``; otherwise return null so the value stays in the review
queue as a candidate *new* city (this keeps weather accuracy correct -- we
never bucket a genuinely distant city into the wrong metro).

Credentials reuse the project's Azure OpenAI env (same as metaActivityTrack.py):
``AZURE_OPENAI_API_KEY``, ``AZURE_OPENAI_ENDPOINT``, ``AZURE_DEPLOYMENT``,
``AZURE_OPENAI_API_VERSION`` (default 2024-12-01-preview).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:
    from .normalize import normalize_city, normalize_state
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from normalize import normalize_city, normalize_state  # type: ignore

logger = logging.getLogger("llm_resolver")

MODULE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = MODULE_ROOT.parent  # holds .env

DEFAULT_BATCH = 25
DEFAULT_MIN_CONFIDENCE = 80  # below this, treat as no-match (stay in review)
# gpt-5-mini is a reasoning model: max_completion_tokens covers reasoning +
# output, so this must be generous or the JSON array is truncated.
MAX_COMPLETION_TOKENS = 12000

_SYSTEM = (
    "You are a geography expert for Indian e-commerce logistics. You map raw "
    "shipping city/state values to a fixed list of canonical monitored cities. "
    "A raw value should map to a canonical city ONLY if it is the same place or "
    "a district, suburb, locality, satellite town, or alternate spelling of that "
    "canonical city (e.g. 'South Delhi' -> Delhi NCR, 'Mira Bhayandar' -> Mumbai, "
    "'Ranga Reddy' -> Hyderabad). If the raw value is a genuinely different city "
    "that is NOT represented in the list, return null for city_id. Never guess a "
    "far-away city just because the name looks similar."
)

_INSTRUCTION = (
    "Canonical cities (id | city | state | region):\n{catalog}\n\n"
    "Map each raw input below. Respond with ONLY a JSON array, one object per "
    "input in the SAME order, each: "
    '{{"i": <input index>, "city_id": <int or null>, "confidence": <0-100>, '
    '"reason": "<short>"}}.\n\n'
    "Raw inputs:\n{inputs}"
)


@dataclass
class LLMResult:
    city_id: int | None
    confidence: int
    reason: str


class LLMResolver:
    def __init__(self, master: pd.DataFrame,
                 batch_size: int = DEFAULT_BATCH,
                 min_confidence: int = DEFAULT_MIN_CONFIDENCE):
        self.master = master
        self.batch_size = batch_size
        self.min_confidence = min_confidence
        self._valid_ids = set(int(x) for x in master["city_id"])
        self._catalog = "\n".join(
            f"{int(r.city_id)} | {r.canonical_city} | {r.state} | {r.region}"
            for r in master.itertuples(index=False)
        )
        self._client = None
        self._deployment = os.getenv("AZURE_DEPLOYMENT")

    # -- availability -----------------------------------------------------
    @staticmethod
    def available() -> bool:
        from dotenv import load_dotenv
        env_path = PROJECT_ROOT / ".env"
        load_dotenv(env_path if env_path.exists() else None)
        return all(os.getenv(k) for k in
                   ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_DEPLOYMENT"))

    def _get_client(self):
        if self._client is None:
            from openai import AzureOpenAI
            self._client = AzureOpenAI(
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            )
            self._deployment = os.getenv("AZURE_DEPLOYMENT")
        return self._client

    # -- public API -------------------------------------------------------
    def resolve(self, items: list[tuple[str, str]]) -> dict[tuple[str, str], LLMResult]:
        """Resolve a list of (raw_city, raw_state). Keyed by normalized pair."""
        # Deduplicate by normalized key.
        uniq: dict[tuple[str, str], tuple[str, str]] = {}
        for city, state in items:
            key = (normalize_city(city), normalize_state(state))
            if key[0] and key not in uniq:
                uniq[key] = (city, state)

        results: dict[tuple[str, str], LLMResult] = {}
        keys = list(uniq.keys())
        for start in range(0, len(keys), self.batch_size):
            chunk = keys[start:start + self.batch_size]
            batch_inputs = [uniq[k] for k in chunk]
            try:
                parsed = self._call(batch_inputs)
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("LLM batch failed (%d items): %s", len(chunk), exc)
                continue
            logger.info("  LLM batch %d-%d: parsed %d/%d objects",
                        start, start + len(chunk) - 1, len(parsed), len(chunk))
            for i, key in enumerate(chunk):
                res = parsed.get(i)
                if res is None:
                    continue
                cid = res.city_id
                if cid is not None and (cid not in self._valid_ids
                                        or res.confidence < self.min_confidence):
                    cid = None
                results[key] = LLMResult(cid, res.confidence, res.reason)
        return results

    # -- internals --------------------------------------------------------
    def _call(self, batch_inputs: list[tuple[str, str]]) -> dict[int, LLMResult]:
        inputs_text = "\n".join(
            f'{i}: city="{c}", state="{s}"' for i, (c, s) in enumerate(batch_inputs)
        )
        prompt = _INSTRUCTION.format(catalog=self._catalog, inputs=inputs_text)
        client = self._get_client()
        resp = client.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=MAX_COMPLETION_TOKENS,
        )
        content = (resp.choices[0].message.content or "").strip()
        data = _parse_json_array(content)
        out: dict[int, LLMResult] = {}
        for obj in data:
            try:
                idx = int(obj["i"])
            except (KeyError, TypeError, ValueError):
                continue
            cid = obj.get("city_id")
            cid = int(cid) if isinstance(cid, (int, float)) and cid is not None else None
            conf = int(obj.get("confidence", 0) or 0)
            out[idx] = LLMResult(cid, conf, str(obj.get("reason", ""))[:120])
        return out


_OBJ_RE = re.compile(r"\{[^{}]*\}")


def _parse_json_array(text: str) -> list:
    """Parse a JSON array, recovering individual objects if the array is
    truncated (common with reasoning models that hit the token cap)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Fallback: extract each {...} object individually (tolerates truncation).
    objs = []
    for m in _OBJ_RE.finditer(text):
        try:
            objs.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return objs
