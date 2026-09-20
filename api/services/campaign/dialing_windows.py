"""Per-lead dialing windows and local-presence caller-ID selection.

A campaign schedule carried a single timezone for the whole list, so a New York
lead in a Pacific campaign got dialled at 7am their time. That is both a bad
first impression and, in the US, a TCPA problem: calling windows are defined in
the *called party's* local time, not the caller's.

This module resolves a lead's local timezone — from an explicit context
variable when the list carries one, otherwise from the North American area code
— and answers the two questions the dispatcher needs: may this lead be called
right now, and if not, when next.

The same area-code knowledge drives local presence: given a pool of caller IDs,
prefer one whose area code matches the lead's. People answer a local number far
more often than an out-of-state one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from api.services.campaign.nanp_timezones import NANP_AREA_CODE_TIMEZONES

# Context-variable keys a lead list can carry to state its own timezone.
LEAD_TIMEZONE_KEYS = ("timezone", "time_zone", "lead_timezone")

_NON_DIGITS = re.compile(r"\D")

# How far ahead to look for the next open slot. A week covers any weekly
# schedule; the extra day absorbs the case where today's slot has passed.
_MAX_LOOKAHEAD_DAYS = 8


def extract_nanp_area_code(phone_number: str | None) -> Optional[str]:
    """Return the 3-digit NANP area code for a number, or None.

    Accepts the shapes a lead list actually contains — ``+1 (555) 123-4567``,
    ``15551234567``, ``5551234567``. Anything that isn't a North American
    number returns None rather than guessing.
    """
    if not phone_number:
        return None
    digits = _NON_DIGITS.sub("", str(phone_number))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    area_code = digits[:3]
    # NANP area codes never start with 0 or 1.
    if area_code[0] in "01":
        return None
    return area_code


def timezone_for_phone_number(phone_number: str | None) -> Optional[str]:
    """Best-effort IANA timezone for a North American phone number."""
    area_code = extract_nanp_area_code(phone_number)
    if area_code is None:
        return None
    return NANP_AREA_CODE_TIMEZONES.get(area_code)


def is_valid_timezone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def resolve_lead_timezone(
    context_variables: Mapping[str, Any] | None,
    phone_number: str | None,
    fallback: str | None = None,
) -> Optional[str]:
    """Resolve the timezone to judge a lead's local time in.

    An explicit value on the lead always wins — a list that knows its own
    timezones shouldn't be second-guessed by an area-code table. Otherwise the
    area code decides, and failing that the campaign-level fallback.
    """
    for key in LEAD_TIMEZONE_KEYS:
        raw = (context_variables or {}).get(key)
        if raw and is_valid_timezone(str(raw)):
            return str(raw)

    from_area_code = timezone_for_phone_number(phone_number)
    if from_area_code:
        return from_area_code

    return fallback if is_valid_timezone(fallback) else None


def _slot_contains(slot: Mapping[str, Any], moment: datetime) -> bool:
    if slot.get("day_of_week") != moment.weekday():
        return False
    start = slot.get("start_time") or ""
    end = slot.get("end_time") or ""
    if not start or not end:
        return False
    return start <= moment.strftime("%H:%M") < end


def is_within_local_window(
    slots: list[Mapping[str, Any]] | None,
    timezone: str | None,
    now: datetime | None = None,
) -> bool:
    """Is it currently inside one of the slots, in the given timezone?

    Fails open — no slots, or a timezone that won't resolve, means "allowed",
    matching how campaign-level scheduling already behaves. A window is a
    restriction someone opted into; a lookup failure shouldn't quietly stop a
    campaign.
    """
    if not slots or not timezone:
        return True
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning(f"Unknown timezone '{timezone}' for dialing window; allowing")
        return True

    moment = (now or datetime.now(UTC)).astimezone(tz)
    return any(_slot_contains(slot, moment) for slot in slots)


def seconds_until_local_window(
    slots: list[Mapping[str, Any]] | None,
    timezone: str | None,
    now: datetime | None = None,
) -> Optional[float]:
    """Seconds until the next slot opens, or None if calling is allowed now.

    None also covers the fail-open cases, so a caller can read None as
    "dial it".
    """
    if is_within_local_window(slots, timezone, now):
        return None
    try:
        tz = ZoneInfo(timezone)  # type: ignore[arg-type]
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None

    start = (now or datetime.now(UTC)).astimezone(tz)
    best: Optional[datetime] = None
    for day_offset in range(_MAX_LOOKAHEAD_DAYS):
        day = start + timedelta(days=day_offset)
        for slot in slots or []:
            if slot.get("day_of_week") != day.weekday():
                continue
            raw_start = slot.get("start_time") or ""
            try:
                hour_text, minute_text = raw_start.split(":", 1)
                hour, minute = int(hour_text), int(minute_text)
            except (ValueError, TypeError):
                continue
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if candidate > start and (best is None or candidate < best):
                best = candidate
    if best is None:
        return None
    return max(0.0, (best - start).total_seconds())


@dataclass(frozen=True)
class DialingPolicy:
    """Campaign-level dialing rules, read from ``orchestrator_metadata``.

    Attributes:
        schedule_enabled: Whether calling windows are enforced at all.
        slots: Weekly ``{day_of_week, start_time, end_time}`` windows.
        timezone: Campaign fallback timezone, used when a lead's own can't be
            resolved.
        per_lead_timezone: Judge each lead's window in *their* local time rather
            than the campaign's single timezone.
        local_presence: Prefer a caller ID sharing the lead's area code.
        from_number_daily_cap: Max dials per caller ID per day, or None.
    """

    schedule_enabled: bool = False
    slots: tuple[Mapping[str, Any], ...] = ()
    timezone: Optional[str] = None
    per_lead_timezone: bool = True
    local_presence: bool = True
    from_number_daily_cap: Optional[int] = None


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def dialing_config(campaign: Any) -> DialingPolicy:
    """Read a campaign's dialing policy, falling back to sane defaults."""
    metadata = getattr(campaign, "orchestrator_metadata", None) or {}
    schedule = metadata.get("schedule_config") or {}
    dialing = metadata.get("dialing") or {}

    slots = schedule.get("slots") or []
    return DialingPolicy(
        schedule_enabled=bool(schedule.get("enabled", False)) and bool(slots),
        slots=tuple(slot for slot in slots if isinstance(slot, Mapping)),
        timezone=schedule.get("timezone") or "UTC",
        per_lead_timezone=bool(dialing.get("per_lead_timezone", True)),
        local_presence=bool(dialing.get("local_presence", True)),
        from_number_daily_cap=_positive_int(dialing.get("from_number_daily_cap")),
    )


def resolve_retry_delay_seconds(retry_config: Mapping[str, Any], attempt: int) -> int:
    """Seconds to wait before retry ``attempt`` (1-based).

    A no-answer retried two minutes later is close to worthless — the person is
    still away from the phone, and attempt two fails in the same daypart that
    attempt one already failed in. Two ways to spread them:

    - ``retry_delays_seconds``: an explicit per-attempt ladder, used as-is.
    - ``retry_delay_seconds`` plus ``daypart_shift_hours``: each further
      attempt lands that many hours later than the previous one, so it samples
      a different time of day.

    The ladder wins when present. Falling back to a bare ``retry_delay_seconds``
    with no shift reproduces the old fixed-delay behavior exactly.
    """
    attempt = max(1, int(attempt))

    ladder = retry_config.get("retry_delays_seconds")
    if isinstance(ladder, (list, tuple)) and ladder:
        index = min(attempt, len(ladder)) - 1
        try:
            return max(0, int(ladder[index]))
        except (TypeError, ValueError):
            pass

    try:
        base = max(0, int(retry_config.get("retry_delay_seconds", 120)))
    except (TypeError, ValueError):
        base = 120

    try:
        shift_hours = float(retry_config.get("daypart_shift_hours", 0) or 0)
    except (TypeError, ValueError):
        shift_hours = 0.0

    return base + int(max(0.0, shift_hours) * 3600 * (attempt - 1))


def rank_from_numbers_by_locality(
    from_numbers: Iterable[str], destination: str | None
) -> list[str]:
    """Order caller IDs by how local they look to the destination.

    Exact area-code match first, then same timezone, then the rest. Order
    within each tier is preserved, so a pool with nothing local comes back
    unchanged and the caller's own tie-breaking still applies.
    """
    numbers = list(from_numbers)
    destination_area = extract_nanp_area_code(destination)
    if destination_area is None:
        return numbers

    destination_tz = NANP_AREA_CODE_TIMEZONES.get(destination_area)
    positions = {number: index for index, number in enumerate(numbers)}

    def tier(number: str) -> int:
        area = extract_nanp_area_code(number)
        if area is None:
            return 2
        if area == destination_area:
            return 0
        if destination_tz and NANP_AREA_CODE_TIMEZONES.get(area) == destination_tz:
            return 1
        return 2

    return sorted(numbers, key=lambda number: (tier(number), positions[number]))
