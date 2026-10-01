"""Tests for the send_sms tool (FracTEL)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.enums import ToolCategory
from api.schemas.tool import SendSmsConfig
from api.services.sms import fractel
from api.services.sms.recipient import (
    RecipientResolutionError,
    resolve_call_recipient,
    snapshot_call_parties,
)
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.services.workflow.tools.custom_tool import tool_to_function_schema

CUSTOM_TOOLS = "api.services.workflow.pipecat_engine_custom_tools"


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


def test_mask_number_keeps_last_four():
    assert fractel.mask_number("+15551234567") == "***4567"
    assert fractel.mask_number(None) == "***"


def test_config_normalizes_from_numbers():
    assert SendSmsConfig(from_numbers=["+18653456051"]).from_numbers == ["8653456051"]


def test_schema_exposes_only_message():
    fn = tool_to_function_schema(_tool())["function"]
    assert set(fn["parameters"]["properties"]) == {"message"}
    assert fn["parameters"]["required"] == ["message"]
    assert fn["description"].startswith("Text the caller")
    assert "Never ask for a phone number" in fn["description"]


def test_rotation_round_robin():
    pool = ["1111111111", "2222222222"]
    picks = {fractel.pick_from_number(pool) for _ in range(2)}
    assert picks == set(pool)


# --- recipient resolution -------------------------------------------------


@pytest.mark.parametrize(
    "dialed", ["+15551234567", "15551234567", "5551234567", "+1 (555) 123-4567"]
)
def test_outbound_recipient_is_dialed_number(dialed):
    parties = snapshot_call_parties(
        {"caller_number": "+18653456051", "called_number": dialed}
    )
    assert resolve_call_recipient(parties) == ("5551234567", "called_number")


def test_outbound_falls_back_to_phone_number():
    parties = snapshot_call_parties({"phone_number": "+15551234567"})
    assert resolve_call_recipient(parties) == ("5551234567", "phone_number")


def test_inbound_recipient_is_caller():
    parties = snapshot_call_parties(
        {
            "direction": "inbound",
            "caller_number": "+15551234567",
            "called_number": "+18653456051",
        }
    )
    assert resolve_call_recipient(parties) == ("5551234567", "caller_number")


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"called_number": ""},
        {"direction": "inbound", "called_number": "+18653456051"},
    ],
)
def test_missing_destination_raises(context):
    with pytest.raises(RecipientResolutionError):
        resolve_call_recipient(snapshot_call_parties(context))


def test_non_us_destination_raises():
    with pytest.raises(RecipientResolutionError) as exc:
        resolve_call_recipient(
            snapshot_call_parties({"called_number": "+442071234567"})
        )
    assert exc.value.source_key == "called_number"


def test_snapshot_is_isolated_from_later_context_changes():
    context = {"called_number": "+15551234567"}
    parties = snapshot_call_parties(context)
    # e.g. a pre-call fetch merging external data into the live context
    context.update(called_number="+19998887777")
    assert resolve_call_recipient(parties)[0] == "5551234567"
    with pytest.raises(TypeError):
        parties["called_number"] = "x"


# --- FracTEL client -------------------------------------------------------


class _Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code, self._body = status, body
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "x", request=Mock(), response=Mock(status_code=self.status_code)
            )


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


AUTH_OK = _Resp(201, {"data": {"auth": {"token": "tok"}}})
SEND_OK = _Resp(200, {"data": {"message": {"id": "m1"}}})


async def _send(responses, **overrides):
    fractel._token_cache.clear()
    client, calls = _client(list(responses))
    kwargs = dict(
        username="u",
        password="p",
        from_number="8653456051",
        to_number="5551234567",
        message="hi",
    )
    kwargs.update(overrides)
    with (
        patch.object(fractel.httpx, "AsyncClient", return_value=client),
        patch.object(fractel.asyncio, "sleep", AsyncMock()),
    ):
        return await fractel.send_sms(**kwargs), calls


@pytest.mark.asyncio
async def test_send_sms_payload_and_token_header():
    mid, calls = await _send([AUTH_OK, SEND_OK], to_number="+1 555 123 4567")
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
async def test_permanent_400_is_config_error_with_provider_details():
    rejected = _Resp(400, {"error": {"code": "21610", "message": "Message NOT sent"}})
    with pytest.raises(fractel.FracTelConfigError) as exc:
        await _send([AUTH_OK, rejected])
    assert exc.value.reason == "provider_rejected"
    assert exc.value.status_code == 400
    assert exc.value.provider_code == "21610"
    assert exc.value.provider_message == "Message NOT sent"


@pytest.mark.asyncio
async def test_permanent_400_is_not_retried():
    fractel._token_cache.clear()
    client, calls = _client([AUTH_OK, _Resp(400, {"error": "Message NOT sent"})])
    with patch.object(fractel.httpx, "AsyncClient", return_value=client):
        with pytest.raises(fractel.FracTelConfigError) as exc:
            await fractel.send_sms(
                username="u",
                password="p",
                from_number="8653456051",
                to_number="5551234567",
                message="hi",
            )
    assert len(calls) == 2
    assert exc.value.provider_message == "Message NOT sent"


@pytest.mark.asyncio
async def test_bad_credentials_are_reported_without_secrets():
    with pytest.raises(fractel.FracTelConfigError) as exc:
        await _send([_Resp(401, {"message": "Invalid login"})], password="s3cret")
    assert exc.value.reason == "credentials_invalid"
    assert exc.value.provider_message == "Invalid login"
    assert "s3cret" not in str(exc.value)


@pytest.mark.parametrize(
    "auth_body",
    [
        {"data": {"auth": {"token": "tok"}}},
        {"data": {"token": "tok"}},
        {"token": "tok"},
        {"auth": {"token": "tok", "expires": 86400}},
        {"data": [{"authorization": {"Token": "tok"}}]},
    ],
)
@pytest.mark.asyncio
async def test_auth_token_found_in_any_layout(auth_body):
    mid, calls = await _send([_Resp(201, auth_body), SEND_OK])
    assert mid == "m1"
    assert calls[1][1]["headers"] == {"token": "tok"}


@pytest.mark.asyncio
async def test_auth_without_token_fails_fast_and_logs_layout_only():
    body = {"data": {"auth": {"jwt_value": "SECRET-JWT"}}}
    messages = []
    sink = fractel.logger.add(lambda m: messages.append(str(m)))
    try:
        with pytest.raises(fractel.FracTelConfigError) as exc:
            await _send([_Resp(201, body)])
    finally:
        fractel.logger.remove(sink)
    assert exc.value.reason == "auth_failed"
    logged = "".join(messages)
    assert "jwt_value" in logged and "SECRET-JWT" not in logged


@pytest.mark.asyncio
async def test_auth_201_error_body_surfaces_provider_message():
    body = {"status": "error", "message": "Invalid username or password"}
    with pytest.raises(fractel.FracTelConfigError) as exc:
        await _send([_Resp(201, body)])
    assert exc.value.reason == "auth_failed"
    assert exc.value.provider_message == "Invalid username or password"


@pytest.mark.asyncio
async def test_2xx_with_error_body_is_not_success():
    with pytest.raises(fractel.FracTelConfigError) as exc:
        await _send([AUTH_OK, _Resp(200, {"status": "error", "message": "No 10DLC"})])
    assert exc.value.reason == "provider_rejected"
    assert exc.value.provider_message == "No 10DLC"


@pytest.mark.asyncio
async def test_5xx_is_retried():
    mid, _ = await _send([AUTH_OK, _Resp(503), SEND_OK])
    assert mid == "m1"


@pytest.mark.asyncio
async def test_retries_exhausted_keeps_last_status():
    with pytest.raises(fractel.FracTelError) as exc:
        await _send([AUTH_OK] + [_Resp(503, {"message": "down"})] * 4)
    assert not isinstance(exc.value, fractel.FracTelConfigError)
    assert exc.value.reason == "provider_unavailable"
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_invalid_recipient_is_config_error():
    with pytest.raises(fractel.FracTelConfigError) as exc:
        await _send([], to_number="12345")
    assert exc.value.reason == "recipient_invalid"


# --- tool handler ---------------------------------------------------------


def _engine(call_context):
    engine = Mock()
    engine._get_organization_id = AsyncMock(return_value=1)
    engine._workflow_run_id = 42
    engine._call_parties = snapshot_call_parties(call_context)
    return engine


def _cred():
    return SimpleNamespace(
        credential_type="basic_auth", credential_data={"username": "u", "password": "p"}
    )


async def _run_handler(engine, arguments, *, tool=None, send=None, cred=_cred):
    handler, timeout = CustomToolManager(engine)._create_handler(
        tool or _tool(), "send_text"
    )
    params = SimpleNamespace(arguments=arguments, result_callback=AsyncMock())
    send = send or AsyncMock(return_value="m1")
    with (
        patch(
            f"{CUSTOM_TOOLS}.db_client.get_credential_by_uuid",
            AsyncMock(return_value=cred() if cred else None),
        ),
        patch(f"{CUSTOM_TOOLS}.send_fractel_sms", send),
    ):
        await handler(params)
    return params.result_callback.await_args.args[0], send, timeout


OUTBOUND = {"caller_number": "+18653456051", "called_number": "+15551234567"}


@pytest.mark.asyncio
async def test_handler_texts_outbound_destination():
    result, send, timeout = await _run_handler(_engine(OUTBOUND), {"message": "hi"})
    assert result == {"status": "success", "message_id": "m1"}
    assert send.await_args.kwargs == {
        "username": "u",
        "password": "p",
        "from_number": "8653456051",
        "to_number": "5551234567",
        "message": "hi",
    }
    assert timeout == 60.0


@pytest.mark.asyncio
async def test_handler_ignores_llm_supplied_recipient():
    result, send, _ = await _run_handler(
        _engine(OUTBOUND), {"to": "2125550000", "message": "hi"}
    )
    assert result["status"] == "success"
    assert send.await_args.kwargs["to_number"] == "5551234567"


@pytest.mark.asyncio
async def test_handler_never_texts_sender_number():
    # Sender pool contains the call's caller ID; the customer is still the target.
    result, send, _ = await _run_handler(
        _engine(OUTBOUND),
        {"message": "hi"},
        tool=_tool(from_numbers=("8653456051",)),
    )
    assert send.await_args.kwargs["from_number"] == "8653456051"
    assert send.await_args.kwargs["to_number"] == "5551234567"


@pytest.mark.asyncio
async def test_handler_round_robins_senders():
    tool = _tool(from_numbers=("1111111111", "2222222222"))
    senders = set()
    for _ in range(2):
        _, send, _ = await _run_handler(_engine(OUTBOUND), {"message": "hi"}, tool=tool)
        senders.add(send.await_args.kwargs["from_number"])
        assert send.await_args.kwargs["to_number"] == "5551234567"
    assert senders == {"1111111111", "2222222222"}


@pytest.mark.asyncio
async def test_handler_inbound_texts_caller():
    engine = _engine(
        {
            "direction": "inbound",
            "caller_number": "+15551234567",
            "called_number": "+18653456051",
        }
    )
    result, send, _ = await _run_handler(engine, {"message": "hi"})
    assert result["status"] == "success"
    assert send.await_args.kwargs["to_number"] == "5551234567"


@pytest.mark.asyncio
async def test_handler_missing_destination_fails_without_sending():
    result, send, _ = await _run_handler(
        _engine({}), {"to": "5551234567", "message": "hi"}
    )
    assert result["status"] == "error"
    assert result["reason"] == "recipient_unavailable"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_errors_without_credential():
    result, send, _ = await _run_handler(
        _engine(OUTBOUND), {"message": "hi"}, cred=None
    )
    assert result["status"] == "error"
    assert result["reason"] == "credentials_missing"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_reports_provider_failure():
    send = AsyncMock(
        side_effect=fractel.FracTelConfigError(
            "FracTEL rejected the message (400)",
            reason="provider_rejected",
            status_code=400,
            provider_message="Message NOT sent",
        )
    )
    result, _, _ = await _run_handler(_engine(OUTBOUND), {"message": "hi"}, send=send)
    assert result == {
        "status": "error",
        "reason": "provider_rejected",
        "error": "The text message provider rejected the message",
    }


@pytest.mark.asyncio
async def test_handler_end_to_end_request_from_outbound_call():
    """Outbound call context -> real FracTEL client -> HTTP payload."""
    fractel._token_cache.clear()
    client, calls = _client([AUTH_OK, SEND_OK])
    engine = _engine(OUTBOUND)
    handler, _ = CustomToolManager(engine)._create_handler(_tool(), "send_text")
    params = SimpleNamespace(
        arguments={"message": "Here is your link"}, result_callback=AsyncMock()
    )
    with (
        patch(
            f"{CUSTOM_TOOLS}.db_client.get_credential_by_uuid",
            AsyncMock(return_value=_cred()),
        ),
        patch.object(fractel.httpx, "AsyncClient", return_value=client),
    ):
        await handler(params)
    assert params.result_callback.await_args.args[0] == {
        "status": "success",
        "message_id": "m1",
    }
    assert calls[1][1]["json"] == {
        "fonenumber": "8653456051",
        "to": ["5551234567"],
        "message": "Here is your link",
    }


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_cross_recipients():
    """Each call's handler texts its own customer even when interleaved."""
    numbers = [f"+1555000{i:04d}" for i in range(20)]
    sent: list[tuple[str, str]] = []

    async def fake_send(**kw):
        await asyncio.sleep(0)  # force interleaving between calls
        sent.append((kw["message"], kw["to_number"]))
        return "m"

    handlers = []
    for i, number in enumerate(numbers):
        engine = _engine({"caller_number": "+18653456051", "called_number": number})
        handler, _ = CustomToolManager(engine)._create_handler(_tool(), "send_text")
        params = SimpleNamespace(
            arguments={"message": f"call-{i}"}, result_callback=AsyncMock()
        )
        handlers.append(handler(params))

    with (
        patch(
            f"{CUSTOM_TOOLS}.db_client.get_credential_by_uuid",
            AsyncMock(return_value=_cred()),
        ),
        patch(f"{CUSTOM_TOOLS}.send_fractel_sms", side_effect=fake_send),
    ):
        await asyncio.gather(*handlers)

    assert len(sent) == len(numbers)
    for message, to_number in sent:
        i = int(message.split("-")[1])
        assert to_number == numbers[i][2:]
