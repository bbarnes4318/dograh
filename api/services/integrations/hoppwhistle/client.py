"""Signed delivery of a finished call to Hoppwhistle.

Same scheme as Hoppwhistle's call-quality ingestion: HMAC-SHA256 over
``"{timestamp}.{raw body}"`` with the shared secret, sent as
``X-Hopwhistle-Signature: v1=<hex>`` beside ``X-Hopwhistle-Timestamp``.
Hoppwhistle refuses a timestamp more than five minutes off, and a repeat of
the same run is a no-op there, so one retry on a 5xx or a network error is
safe.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

SIGNATURE_HEADER = "X-Hopwhistle-Signature"
TIMESTAMP_HEADER = "X-Hopwhistle-Timestamp"
REPORT_PATH = "/api/internal/ai-agent/runs/complete"


def sign(secret: str, timestamp: str, body: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.{body}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v1={digest}"


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    status_code: int | None
    detail: str


async def deliver_report(
    *,
    base_url: str,
    secret: str,
    payload: dict[str, Any],
    timeout_seconds: float = 10.0,
    attempts: int = 2,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DeliveryResult:
    url = f"{base_url.rstrip('/')}{REPORT_PATH}"
    body = json.dumps(payload, separators=(",", ":"), default=str)
    last = DeliveryResult(ok=False, status_code=None, detail="not attempted")
    for attempt in range(1, attempts + 1):
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign(secret, timestamp, body),
        }
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, transport=transport
            ) as client:
                response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            last = DeliveryResult(
                ok=False, status_code=None, detail=f"request failed: {exc}"
            )
            logger.warning(f"Hoppwhistle report attempt {attempt} failed: {exc}")
            continue
        if 200 <= response.status_code < 300:
            return DeliveryResult(
                ok=True, status_code=response.status_code, detail="delivered"
            )
        last = DeliveryResult(
            ok=False,
            status_code=response.status_code,
            detail=response.text[:300],
        )
        # A 4xx will not change on retry: bad signature, unknown session, bad body.
        if response.status_code < 500:
            break
        logger.warning(
            f"Hoppwhistle report attempt {attempt}: HTTP {response.status_code}"
        )
    return last
