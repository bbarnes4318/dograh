"""Outbound calls: Voice API request shape, JWT auth, numbers, errors."""

import asyncio
import json
from typing import Any, List, Optional
from unittest.mock import AsyncMock, patch

import aiohttp
import jwt
import pytest

from api.services.telephony.providers.vonage import client as client_module
from api.services.telephony.providers.vonage.errors import (
    VonageAPIError,
    VonageErrorCategory,
    classify_vonage_error,
)

APPLICATION_ID = "aaaaaaaa-bbbb-cccc-dddd-0123456789ab"
WEBHOOK = "https://dograh.test/api/v1/telephony/ncco?workflow_id=7&workflow_run_id=123&organization_id=11"


class _FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: Optional[dict] = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self):
        if self._body is None:
            return ""
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeVonageHTTP:
    """Records requests; replies from a queue of responses or exceptions."""

    def __init__(self, replies: List[Any]):
        self.replies = list(replies)
        self.requests: List[dict] = []

    def session_factory(self, *args, **kwargs):
        fake = self

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(self, method, url, json=None, headers=None, **kw):
                fake.requests.append(
                    {"method": method, "url": url, "json": json, "headers": headers}
                )
                reply = fake.replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply

        return _Session()


@pytest.fixture
def fake_http(monkeypatch):
    def _install(*replies):
        fake = FakeVonageHTTP(list(replies))
        monkeypatch.setattr(client_module, "_new_session", fake.session_factory)
        monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())
        return fake

    return _install


@pytest.fixture(autouse=True)
def backend_endpoints():
    with patch(
        "api.services.telephony.providers.vonage.provider.get_backend_endpoints",
        new=AsyncMock(return_value=("https://dograh.test", "wss://dograh.test")),
    ):
        yield


async def test_initiate_call_builds_correct_voice_api_request(
    vonage_provider, fake_http, rsa_keypair
):
    fake = fake_http(
        _FakeResponse(
            201,
            {"uuid": "call-uuid-1", "status": "started", "conversation_uuid": "CON-1"},
        )
    )
    provider = vonage_provider()

    result = await provider.initiate_call(
        to_number="+14155551212",
        webhook_url=WEBHOOK,
        workflow_run_id=123,
        from_number="+15551230002",
        workflow_id=7,
        organization_id=11,
    )

    req = fake.requests[0]
    assert req["method"] == "POST"
    assert req["url"] == "https://api.nexmo.com/v1/calls"
    assert req["json"] == {
        "to": [{"type": "phone", "number": "14155551212"}],
        "from": {"type": "phone", "number": "15551230002"},
        "answer_url": [WEBHOOK],
        "answer_method": "GET",
        "event_url": ["https://dograh.test/api/v1/telephony/vonage/events/123"],
        "event_method": "POST",
    }
    # Dograh routing kwargs never leak into the Vonage body.
    assert "workflow_id" not in req["json"]
    assert "organization_id" not in req["json"]
    # Credentials are in the Authorization header, never the URL.
    assert "?" not in req["url"]

    assert result.call_id == "call-uuid-1"
    assert result.caller_number == "+15551230002"
    assert result.provider_metadata == {
        "call_id": "call-uuid-1",
        "call_uuid": "call-uuid-1",
        "conversation_uuid": "CON-1",
    }


async def test_initiate_call_uses_rs256_jwt_bearer(
    vonage_provider, fake_http, rsa_keypair
):
    fake = fake_http(_FakeResponse(201, {"uuid": "u"}))
    await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)

    auth = fake.requests[0]["headers"]["Authorization"]
    scheme, token = auth.split()
    assert scheme == "Bearer"
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, rsa_keypair[1], algorithms=["RS256"])
    assert claims["application_id"] == APPLICATION_ID
    assert claims["exp"] - claims["iat"] <= 300
    assert claims["jti"]


async def test_default_caller_number_selected_from_config(vonage_provider, fake_http):
    fake = fake_http(_FakeResponse(201, {"uuid": "u"}))
    provider = vonage_provider(from_numbers=["+15551230009"])
    result = await provider.initiate_call("+14155551212", WEBHOOK, 1)
    assert fake.requests[0]["json"]["from"]["number"] == "15551230009"
    assert result.caller_number == "+15551230009"


async def test_explicit_caller_number_wins(vonage_provider, fake_http):
    fake = fake_http(_FakeResponse(201, {"uuid": "u"}))
    provider = vonage_provider(from_numbers=["+15551230009", "+15551230010"])
    await provider.initiate_call("+14155551212", WEBHOOK, 1, from_number="+15551230010")
    assert fake.requests[0]["json"]["from"]["number"] == "15551230010"


async def test_invalid_destination_rejected_before_calling_vonage(
    vonage_provider, fake_http
):
    fake = fake_http()
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider().initiate_call("12345", WEBHOOK, 1)
    assert exc.value.category == VonageErrorCategory.INVALID_NUMBER
    assert fake.requests == []


async def test_amd_enabled_adds_machine_detection(vonage_provider, fake_http):
    fake = fake_http(_FakeResponse(201, {"uuid": "u"}))
    await vonage_provider(amd_enabled=True).initiate_call("+14155551212", WEBHOOK, 1)
    assert fake.requests[0]["json"]["machine_detection"] == "continue"


async def test_amd_disabled_by_default(vonage_provider, fake_http):
    fake = fake_http(_FakeResponse(201, {"uuid": "u"}))
    await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert "machine_detection" not in fake.requests[0]["json"]


async def test_missing_config_rejected(vonage_provider, fake_http):
    fake_http()
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider(from_numbers=[]).initiate_call("+14155551212", WEBHOOK, 1)
    assert exc.value.category == VonageErrorCategory.NOT_CONFIGURED
    assert exc.value.status_code == 400


async def test_malformed_private_key_classified(vonage_provider, fake_http):
    fake_http()
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider(private_key="garbage").initiate_call(
            "+14155551212", WEBHOOK, 1
        )
    assert exc.value.category == VonageErrorCategory.MALFORMED_PRIVATE_KEY
    assert "garbage" not in str(exc.value)


@pytest.mark.parametrize(
    "status,body,category,http_status",
    [
        (
            401,
            {"title": "Unauthorized", "detail": "Invalid Token"},
            VonageErrorCategory.INVALID_CREDENTIALS,
            400,
        ),
        (403, {"title": "Forbidden"}, VonageErrorCategory.NUMBER_NOT_AUTHORIZED, 400),
        (
            400,
            {
                "title": "Bad Request",
                "invalid_parameters": [{"name": "to.number", "reason": "invalid"}],
            },
            VonageErrorCategory.INVALID_NUMBER,
            400,
        ),
        (
            402,
            {"title": "Payment required"},
            VonageErrorCategory.INSUFFICIENT_BALANCE,
            402,
        ),
        (
            400,
            {"title": "Insufficient balance"},
            VonageErrorCategory.INSUFFICIENT_BALANCE,
            402,
        ),
        (
            404,
            {"title": "Application not found"},
            VonageErrorCategory.INVALID_APPLICATION_ID,
            400,
        ),
    ],
)
async def test_permanent_errors_not_retried(
    vonage_provider, fake_http, status, body, category, http_status
):
    fake = fake_http(_FakeResponse(status, body))
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert exc.value.category == category
    assert exc.value.status_code == http_status
    assert len(fake.requests) == 1


async def test_rate_limit_is_retried_then_succeeds(vonage_provider, fake_http):
    fake = fake_http(
        _FakeResponse(429, {"title": "Too Many Requests"}, {"Retry-After": "1"}),
        _FakeResponse(201, {"uuid": "after-retry"}),
    )
    result = await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert result.call_id == "after-retry"
    assert len(fake.requests) == 2


async def test_rate_limit_retries_are_bounded(vonage_provider, fake_http):
    fake = fake_http(*[_FakeResponse(429, {}) for _ in range(5)])
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert exc.value.category == VonageErrorCategory.RATE_LIMITED
    assert exc.value.status_code == 429
    assert len(fake.requests) == client_module.MAX_ATTEMPTS


async def test_5xx_on_call_creation_not_retried(vonage_provider, fake_http):
    """A 5xx may mean the call was created; retrying could ring twice."""
    fake = fake_http(
        _FakeResponse(503, "upstream"), _FakeResponse(201, {"uuid": "dup"})
    )
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert exc.value.category == VonageErrorCategory.PROVIDER_UNAVAILABLE
    assert exc.value.status_code == 502
    assert len(fake.requests) == 1


async def test_timeout_on_call_creation_not_retried(vonage_provider, fake_http):
    fake = fake_http(asyncio.TimeoutError(), _FakeResponse(201, {"uuid": "dup"}))
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert exc.value.category == VonageErrorCategory.NETWORK_TIMEOUT
    assert exc.value.status_code == 504
    assert len(fake.requests) == 1


async def test_connect_error_is_retried(vonage_provider, fake_http):
    conn_err = aiohttp.ClientConnectorError(
        connection_key=None, os_error=OSError("refused")
    )
    fake = fake_http(conn_err, _FakeResponse(201, {"uuid": "ok"}))
    result = await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    assert result.call_id == "ok"
    assert len(fake.requests) == 2


async def test_errors_never_contain_credentials(
    vonage_provider, fake_http, rsa_keypair
):
    fake_http(_FakeResponse(401, {"title": "Unauthorized"}))
    with pytest.raises(VonageAPIError) as exc:
        await vonage_provider().initiate_call("+14155551212", WEBHOOK, 1)
    text = str(exc.value) + str(exc.value.detail)
    assert "PRIVATE KEY" not in text
    assert "vonage-api-secret" not in text
    assert "Bearer" not in text


async def test_get_call_status_retries_5xx(vonage_provider, fake_http):
    fake = fake_http(
        _FakeResponse(502, "bad gateway"), _FakeResponse(200, {"status": "answered"})
    )
    data = await vonage_provider().get_call_status("call-1")
    assert data["status"] == "answered"
    assert [r["method"] for r in fake.requests] == ["GET", "GET"]


async def test_get_call_cost(vonage_provider, fake_http):
    fake_http(
        _FakeResponse(
            200,
            {
                "price": "0.0123",
                "duration": "42",
                "status": "completed",
                "rate": "0.02",
            },
        )
    )
    cost = await vonage_provider().get_call_cost("call-1")
    assert cost["cost_usd"] == pytest.approx(0.0123)
    assert cost["duration"] == 42
    assert cost["status"] == "completed"


def test_classifier_defaults_to_request_rejected():
    err = classify_vonage_error(400, {"title": "Something odd"})
    assert err.category == VonageErrorCategory.REQUEST_REJECTED
    assert not err.retryable


async def test_hangup_treats_missing_leg_as_done(vonage_provider, fake_http):
    fake = fake_http(_FakeResponse(404, {"title": "Not found"}))
    assert await vonage_provider().client.hangup("gone")
    assert fake.requests[0]["json"] == {"action": "hangup"}
    assert fake.requests[0]["method"] == "PUT"
