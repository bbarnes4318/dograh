"""Concise, neutral ``agent_context`` for AssemblyAI's voice-agent models.

AssemblyAI uses the agent's last utterance to transcribe the caller's reply
better (short answers, ages, states, spelled names). We send that utterance,
plus a one-line hint naming the workflow step, the *kind* of reply the question
invites and the call language.

The hint names answer types, never answers: "a yes/no answer or a correction",
not "yes". It must not nudge the recogniser toward a favourable reply, and
nothing here interprets the caller's words: consent and qualification are
decided downstream from final transcripts only.
"""

from __future__ import annotations

import re

from api.services.configuration.assemblyai import ASSEMBLYAI_MAX_AGENT_CONTEXT_CHARS

# Only the tail of a long bot turn matters for the next reply.
_MAX_AGENT_TEXT_CHARS = 900

_LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "multi": "English or Spanish",
    "de": "German",
    "fr": "French",
    "pt": "Portuguese",
    "it": "Italian",
}

_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|"
    "maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
    "nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    "north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
    "wisconsin|wyoming|puerto rico"
)

# (pattern, hint) in priority order; matched against the agent's last question.
_EXPECTATIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(how old|your age|age are you|year were you born|date of birth|birthday|cu[aá]ntos años|edad|fecha de nacimiento)\b"
        ),
        "an age or date of birth (spoken numbers)",
    ),
    (
        re.compile(
            rf"\b(what state|which state|state do you live|state are you|(in|from) ({_US_STATES})\b.*\?|qu[eé] estado|en qu[eé] estado)",
            re.I,
        ),
        "a US state name, a yes/no confirmation, or a correction",
    ),
    (
        re.compile(r"\b(zip|postal code|c[oó]digo postal)\b"),
        "a ZIP code (digits)",
    ),
    (
        re.compile(
            r"\b(phone number|best number|callback number|n[uú]mero de tel[eé]fono)\b"
        ),
        "a phone number (digits)",
    ),
    (
        re.compile(
            r"\b(your name|first name|last name|spell|su nombre|c[oó]mo se llama)\b"
        ),
        "a personal name, possibly spelled out",
    ),
    (
        re.compile(r"\b(how much|how many|amount|budget|coverage amount|cu[aá]nto)\b"),
        "a number or amount",
    ),
    (
        re.compile(r"\b(when|what day|what time|cu[aá]ndo|qu[eé] d[ií]a)\b.*\?"),
        "a day, date or time",
    ),
    (
        re.compile(r"\b(email|correo)\b"),
        "an email address, possibly spelled out",
    ),
    (
        re.compile(
            r"\b(do you|did you|are you|is it|is that|have you|can you|could you|would you|will you|correct|right\?|okay\?|¿)",
            re.I,
        ),
        "a yes/no answer, a refusal, or a clarifying question",
    ),
]


def _last_question(text: str) -> str:
    """The last sentence ending in '?', or the last sentence when none does."""
    sentences = re.split(r"(?<=[.?!])\s+", text.strip())
    for sentence in reversed(sentences):
        if sentence.rstrip().endswith("?"):
            return sentence
    return sentences[-1] if sentences else ""


def infer_expected_reply(agent_text: str) -> str | None:
    question = _last_question(agent_text or "").lower()
    if not question:
        return None
    for pattern, hint in _EXPECTATIONS:
        if pattern.search(question):
            return hint
    return None


def compose_agent_context(
    agent_text: str,
    *,
    node_name: str | None = None,
    language: str | None = None,
    include_hints: bool = True,
) -> str:
    """Build the ``agent_context`` value for the caller's next turn."""
    text = " ".join((agent_text or "").split())
    if len(text) > _MAX_AGENT_TEXT_CHARS:
        text = text[-_MAX_AGENT_TEXT_CHARS:]
    if not text:
        return ""
    if not include_hints:
        return text

    hints: list[str] = []
    if node_name:
        hints.append(f"step: {' '.join(str(node_name).split())[:80]}")
    expected = infer_expected_reply(text)
    if expected:
        hints.append(f"likely reply: {expected}")
    lang = _LANGUAGE_NAMES.get((language or "").split("-")[0].lower())
    if lang:
        hints.append(f"language: {lang}")
    if not hints:
        return text
    composed = f"{text}\n(Phone call; {'; '.join(hints)}.)"
    return composed[-ASSEMBLYAI_MAX_AGENT_CONTEXT_CHARS:]
