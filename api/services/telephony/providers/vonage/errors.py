"""Classified Vonage API errors.

Vonage returns RFC 7807-style problem documents (``type``/``title``/
``detail``/``invalid_parameters``). This module turns an HTTP status plus that
body into a :class:`VonageAPIError` with a stable ``category`` so callers (and
operators reading logs) can tell an invalid credential from a rejected
destination, a rate limit, or an outage.

``VonageAPIError`` subclasses ``HTTPException`` because the outbound call
routes surface provider failures directly to the caller. The HTTP status is
mapped to something meaningful from *Dograh's* point of view: a Vonage 401
means our stored credentials are wrong (a 400 for the Dograh user, never a 401
that would look like their Dograh session expired).

Error messages never include request headers, JWTs or any credential.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional

from fastapi import HTTPException


class VonageErrorCategory(str, Enum):
    INVALID_CREDENTIALS = "invalid_credentials"
    MALFORMED_PRIVATE_KEY = "malformed_private_key"
    INVALID_APPLICATION_ID = "invalid_application_id"
    NUMBER_NOT_AUTHORIZED = "number_not_authorized"
    INVALID_NUMBER = "invalid_number"
    DESTINATION_REJECTED = "destination_rejected"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_ERROR = "network_error"
    NOT_CONFIGURED = "not_configured"
    CALL_NOT_FOUND = "call_not_found"
    REQUEST_REJECTED = "request_rejected"


_MESSAGES = {
    VonageErrorCategory.INVALID_CREDENTIALS: (
        "Vonage rejected the application credentials. Check the Application ID "
        "and that the private key belongs to that application."
    ),
    VonageErrorCategory.MALFORMED_PRIVATE_KEY: (
        "The Vonage private key could not be used to sign a request. Paste the "
        "full PEM key including the BEGIN/END lines."
    ),
    VonageErrorCategory.INVALID_APPLICATION_ID: (
        "Vonage could not find the configured Application ID."
    ),
    VonageErrorCategory.NUMBER_NOT_AUTHORIZED: (
        "The caller ID is not authorized for this Vonage account/application. "
        "Make sure the number is owned by the account and linked to the "
        "application."
    ),
    VonageErrorCategory.INVALID_NUMBER: "Vonage rejected the phone number format.",
    VonageErrorCategory.DESTINATION_REJECTED: (
        "Vonage refused to call the destination number."
    ),
    VonageErrorCategory.INSUFFICIENT_BALANCE: (
        "The Vonage account has insufficient balance or calling is restricted."
    ),
    VonageErrorCategory.RATE_LIMITED: "Vonage rate limit exceeded; retry later.",
    VonageErrorCategory.PROVIDER_UNAVAILABLE: "Vonage is temporarily unavailable.",
    VonageErrorCategory.NETWORK_TIMEOUT: "Timed out talking to the Vonage API.",
    VonageErrorCategory.NETWORK_ERROR: "Could not reach the Vonage API.",
    VonageErrorCategory.NOT_CONFIGURED: "The Vonage configuration is incomplete.",
    VonageErrorCategory.CALL_NOT_FOUND: "Vonage could not find the call.",
    VonageErrorCategory.REQUEST_REJECTED: "Vonage rejected the request.",
}

# Dograh-facing HTTP status for each category.
_HTTP_STATUS = {
    VonageErrorCategory.INVALID_CREDENTIALS: 400,
    VonageErrorCategory.MALFORMED_PRIVATE_KEY: 400,
    VonageErrorCategory.INVALID_APPLICATION_ID: 400,
    VonageErrorCategory.NUMBER_NOT_AUTHORIZED: 400,
    VonageErrorCategory.INVALID_NUMBER: 400,
    VonageErrorCategory.DESTINATION_REJECTED: 400,
    VonageErrorCategory.INSUFFICIENT_BALANCE: 402,
    VonageErrorCategory.RATE_LIMITED: 429,
    VonageErrorCategory.PROVIDER_UNAVAILABLE: 502,
    VonageErrorCategory.NETWORK_TIMEOUT: 504,
    VonageErrorCategory.NETWORK_ERROR: 502,
    VonageErrorCategory.NOT_CONFIGURED: 400,
    VonageErrorCategory.CALL_NOT_FOUND: 404,
    VonageErrorCategory.REQUEST_REJECTED: 400,
}

RETRYABLE_CATEGORIES = frozenset(
    {
        VonageErrorCategory.RATE_LIMITED,
        VonageErrorCategory.PROVIDER_UNAVAILABLE,
        VonageErrorCategory.NETWORK_TIMEOUT,
        VonageErrorCategory.NETWORK_ERROR,
    }
)

_MAX_PROVIDER_TEXT = 300


class VonageAPIError(HTTPException):
    """A classified, credential-free Vonage failure."""

    def __init__(
        self,
        category: VonageErrorCategory,
        *,
        vonage_status: Optional[int] = None,
        provider_detail: Optional[str] = None,
        operation: str = "request",
    ):
        self.category = category
        self.vonage_status = vonage_status
        self.provider_detail = (provider_detail or "")[:_MAX_PROVIDER_TEXT]
        self.operation = operation
        message = f"Vonage {operation} failed ({category.value}): {_MESSAGES[category]}"
        if self.provider_detail:
            message = f"{message} Vonage said: {self.provider_detail}"
        super().__init__(status_code=_HTTP_STATUS[category], detail=message)

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    def __str__(self) -> str:  # HTTPException's repr includes the status code
        return self.detail


def _provider_text(body: Any) -> str:
    if isinstance(body, Mapping):
        parts = [str(body.get(k)) for k in ("title", "detail") if body.get(k)]
        invalid = body.get("invalid_parameters")
        if isinstance(invalid, list):
            for item in invalid[:3]:
                if isinstance(item, Mapping):
                    parts.append(f"{item.get('name')}: {item.get('reason')}")
        if not parts and body.get("error_title"):
            parts.append(str(body["error_title"]))
        return " - ".join(parts)
    if body is None:
        return ""
    return str(body)


def classify_vonage_error(
    status: int, body: Any, *, operation: str = "request"
) -> VonageAPIError:
    """Map a non-success Vonage response to a :class:`VonageAPIError`."""
    text = _provider_text(body)
    haystack = (
        f"{text} {body.get('type', '') if isinstance(body, Mapping) else ''}".lower()
    )

    if status == 401:
        category = VonageErrorCategory.INVALID_CREDENTIALS
    elif status == 402 or any(
        k in haystack for k in ("balance", "credit", "payment", "funds")
    ):
        category = VonageErrorCategory.INSUFFICIENT_BALANCE
    elif status == 429:
        category = VonageErrorCategory.RATE_LIMITED
    elif status >= 500:
        category = VonageErrorCategory.PROVIDER_UNAVAILABLE
    elif status == 403:
        category = VonageErrorCategory.NUMBER_NOT_AUTHORIZED
    elif status == 404:
        category = (
            VonageErrorCategory.INVALID_APPLICATION_ID
            if "application" in haystack
            else VonageErrorCategory.CALL_NOT_FOUND
        )
    elif status in (400, 422):
        if "application" in haystack:
            category = VonageErrorCategory.INVALID_APPLICATION_ID
        elif "from" in haystack and any(
            k in haystack for k in ("not allowed", "unauthorized", "not owned")
        ):
            category = VonageErrorCategory.NUMBER_NOT_AUTHORIZED
        elif any(k in haystack for k in ("number", "msisdn", "e.164", "to.")):
            category = VonageErrorCategory.INVALID_NUMBER
        elif any(k in haystack for k in ("barred", "blocked", "destination")):
            category = VonageErrorCategory.DESTINATION_REJECTED
        else:
            category = VonageErrorCategory.REQUEST_REJECTED
    else:
        category = VonageErrorCategory.REQUEST_REJECTED

    return VonageAPIError(
        category, vonage_status=status, provider_detail=text, operation=operation
    )
