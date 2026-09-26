"""Do-not-call suppression.

Nothing in the platform consulted a suppression list. Every uploaded row was
dialled as given, and ``DNC`` existed only as a *disposition* — a label written
onto a call that had already happened, which no later dial ever read. A caller
who said "take me off your list" got called again by the next campaign. That is
a compliance exposure, not a missing convenience.

Suppression is checked at dial time rather than at upload time, so a number
added while a campaign is mid-flight stops the calls still queued against it.

**Matching.** The same person shows up across lists as ``5551234567``,
``(555) 123-4567`` and ``+1 555 123 4567``, so every number is reduced to one
canonical key before it is stored or compared. North American numbers collapse
onto ``+1XXXXXXXXXX`` whether or not the country code was given. Numbers
outside the NANP are only matched when they carry their country code, because
a bare national number is ambiguous without knowing the country — an 8-digit
string is a valid subscriber number in several.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Where a lead's phone number is found on a queued run's context.
PHONE_NUMBER_CONTEXT_KEYS = ("phone_number", "phoneNumber", "to_number", "number")

# How an entry came to be on the list. Recorded so an operator can tell a
# caller's own request from a bulk import, which matters when one is disputed.
SOURCE_MANUAL = "manual"
SOURCE_IMPORT = "import"
SOURCE_DISPOSITION = "disposition"
SOURCE_AGENT = "agent"

DNC_SOURCES = (SOURCE_MANUAL, SOURCE_IMPORT, SOURCE_DISPOSITION, SOURCE_AGENT)

# Dispositions that mean the caller asked not to be contacted again. A call
# ending this way adds the number to the list without anyone intervening.
SUPPRESSING_DISPOSITIONS = frozenset({"DNC"})

_NON_DIGITS = re.compile(r"\D")

# E.164 allows 15 digits; below 8 it isn't a dialable subscriber number.
_MIN_E164_DIGITS = 8
_MAX_E164_DIGITS = 15


def normalize_dnc_number(raw: object) -> str | None:
    """Reduce a phone number to the key the suppression list is stored under.

    Returns ``None`` for anything that isn't a dialable number, so callers can
    tell "not on the list" from "not a number at all" and report the difference
    on an import.

    Both writes and reads go through here. A number is only ever suppressed if
    the two sides agree on its key, so nothing may normalize on its own.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    digits = _NON_DIGITS.sub("", text)
    if not digits:
        return None

    # North American numbers, with or without the country code. Area codes
    # never start with 0 or 1, so a bare 10-digit number is unambiguous.
    if len(digits) == 11 and digits.startswith("1") and digits[1] not in "01":
        return f"+{digits}"
    if len(digits) == 10 and digits[0] not in "01":
        return f"+1{digits}"

    if _MIN_E164_DIGITS <= len(digits) <= _MAX_E164_DIGITS:
        return f"+{digits}"

    return None


def normalize_dnc_numbers(raws: Iterable[object]) -> tuple[list[str], list[str]]:
    """Normalize many numbers at once, de-duplicated and order-preserving.

    Returns the accepted keys and the inputs that could not be parsed, so a
    bulk import can report exactly which rows it ignored instead of silently
    dropping them.
    """
    accepted: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    for raw in raws:
        key = normalize_dnc_number(raw)
        if key is None:
            text = "" if raw is None else str(raw).strip()
            if text:
                rejected.append(text)
            continue
        if key in seen:
            continue
        seen.add(key)
        accepted.append(key)

    return accepted, rejected


def phone_number_from_context(
    context_variables: Mapping[str, object] | None,
) -> str | None:
    """Pull the lead's phone number off a queued run's context variables."""
    for key in PHONE_NUMBER_CONTEXT_KEYS:
        value = (context_variables or {}).get(key)
        if value:
            return str(value)
    return None


def counterparty_number(
    initial_context: Mapping[str, object] | None,
    call_type: object = None,
) -> str | None:
    """The *other party's* number on a call — the one a DNC request is about.

    Which key holds it depends on direction: on an outbound call the person is
    the number we dialled, on an inbound call they are the number that dialled
    us. Suppressing the wrong one would add our own caller ID to the list and
    quietly kill the campaign using it.

    ``call_type`` is preferred over the context's ``direction``, which some
    providers leave unset on outbound calls.
    """
    context = initial_context or {}
    direction = str(call_type or context.get("direction") or "outbound").lower()

    if "inbound" in direction:
        keys = ("caller_number", "from_number")
    else:
        keys = ("called_number", *PHONE_NUMBER_CONTEXT_KEYS)

    for key in keys:
        value = context.get(key)
        if value:
            return str(value)
    return None


def disposition_requests_suppression(disposition: object) -> bool:
    """Whether a call's final disposition means "never call this number again"."""
    if not disposition:
        return False
    return str(disposition).strip().upper() in SUPPRESSING_DISPOSITIONS
