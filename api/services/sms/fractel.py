"""FracTEL (api.fonestorm.com) SMS/MMS client.

Auth: POST /v2/auth -> token (cached ~23.5h per account).
Send: POST /v2/messages/send with a ``token`` header. Numbers are 10 digits,
no ``+1``. Transient failures (timeouts, 5xx, 429) are retried with backoff;
other 4xx responses are permanent and surface as ``FracTelConfigError``.

Errors carry a ``reason`` code plus the provider's HTTP status, error code and
error message so callers can log them. Credentials and tokens never appear in
exception text or logs.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import time
from typing import Any, Optional

import httpx
from loguru import logger

FRACTEL_BASE_URL = "https://api.fonestorm.com/v2"
REQUEST_TIMEOUT_SECS = 10.0
MAX_RETRIES = 3
TOKEN_TTL_SECS = 23.5 * 3600
TOKEN_EXPIRES_SECS = 86400
PROVIDER_TEXT_LIMIT = 300

# (username) -> (token, expires_at monotonic)
_token_cache: dict[str, tuple[str, float]] = {}
_rotation = itertools.count()

_FAILED_STATUSES = {"error", "errors", "fail", "failed", "failure"}


class FracTelError(Exception):
    """Transient or unknown failure sending via FracTEL."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "provider_error",
        status_code: Optional[int] = None,
        provider_code: Optional[str] = None,
        provider_message: Optional[str] = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_message = provider_message


class FracTelConfigError(FracTelError):
    """Permanent failure (bad credentials, unregistered/rejected DID, bad input)."""


def normalize_number(value: str) -> str:
    """Return a 10-digit US number from any common format, or raise ValueError."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"'{value}' is not a valid 10-digit US phone number")
    return digits


def mask_number(value: Optional[str]) -> str:
    """Mask a phone number for logs, keeping the last four digits."""
    digits = re.sub(r"\D", "", value or "")
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"


# Added to the end of every text, on top of any links configured on the tool.
DEFAULT_APPEND_LINKS = ["https://dialbrowser.com/distribution"]


def append_links(message: str, links: list[str]) -> str:
    """Add each link to the end of the message on its own line, skipping any
    link the message already contains."""
    missing = [link for link in links if link and link not in message]
    if not missing:
        return message
    return message.rstrip() + "\n" + "\n".join(missing)


def pick_from_number(from_numbers: list[str]) -> str:
    """Round-robin over the configured sender pool."""
    if not from_numbers:
        raise FracTelConfigError(
            "No sender numbers configured for this SMS tool",
            reason="sender_not_configured",
        )
    return from_numbers[next(_rotation) % len(from_numbers)]


def _json_body(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _first_str(container: Any, *keys: str) -> Optional[str]:
    if not isinstance(container, dict):
        return None
    for key in keys:
        value = container.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            if str(value).strip():
                return str(value)
    return None


def _provider_error_details(resp: Any) -> tuple[Optional[str], Optional[str]]:
    """Pull an error code and message out of a FracTEL response, if present."""
    body = _json_body(resp)
    if not isinstance(body, dict):
        text = (getattr(resp, "text", "") or "").strip()
        return None, text[:PROVIDER_TEXT_LIMIT] or None

    candidates = [body]
    for key in ("error", "errors", "data"):
        nested = body.get(key)
        if isinstance(nested, list) and nested:
            nested = nested[0]
        if isinstance(nested, dict):
            candidates.append(nested)

    code = message = None
    for candidate in candidates:
        code = code or _first_str(candidate, "code", "error_code", "status_code")
        message = message or _first_str(
            candidate, "message", "error_message", "detail", "description"
        )
    # A bare string under "error"/"errors" is the message itself.
    for key in ("error", "errors"):
        if not message and isinstance(body.get(key), str):
            message = body[key]
    return code, (message[:PROVIDER_TEXT_LIMIT] if message else None)


def _explicit_failure(body: Any) -> bool:
    """True when a 2xx response body still reports that the send failed."""
    if not isinstance(body, dict):
        return False
    if body.get("error") or body.get("errors"):
        return True
    data = body.get("data")
    containers = [body]
    if isinstance(data, dict):
        containers += [data, data.get("message")]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("status", "result"):
            value = container.get(key)
            if isinstance(value, str) and value.strip().lower() in _FAILED_STATUSES:
                return True
    return False


def _message_id(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, dict) and message.get("id"):
            return str(message["id"])
        messages = data.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            if messages[0].get("id"):
                return str(messages[0]["id"])
        if data.get("id"):
            return str(data["id"])
    return str(body["id"]) if body.get("id") else None


_TOKEN_KEYS = ("token", "auth_token", "access_token", "api_token")


def _find_token(body: Any, depth: int = 0) -> Optional[str]:
    """Find the auth token in a FracTEL auth response, wherever it is nested.

    The documented layout is ``data.auth.token``, but the live API has been
    seen returning 201 without it there, so search breadth-first for a
    token-named string key instead of relying on one path.
    """
    if depth > 5:
        return None
    if isinstance(body, dict):
        for key, value in body.items():
            if key.lower() in _TOKEN_KEYS and isinstance(value, str) and value:
                return value
        children = list(body.values())
    elif isinstance(body, list):
        children = body
    else:
        return None
    for child in children:
        found = _find_token(child, depth + 1)
        if found:
            return found
    return None


def _header_token(resp: Any) -> Optional[str]:
    headers = getattr(resp, "headers", None) or {}
    try:
        return headers.get("token") or headers.get("x-auth-token")
    except AttributeError:
        return None


def _layout(body: Any, depth: int = 0) -> Any:
    """Describe a JSON value's structure without any of its values."""
    if depth > 4:
        return "..."
    if isinstance(body, dict):
        return {k: _layout(v, depth + 1) for k, v in body.items()}
    if isinstance(body, list):
        return [_layout(body[0], depth + 1)] if body else []
    return type(body).__name__


async def _get_token(
    client: httpx.AsyncClient, username: str, password: str, *, force: bool = False
) -> str:
    cached = _token_cache.get(username)
    if cached and not force and cached[1] > time.monotonic():
        return cached[0]

    resp = await client.post(
        f"{FRACTEL_BASE_URL}/auth",
        json={
            "username": username,
            "password": password,
            "expires": TOKEN_EXPIRES_SECS,
        },
    )
    if resp.status_code in (400, 401, 403):
        code, message = _provider_error_details(resp)
        raise FracTelConfigError(
            f"FracTEL authentication failed ({resp.status_code})",
            reason="credentials_invalid",
            status_code=resp.status_code,
            provider_code=code,
            provider_message=message,
        )
    resp.raise_for_status()
    body = _json_body(resp)
    token = _find_token(body) or _header_token(resp)
    if not token:
        # Retrying cannot help: the same login returns the same body. Log the
        # body's layout (key names and value types only, never values) and
        # any error code/message FracTEL put in it.
        code, detail = _provider_error_details(resp)
        logger.error(
            f"FracTEL auth returned {resp.status_code} with no token; "
            f"provider_code={code} provider_message={detail!r} "
            f"response layout={_layout(body)}, "
            f"headers={sorted(getattr(resp, 'headers', {}) or {})}"
        )
        raise FracTelConfigError(
            "FracTEL auth response contained no token",
            reason="auth_failed",
            status_code=resp.status_code,
            provider_code=code,
            provider_message=detail,
        )
    _token_cache[username] = (token, time.monotonic() + TOKEN_TTL_SECS)
    return token


async def send_sms(
    *,
    username: str,
    password: str,
    from_number: str,
    to_number: str,
    message: str,
    media_url: Optional[str] = None,
) -> Optional[str]:
    """Send one SMS (or MMS when ``media_url`` is given). Returns FracTEL's id."""
    if not message or not message.strip():
        raise FracTelConfigError("Message text is empty", reason="empty_message")
    if not username or not password:
        raise FracTelConfigError(
            "FracTEL credentials are not configured", reason="credentials_missing"
        )
    try:
        sender = normalize_number(from_number)
    except ValueError:
        raise FracTelConfigError(
            "Configured sender number is not a valid 10-digit US number",
            reason="sender_invalid",
        )
    try:
        recipient = normalize_number(to_number)
    except ValueError:
        raise FracTelConfigError(
            "Recipient is not a valid 10-digit US phone number",
            reason="recipient_invalid",
        )

    payload: dict = {
        "fonenumber": sender,
        "to": [recipient],
        "message": message,
    }
    if media_url:
        payload["media"] = media_url

    last_error: Optional[FracTelError] = None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECS) as client:
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                await asyncio.sleep(2 ** (attempt - 1))
            try:
                token = await _get_token(client, username, password)
                resp = await client.post(
                    f"{FRACTEL_BASE_URL}/messages/send",
                    headers={"token": token},
                    json=payload,
                )
                if resp.status_code == 401:
                    _token_cache.pop(username, None)
                    code, detail = _provider_error_details(resp)
                    last_error = FracTelError(
                        "FracTEL token rejected",
                        reason="credentials_invalid",
                        status_code=401,
                        provider_code=code,
                        provider_message=detail,
                    )
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    code, detail = _provider_error_details(resp)
                    last_error = FracTelError(
                        f"FracTEL error {resp.status_code}",
                        reason="provider_unavailable",
                        status_code=resp.status_code,
                        provider_code=code,
                        provider_message=detail,
                    )
                    continue
                if resp.status_code >= 400:
                    code, detail = _provider_error_details(resp)
                    raise FracTelConfigError(
                        f"FracTEL rejected the message ({resp.status_code})",
                        reason="provider_rejected",
                        status_code=resp.status_code,
                        provider_code=code,
                        provider_message=detail,
                    )
                body = _json_body(resp)
                if _explicit_failure(body):
                    code, detail = _provider_error_details(resp)
                    raise FracTelConfigError(
                        f"FracTEL reported a failed send ({resp.status_code})",
                        reason="provider_rejected",
                        status_code=resp.status_code,
                        provider_code=code,
                        provider_message=detail,
                    )
                message_id = _message_id(body)
                if message_id is None:
                    # Accepted (2xx, no error in the body) but not in the
                    # documented shape; keep it visible for debugging.
                    logger.warning(
                        f"FracTEL accepted SMS with no message id "
                        f"(status={resp.status_code}, "
                        f"body={(getattr(resp, 'text', '') or '')[:PROVIDER_TEXT_LIMIT]})"
                    )
                logger.info(
                    f"FracTEL SMS sent (id={message_id}, "
                    f"from={sender}, to={mask_number(recipient)})"
                )
                return message_id
            except FracTelConfigError:
                raise
            except FracTelError as e:
                last_error = e
            except httpx.TimeoutException as e:
                last_error = FracTelError(
                    f"FracTEL request timed out ({type(e).__name__})",
                    reason="provider_unavailable",
                )
            except httpx.TransportError as e:
                last_error = FracTelError(
                    f"FracTEL connection failed ({type(e).__name__})",
                    reason="provider_unavailable",
                )
            except httpx.HTTPStatusError as e:
                last_error = FracTelError(
                    f"FracTEL error {e.response.status_code}",
                    reason="provider_unavailable",
                    status_code=e.response.status_code,
                )

    assert last_error is not None
    raise FracTelError(
        f"FracTEL send failed after retries: {last_error}",
        reason=last_error.reason,
        status_code=last_error.status_code,
        provider_code=last_error.provider_code,
        provider_message=last_error.provider_message,
    )
