"""Delivery of a finished call to Hoppwhistle's Holiday Voices engine route.

Authenticated exactly like the tools: ``Authorization: Bearer <engine key>``
from the organization's own bearer credential, which Hoppwhistle maps to the
store's tenant. Hoppwhistle treats a repeat report for a completed session as
a no-op, so one retry on a 5xx or a network error is safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    status_code: int | None
    detail: str


async def deliver_report(
    *,
    url: str,
    auth_headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float = 10.0,
    attempts: int = 2,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DeliveryResult:
    body = json.dumps(payload, separators=(",", ":"), default=str)
    headers = {"Content-Type": "application/json", **auth_headers}
    last = DeliveryResult(ok=False, status_code=None, detail="not attempted")
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, transport=transport
            ) as client:
                response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            last = DeliveryResult(False, None, f"request failed: {exc}")
            logger.warning(f"Holiday Voices report attempt {attempt} failed: {exc}")
            continue
        if 200 <= response.status_code < 300:
            return DeliveryResult(True, response.status_code, "delivered")
        last = DeliveryResult(False, response.status_code, response.text[:300])
        if response.status_code < 500:
            break  # bad key, unknown session, bad body: a retry will not change it
        logger.warning(
            f"Holiday Voices report attempt {attempt}: HTTP {response.status_code}"
        )
    return last
