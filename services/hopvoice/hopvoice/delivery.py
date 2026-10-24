"""Delivery Planner: decides *how* a sentence is spoken, never *what* is said.

The planner reads the text and the previous turn in the call session and returns
a ``Delivery``. It has no access to, and never returns, modified text: the TTS
input is passed through byte-for-byte, so it cannot invent or alter insurance
claims, numbers or names.

What the Delivery maps to on a Qwen3 Base clone worker (the levers that are
audible on that model; it ignores free-text ``instructions``):

- ``style``: picks one of the voice's registered style references
  (``voices.json`` -> ``styles``). ICL cloning carries the reference's delivery
  (warmth, energy, pace) into the utterance while the speaker stays the same.
  Unknown styles fall back to the voice's base reference.
- ``temperature``: lower for confirmations/numbers/terms (steady, precise),
  slightly higher for warm/upbeat turns (more natural variation).
- ``pause_before_ms``: silence prepended to the turn, e.g. a beat before an
  answer that follows an empathetic line, none inside a running reply.

The descriptive fields (speech_act, warmth, pace, energy, emphasis) are carried
in ``turn_done`` for listening review and for models that accept instructions.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_ACK = {
    "okay",
    "ok",
    "got it",
    "perfect",
    "absolutely",
    "sure",
    "great",
    "alright",
    "all right",
    "right",
    "yes",
    "yeah",
    "mm-hmm",
    "uh-huh",
    "that's fine",
    "no problem",
    "of course",
    "sounds good",
    "awesome",
    "thank you",
    "thanks",
}
_EMPATHY = re.compile(
    r"\b(sorry to hear|i understand|understandable|take (all )?the time|"
    r"no rush|i hear you|that must|completely understand|feel the same)\b",
    re.I,
)
_OBJECTION = re.compile(
    r"\b(only take a (second|minute)|no problem|no obligation|"
    r"the licensed agent can answer|not trying to)\b",
    re.I,
)
_TRANSFER = re.compile(
    r"\b(bring|get|connect|transfer)\b.*\b(licensed agent|agent)\b.*\b(on|line|now|with us)\b",
    re.I,
)
_UPBEAT = re.compile(
    r"(!|\b(great news|awesome|wonderful|congrat|glad to hear|good shape)\b)", re.I
)
_APOLOGY = re.compile(
    r"^\s*(oh,?\s*)?(sorry|pardon|my apologies)\b|\b(go ahead|what was that)\b", re.I
)
_CORRECTION = re.compile(r"\b(i meant|let me correct|actually,? (it'?s|i))\b", re.I)
_NUMBER = re.compile(
    r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)\b",
    re.I,
)
_TERMS = (
    "final expense",
    "beneficiary",
    "burial coverage",
    "whole life insurance",
    "licensed agent",
    "nursing home",
    "assisted living",
    "premium",
    "coverage amount",
    "funeral",
)


@dataclass(frozen=True)
class Delivery:
    speech_act: str = "statement"
    style: str = "neutral"
    warmth: str = "medium"
    pace: str = "medium"
    energy: str = "medium"
    temperature: float | None = None
    pause_before_ms: int = 0
    emphasis: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["emphasis"] = list(self.emphasis)
        return data


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z' -]+", "", text.lower()).strip(" -")


def plan(text: str, previous: Delivery | None = None) -> Delivery:
    """Choose delivery for one TTS turn. ``text`` is read, never rewritten."""
    stripped = text.strip()
    norm = _normalized(stripped)
    words = norm.split()
    emphasis = tuple(term for term in _TERMS if term in stripped.lower())
    # A turn right after an empathetic one gets a short beat, the way people
    # pause before moving on; consecutive sentences of one reply do not.
    beat = 180 if previous and previous.speech_act == "empathy" else 0

    if norm in _ACK or (len(words) <= 3 and norm.rstrip(".!") in _ACK):
        return Delivery(
            "acknowledgment", "short", "medium", "medium", "medium", 0.7, beat, emphasis
        )
    if _APOLOGY.search(stripped):
        return Delivery("yield", "warm", "high", "medium", "low", 0.75, 0, emphasis)
    if _EMPATHY.search(stripped):
        return Delivery("empathy", "warm", "high", "slow", "low", 0.75, beat, emphasis)
    if _TRANSFER.search(stripped):
        return Delivery(
            "transfer", "neutral", "medium", "medium", "medium", 0.7, beat, emphasis
        )
    if _CORRECTION.search(stripped):
        return Delivery(
            "correction", "neutral", "medium", "medium", "medium", 0.65, beat, emphasis
        )
    if _OBJECTION.search(stripped):
        return Delivery(
            "reassurance", "warm", "high", "medium", "low", 0.75, beat, emphasis
        )
    if _UPBEAT.search(stripped):
        return Delivery(
            "upbeat", "upbeat", "high", "medium", "medium-high", 0.85, beat, emphasis
        )
    if stripped.endswith("?"):
        act = "confirmation" if _NUMBER.search(stripped) else "question"
        temp = 0.65 if act == "confirmation" else 0.75
        return Delivery(
            act, "neutral", "medium", "medium", "medium", temp, beat, emphasis
        )
    if _NUMBER.search(stripped) or emphasis:
        return Delivery(
            "information", "neutral", "medium", "medium", "medium", 0.7, beat, emphasis
        )
    return Delivery(
        "statement", "neutral", "medium", "medium", "medium", None, beat, emphasis
    )


def from_client(raw: dict | None) -> Delivery | None:
    """Accept an explicit delivery from the client (only known fields)."""
    if not isinstance(raw, dict):
        return None
    allowed = {k: raw[k] for k in Delivery.__dataclass_fields__ if k in raw}
    if not allowed:
        return None
    if "emphasis" in allowed:
        allowed["emphasis"] = tuple(str(x) for x in allowed["emphasis"] or ())
    if "temperature" in allowed and allowed["temperature"] is not None:
        allowed["temperature"] = min(1.2, max(0.3, float(allowed["temperature"])))
    if "pause_before_ms" in allowed:
        allowed["pause_before_ms"] = min(1500, max(0, int(allowed["pause_before_ms"])))
    return Delivery(**allowed)
