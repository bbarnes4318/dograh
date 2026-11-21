"""Scoring helpers for labeled STT clips (no network, no provider code)."""

from __future__ import annotations

import re
import unicodedata

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def normalize(text: str) -> list[str]:
    """Lowercase, strip accents and punctuation, split into words."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("-", " ")
    words = re.findall(r"[a-z0-9']+", text)
    return [_NUMBER_WORDS.get(w, w) for w in words]


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """Word-level Levenshtein distance / reference length (None without a reference)."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return None if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return round(prev[-1] / len(ref), 4)


def endpoint_errors(expected_turns: int | None, observed_turns: int | None) -> dict:
    """False endpoints (extra turns) and missed endpoints (merged turns)."""
    if expected_turns is None or observed_turns is None:
        return {"false_endpoints": None, "missed_endpoints": None}
    return {
        "false_endpoints": max(observed_turns - expected_turns, 0),
        "missed_endpoints": max(expected_turns - observed_turns, 0),
    }


def cost_per_minute(price_per_hour: float) -> float:
    return round(price_per_hour / 60, 5)
