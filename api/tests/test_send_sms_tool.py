"""Tests for the send_sms tool (FracTEL)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.enums import ToolCategory
from api.schemas.tool import SendSmsConfig
from api.services.sms import fractel
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.services.workflow.tools.custom_tool import tool_to_function_schema


def _tool(from_numbers=("8653456051",)):
    return SimpleNamespace(
        name="Send Text",
        description="Text the caller",
        tool_uuid="t1",
        category=ToolCategory.SEND_SMS.value,
        definition={
            "type": "send_sms",
            "config": {"credential_uuid": "c1", "from_numbers": list(from_numbers)},
        },
    )


def test_normalize_number():
    assert fractel.normalize_number("+1 (865) 345-6051") == "8653456051"
    with pytest.raises(ValueError):
        fractel.normalize_number("12345")


def test_config_normalizes_from_numbers():
    assert SendSmsConfig(from_numbers=["+18653456051"]).from_numbers == ["8653456051"]


def test_schema_exposes_to_and_message():
    fn = tool_to_function_schema(_tool())["function"]
    assert set(fn["parameters"]["properties"]) == {"to", "message"}
    assert fn["parameters"]["required"] == ["to", "message"]


def test_rotation_round_robin():
    pool = ["1111111111", "2222222222"]
    picks = {fractel.pick_from_number(pool) for _ in range(2)}
    assert picks == set(pool)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=Mock(), response=Mock())


def _client(responses):
    calls = []

    async def post(url, **kw):
        calls.append((url, kw))
        return responses.pop(0)

    client = Mock()
    client.post = post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client, calls


AUTH_OK = _Resp(200, {"data": {"auth": {"token": "tok"}}})
SEND_OK = _Resp(200, {"data": {"message": {"id": "m1"}}})


@pytest.mark.asyncio
async def test_send_sms_payload_and_token_header():
    fractel._token_cache.clear()
    client, calls = _client([AUTH_OK, SEND_OK])
    with patch.object(fractel.httpx, "AsyncClient", return_value=client):
        mid = await fractel.send_sms(
            username="u",
            password="p",
            from_number="8653456051",
            to_number="+1 555 123 4567",
            message="hi",
        )
    assert mid == "m1"
    assert calls[0][1]["json"]["expires"] == 86400
    url, kw = calls[1]
    assert url.endswith("/messages/send") and kw["headers"] == {"token": "tok"}
    assert kw["json"] == {
        "fonenumber": "8653456051",
        "to": ["5551234567"],
        "message": "hi",
    }


@pytest.mark.asyncio
async def test_permanent_400_is_config_error_without_retry():
    fractel._token_cache.clear()
    client, calls = _client([AUTH_OK, _Resp(400, {"error": "Message NOT sent"})])
    with patch.object(fractel.httpx, "AsyncClient", return_value=client):
        with pytest.raises(fractel.FracTelConfigError):
            await fractel.send_sms(
                username="u",
                password="p",
                from_number="8653456051",
                to_number="5551234567",
                message="hi",
            )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_5xx_is_retried():
    fractel._token_cache.clear()
    client, _ = _client([AUTH_OK, _Resp(503), SEND_OK])
    with (
        patch.object(fractel.httpx, "AsyncClient", return_value=client),
        patch.object(fractel.asyncio, "sleep", AsyncMock()),
    ):
        assert (
            await fractel.send_sms(
                username="u",
                password="p",
                from_number="8653456051",
                to_number="5551234567",
                message="hi",
            )
            == "m1"
        )


@pytest.mark.asyncio
async def test_handler_uses_credential_and_reports_result():
    engine = Mock()
    engine._get_organization_id = AsyncMock(return_value=1)
    handler, timeout = CustomToolManager(engine)._create_handler(_tool(), "send_text")
    params = SimpleNamespace(
        arguments={"to": "5551234567", "message": "hi"}, result_callback=AsyncMock()
    )
    cred = SimpleNamespace(
        credential_type="basic_auth", credential_data={"username": "u", "password": "p"}
    )
    with (
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.db_client.get_credential_by_uuid",
            AsyncMock(return_value=cred),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.send_fractel_sms",
            AsyncMock(return_value="m1"),
        ) as send,
    ):
        await handler(params)
    assert send.await_args.kwargs["from_number"] == "8653456051"
    assert params.result_callback.await_args.args[0] == {
        "status": "success",
        "message_id": "m1",
    }
    assert timeout == 60.0


@pytest.mark.asyncio
async def test_handler_errors_without_credential():
    engine = Mock()
    engine._get_organization_id = AsyncMock(return_value=1)
    handler, _ = CustomToolManager(engine)._create_handler(_tool(), "send_text")
    params = SimpleNamespace(
        arguments={"to": "5551234567", "message": "hi"}, result_callback=AsyncMock()
    )
    with patch(
        "api.services.workflow.pipecat_engine_custom_tools.db_client.get_credential_by_uuid",
        AsyncMock(return_value=None),
    ):
        await handler(params)
    assert params.result_callback.await_args.args[0]["status"] == "error"
