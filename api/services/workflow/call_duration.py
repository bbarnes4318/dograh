"""Reconcile a run's call duration with the duration the carrier reports.

``usage_info.call_duration_seconds`` is normally the Pipecat pipeline's wall
time. When the pipeline never ran (the media websocket was refused or never
connected) or its timer never started, that value is missing or 0 even though
the carrier billed a connected call. The carrier's completed-status callback
carries the real talk time, so it is kept alongside as
``telephony_duration_seconds`` and used whenever the pipeline has nothing.
"""

from datetime import UTC, datetime
from typing import Any, Iterable

CALL_DURATION_KEY = "call_duration_seconds"
TELEPHONY_DURATION_KEY = "telephony_duration_seconds"
_ANSWERED_STATUSES = frozenset({"in-progress", "answered"})


def parse_duration_seconds(value: Any) -> float:
    """Parse a provider duration (``"42"``, ``42``, ``"42.5"``) into seconds."""
    if value in (None, ""):
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if parsed != parsed or parsed < 0:  # NaN or negative
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def _fill_call_duration(usage_info: dict) -> dict:
    telephony = parse_duration_seconds(usage_info.get(TELEPHONY_DURATION_KEY))
    if telephony > 0 and parse_duration_seconds(usage_info.get(CALL_DURATION_KEY)) <= 0:
        usage_info[CALL_DURATION_KEY] = telephony
    return usage_info


def apply_telephony_duration(usage_info: dict | None, seconds: Any) -> dict:
    """Return ``usage_info`` with the carrier duration recorded and, when the
    pipeline measured nothing, used as the call duration."""
    merged = dict(usage_info or {})
    telephony = parse_duration_seconds(seconds)
    if telephony <= 0:
        return merged
    merged[TELEPHONY_DURATION_KEY] = telephony
    return _fill_call_duration(merged)


def carry_over_telephony_duration(existing: dict | None, new: dict) -> dict:
    """Keep a carrier duration already stored on the run when ``new`` replaces
    ``usage_info`` wholesale (the pipeline writes its usage after, or racing
    with, the carrier's completed callback)."""
    merged = dict(new)
    if TELEPHONY_DURATION_KEY not in merged and existing:
        if TELEPHONY_DURATION_KEY in existing:
            merged[TELEPHONY_DURATION_KEY] = existing[TELEPHONY_DURATION_KEY]
    return _fill_call_duration(merged)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def telephony_duration_from_callbacks(callbacks: Iterable[Any]) -> float:
    """Call duration from the run's logged telephony status callbacks.

    Uses the largest duration a carrier reported. Carriers that report none
    on hangup (Telnyx, ARI) fall back to the time between the first answered
    callback and the completed one.
    """
    best: float = 0
    answered_at: datetime | None = None
    completed_at: datetime | None = None
    for callback in callbacks or []:
        if not isinstance(callback, dict):
            continue
        best = max(best, parse_duration_seconds(callback.get("duration")))
        status = str(callback.get("status") or "").lower()
        timestamp = _parse_timestamp(callback.get("timestamp"))
        if timestamp is None:
            continue
        if status in _ANSWERED_STATUSES and answered_at is None:
            answered_at = timestamp
        elif status == "completed" and completed_at is None:
            completed_at = timestamp

    if best > 0:
        return best
    if answered_at and completed_at and completed_at > answered_at:
        return int(round((completed_at - answered_at).total_seconds()))
    return 0
