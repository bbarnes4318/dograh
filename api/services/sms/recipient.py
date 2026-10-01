"""Resolve the SMS recipient from the call a tool is running on.

The recipient of a Send SMS tool is always the customer on the active call,
never a number the LLM supplies. Call setup records both parties in the run's
``initial_context``:

* outbound (campaign dispatcher, ``/telephony/initiate-call``, public agent
  API): ``called_number`` is the number that was dialed, i.e. the customer.
* inbound (telephony webhooks, ARI, agent-stream): ``direction == "inbound"``
  and ``caller_number`` is the customer; ``called_number`` is our own DID.

``snapshot_call_parties`` copies those keys when the engine for a call is
built, before a pre-call fetch can merge external data into the call context,
so each engine owns an immutable view of who it is talking to.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional

from api.services.sms.fractel import normalize_number

CALL_PARTY_KEYS = ("direction", "caller_number", "called_number", "phone_number")


class RecipientResolutionError(Exception):
    """The active call has no usable customer number to text."""

    def __init__(self, message: str, *, source_key: Optional[str] = None):
        super().__init__(message)
        self.source_key = source_key


def snapshot_call_parties(
    call_context_vars: Optional[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Return a read-only copy of the call-party fields of a call context."""
    context = call_context_vars or {}
    return MappingProxyType(
        {key: context[key] for key in CALL_PARTY_KEYS if context.get(key)}
    )


def resolve_call_recipient(call_parties: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(recipient, source_key)`` for the customer on the call.

    The recipient is a 10-digit US number, the format FracTEL expects.
    Raises ``RecipientResolutionError`` when the call has no customer number
    or it is not a valid US number.
    """
    if str(call_parties.get("direction") or "").lower() == "inbound":
        candidates = ("caller_number",)
    else:
        # phone_number covers runs created before called_number was recorded.
        candidates = ("called_number", "phone_number")

    for key in candidates:
        raw = call_parties.get(key)
        if not raw:
            continue
        try:
            return normalize_number(str(raw)), key
        except ValueError:
            raise RecipientResolutionError(
                "The customer's number on this call is not a valid 10-digit "
                "US phone number",
                source_key=key,
            )

    raise RecipientResolutionError(
        "No customer phone number is available for this call"
    )
