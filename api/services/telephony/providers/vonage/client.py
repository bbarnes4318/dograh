"""Minimal Vonage Voice API client used by the provider and its strategies.

Retry policy (bounded, and only where a retry cannot duplicate a side effect):

* ``POST /v1/calls`` (creates a call) is retried only on HTTP 429 and on
  connection failures that happen before the request reached Vonage. A 5xx or
  read timeout after sending might mean the call was created, so it is never
  retried — retrying could ring a consumer twice.
* ``GET`` and the idempotent ``PUT /v1/calls/{uuid}`` actions (hangup,
  transfer) are retried on 429, 5xx and network errors.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger

from .auth import generate_api_jwt
from .errors import VonageAPIError, VonageErrorCategory, classify_vonage_error

VONAGE_API_BASE_URL = "https://api.nexmo.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)
MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 5.0


def _new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return min(float(retry_after), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), _MAX_RETRY_AFTER_SECONDS)


async def _read_body(response: aiohttp.ClientResponse) -> Any:
    text = await response.text()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


class VonageVoiceClient:
    """Thin async wrapper around the Voice API with JWT auth."""

    def __init__(
        self,
        application_id: Optional[str],
        private_key: Optional[str],
        *,
        base_url: str = VONAGE_API_BASE_URL,
    ):
        self._application_id = application_id
        self._private_key = private_key
        self._base_url = base_url

    def _headers(self) -> Dict[str, str]:
        token = generate_api_jwt(self._application_id, self._private_key)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        operation: str,
        expected: tuple[int, ...] = (200,),
        side_effect_safe: bool = True,
    ) -> tuple[int, Any]:
        """Send a request; return ``(status, body)`` or raise VonageAPIError.

        ``side_effect_safe=False`` restricts retries to cases where Vonage
        cannot have acted on the request (429, connect errors).
        """
        url = f"{self._base_url}{path}"
        last_error: Optional[VonageAPIError] = None
        for attempt in range(MAX_ATTEMPTS):
            headers = self._headers()
            retry_after: Optional[str] = None
            try:
                async with _new_session() as session:
                    async with session.request(
                        method, url, json=json_body, headers=headers
                    ) as response:
                        body = await _read_body(response)
                        if response.status in expected:
                            return response.status, body
                        retry_after = response.headers.get("Retry-After")
                        last_error = classify_vonage_error(
                            response.status, body, operation=operation
                        )
            except aiohttp.ClientConnectorError:
                # Never reached Vonage: always safe to retry.
                last_error = VonageAPIError(
                    VonageErrorCategory.NETWORK_ERROR, operation=operation
                )
            except asyncio.TimeoutError:
                last_error = VonageAPIError(
                    VonageErrorCategory.NETWORK_TIMEOUT, operation=operation
                )
                if not side_effect_safe:
                    raise last_error from None
            except aiohttp.ClientError as exc:
                last_error = VonageAPIError(
                    VonageErrorCategory.NETWORK_ERROR,
                    operation=operation,
                    provider_detail=type(exc).__name__,
                )
                if not side_effect_safe:
                    raise last_error from None

            retryable = last_error.category in (
                (VonageErrorCategory.RATE_LIMITED, VonageErrorCategory.NETWORK_ERROR)
                if not side_effect_safe
                else (
                    VonageErrorCategory.RATE_LIMITED,
                    VonageErrorCategory.PROVIDER_UNAVAILABLE,
                    VonageErrorCategory.NETWORK_ERROR,
                    VonageErrorCategory.NETWORK_TIMEOUT,
                )
            )
            if not retryable or attempt == MAX_ATTEMPTS - 1:
                raise last_error
            delay = _retry_delay(attempt, retry_after)
            logger.warning(
                f"provider=vonage op={operation} attempt={attempt + 1} "
                f"category={last_error.category.value} retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)

        raise last_error  # pragma: no cover - loop always returns or raises

    async def create_call(self, body: Dict[str, Any]) -> Dict[str, Any]:
        _, data = await self.request(
            "POST",
            "/v1/calls",
            json_body=body,
            operation="call creation",
            expected=(200, 201),
            side_effect_safe=False,
        )
        if not isinstance(data, dict) or not data.get("uuid"):
            raise VonageAPIError(
                VonageErrorCategory.REQUEST_REJECTED,
                operation="call creation",
                provider_detail="response did not include a call uuid",
            )
        return data

    async def get_call(self, call_uuid: str) -> Dict[str, Any]:
        _, data = await self.request(
            "GET", f"/v1/calls/{call_uuid}", operation="call lookup"
        )
        return data if isinstance(data, dict) else {}

    async def hangup(self, call_uuid: str) -> bool:
        """Hang up a leg. Returns True if it is (now) gone."""
        try:
            await self.request(
                "PUT",
                f"/v1/calls/{call_uuid}",
                json_body={"action": "hangup"},
                operation="hangup",
                expected=(200, 204),
            )
        except VonageAPIError as exc:
            if exc.vonage_status in (404, 400):
                # 404: unknown leg; 400: leg already completed.
                logger.debug(
                    f"provider=vonage call_uuid={call_uuid} hangup: leg already gone"
                )
                return True
            raise
        return True

    async def transfer_to_ncco(self, call_uuid: str, ncco: list) -> None:
        await self.request(
            "PUT",
            f"/v1/calls/{call_uuid}",
            json_body={
                "action": "transfer",
                "destination": {"type": "ncco", "ncco": ncco},
            },
            operation="call transfer",
            expected=(200, 204),
        )
