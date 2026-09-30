"""FracTEL (api.fonestorm.com) SMS/MMS client.

Auth: POST /v2/auth -> token (cached ~23.5h per account).
Send: POST /v2/messages/send with a ``token`` header. Numbers are 10 digits,
no ``+1``. Transient failures (timeouts, 5xx, 429) are retried with backoff;
other 4xx responses are permanent and surface as ``FracTelConfigError``.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import time
from typing import Optional

import httpx
from loguru import logger

FRACTEL_BASE_URL = "https://api.fonestorm.com/v2"
REQUEST_TIMEOUT_SECS = 10.0
MAX_RETRIES = 3
TOKEN_TTL_SECS = 23.5 * 3600
TOKEN_EXPIRES_SECS = 86400

# (username) -> (token, expires_at monotonic)
_token_cache: dict[str, tuple[str, float]] = {}
_rotation = itertools.count()


class FracTelError(Exception):
    """Transient or unknown failure sending via FracTEL."""


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


def pick_from_number(from_numbers: list[str]) -> str:
    """Round-robin over the configured sender pool."""
    if not from_numbers:
        raise FracTelConfigError("No sender numbers configured for this SMS tool")
    return from_numbers[next(_rotation) % len(from_numbers)]


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
        raise FracTelConfigError(f"FracTEL authentication failed ({resp.status_code})")
    resp.raise_for_status()
    token = (((resp.json() or {}).get("data") or {}).get("auth") or {}).get("token")
    if not token:
        raise FracTelError("FracTEL auth response contained no token")
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
        raise FracTelConfigError("Message text is empty")
    if not username or not password:
        raise FracTelConfigError("FracTEL credentials are not configured")

    payload: dict = {
        "fonenumber": normalize_number(from_number),
        "to": [normalize_number(to_number)],
        "message": message,
    }
    if media_url:
        payload["media"] = media_url

    last_error: Optional[Exception] = None
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
                    last_error = FracTelError("FracTEL token rejected")
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = FracTelError(f"FracTEL error {resp.status_code}")
                    continue
                if resp.status_code >= 400:
                    raise FracTelConfigError(
                        f"FracTEL rejected the message ({resp.status_code}): "
                        f"{resp.text[:200]}"
                    )
                message_id = (
                    ((resp.json() or {}).get("data") or {}).get("message") or {}
                ).get("id")
                logger.info(f"FracTEL SMS sent (id={message_id})")
                return message_id
            except FracTelConfigError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = e
            except httpx.HTTPStatusError as e:
                last_error = e

    raise FracTelError(f"FracTEL send failed after retries: {last_error}")
