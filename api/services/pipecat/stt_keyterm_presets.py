"""Opt-in STT vocabulary presets, selected per workflow.

A workflow lists presets in its configuration under ``stt_keyterm_presets``
(e.g. ``["insurance"]``); their terms are appended to the workflow's own
``dictionary`` words and sent to STT providers that support keyterm boosting.
Nothing is applied by default, so insurance wording never biases a real
estate or Holiday Voices agent. Lists stay short on purpose: boosting many
terms can hurt recognition of ordinary speech.
"""

from __future__ import annotations

KEYTERM_PRESETS: dict[str, tuple[str, ...]] = {
    "insurance": (
        "Final Expense",
        "Medicare",
        "burial insurance",
        "funeral coverage",
        "life insurance",
        "licensed insurance agent",
        "beneficiary",
        "premium",
        "policy",
        "coverage",
        "application",
        "qualification",
    ),
    "medicare": (
        "Medicare",
        "Medicare Advantage",
        "Medicare Supplement",
        "Medigap",
        "Part D",
        "Medicaid",
        "licensed insurance agent",
        "enrollment",
    ),
    "real_estate": (
        "listing",
        "pre-approved",
        "mortgage",
        "closing",
        "escrow",
        "open house",
        "square feet",
        "realtor",
    ),
}


def keyterms_for_presets(presets) -> list[str]:
    """Terms for the named presets, in order, without duplicates."""
    if isinstance(presets, str):
        presets = [p.strip() for p in presets.split(",")]
    if not isinstance(presets, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for name in presets:
        for term in KEYTERM_PRESETS.get(str(name).strip().lower(), ()):
            if term.lower() not in seen:
                seen.add(term.lower())
                out.append(term)
    return out
