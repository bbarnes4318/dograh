"""Vonage phone-number normalization.

Dograh stores and passes PSTN numbers in canonical E.164 form (``+14155551212``,
see ``api.utils.telephony_address``). The Vonage Voice API instead expects
international digits **without** the ``+`` (``14155551212``) in every ``phone``
endpoint, and sends numbers the same way in its webhooks.

This module is the single place that converts between the two. Convert only at
the provider boundary: outbound requests go through :func:`to_vonage_number`,
inbound webhook numbers go through :func:`from_vonage_number`.
"""

from __future__ import annotations

import re
from typing import Optional

_FORMATTING_RE = re.compile(r"[\s\-\.\(\)]")
_DIGITS_RE = re.compile(r"^\d+$")

# E.164 caps a number at 15 digits; 8 is the shortest real-world international
# number we accept (matches ``api.utils.telephony_address``).
_MIN_E164_DIGITS = 8
_MAX_E164_DIGITS = 15


class VonagePhoneNumberError(ValueError):
    """Raised when a value cannot be turned into a dialable Vonage number."""


def _is_nanp_national(digits: str) -> bool:
    """A 10-digit North American number with valid area and exchange codes."""
    return len(digits) == 10 and digits[0] in "23456789" and digits[3] in "23456789"


def to_vonage_number(raw: Optional[str], *, field: str = "phone number") -> str:
    """Return ``raw`` as international digits without ``+`` for the Voice API.

    Accepts E.164 (``+14155551212``), bare international digits
    (``14155551212``) and common formatting (``+1 (415) 555-1212``). A bare
    10-digit number is accepted only when it is a valid NANP number, in which
    case it is assumed to be US/Canada and ``1`` is prefixed. Anything else
    that is not unambiguously an international number raises
    :class:`VonagePhoneNumberError` instead of producing a malformed number.
    """
    if raw is None or not str(raw).strip():
        raise VonagePhoneNumberError(f"Vonage {field} is empty")

    value = str(raw).strip()
    if value.lower().startswith(("sip:", "sips:")) or "@" in value:
        raise VonagePhoneNumberError(
            f"Vonage {field} must be a PSTN number, got a SIP address"
        )

    has_plus = value.startswith("+")
    digits = _FORMATTING_RE.sub("", value[1:] if has_plus else value)
    if digits.startswith("00") and not has_plus:
        # International dialing prefix, e.g. 0044... -> 44...
        digits = digits[2:]
        has_plus = True

    if not _DIGITS_RE.fullmatch(digits or "x"):
        raise VonagePhoneNumberError(
            f"Vonage {field} contains invalid characters: {value!r}"
        )

    if not has_plus and len(digits) == 10:
        if not _is_nanp_national(digits):
            raise VonagePhoneNumberError(
                f"Vonage {field} {value!r} is ambiguous; use E.164 format "
                "(e.g. +14155551212)"
            )
        digits = f"1{digits}"
    elif not has_plus and len(digits) < 11:
        raise VonagePhoneNumberError(
            f"Vonage {field} {value!r} is not an international number; use "
            "E.164 format (e.g. +14155551212)"
        )

    if not (_MIN_E164_DIGITS <= len(digits) <= _MAX_E164_DIGITS):
        raise VonagePhoneNumberError(
            f"Vonage {field} {value!r} must have {_MIN_E164_DIGITS}-"
            f"{_MAX_E164_DIGITS} digits including the country code"
        )
    if digits[0] == "0":
        raise VonagePhoneNumberError(
            f"Vonage {field} {value!r} must start with a country code"
        )

    return digits


def to_e164(raw: Optional[str], *, field: str = "phone number") -> str:
    """Canonical Dograh form (``+`` + digits) of a Vonage-acceptable number."""
    return f"+{to_vonage_number(raw, field=field)}"


def from_vonage_number(raw: Optional[str]) -> str:
    """Convert a number from a Vonage webhook into Dograh's canonical form.

    Vonage sends PSTN numbers as international digits without ``+``. Values
    that are not PSTN numbers (``"Unknown"``, SIP URIs, app user names) are
    returned unchanged so they are never mangled into fake numbers.
    """
    if raw is None:
        return ""
    value = str(raw).strip()
    if not value:
        return ""
    try:
        return to_e164(value)
    except VonagePhoneNumberError:
        return value
