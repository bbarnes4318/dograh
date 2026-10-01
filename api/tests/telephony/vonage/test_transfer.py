"""Vonage call transfer: provider leg, events route, strategies, serializer,
and the generic Transfer Call tool end-to-end (real Redis pub/sub)."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pipecat.frames.frames import CancelFrame, EndFrame
from pipecat.utils.enums import EndTaskReason
from starlette.requests import Request

from api.services.telephony.providers.vonage import client as client_module
from api.services.telephony.providers.vonage import routes as vonage_routes
from api.services.telephony.providers.vonage.serializers import VonageFrameSerializer
from api.services.telephony.providers.vonage.strategies import (
    VonageConversationTransferStrategy,
    VonageHangupStrategy,
)
from api.services.telephony.transfer_event_protocol import (
    TransferContext,
    TransferEventType,
)

CALLER_UUID = "caller-leg-uuid"
DEST_UUID = "destination-leg-uuid"


class _Resp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = body
        self.headers = {}

    async def text(self):
        return "" if self._body is None else json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeHTTP:
    def __init__(self, replies: List[Any]):
        self.replies = list(replies)
        self.requests: List[dict] = []

    def session(self, *a, **kw):
        fake = self

        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(self, method, url, json=None, headers=None, **kw):
                fake.requests.append({"method": method, "url": url, "json": json})
                reply = fake.replies.pop(0) if fake.replies else _Resp(204)
                if isinstance(reply, Exception):
                    raise reply
                return reply

        return _S()


@pytest.fixture
def fake_http(monkeypatch):
    def _install(*replies):
        fake = FakeHTTP(list(replies))
        monkeypatch.setattr(client_module, "_new_session", fake.session)
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


@pytest.fixture
def transfer_manager():
    """Fresh CallTransferManager on the test Redis."""
    from api.services.telephony.call_transfer_manager import CallTransferManager

    return CallTransferManager()


def _event_request(body: str, headers: dict, transfer_id: str) -> Request:
    async def receive():
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/telephony/vonage/transfer-events/{transfer_id}",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "scheme": "https",
            "server": ("dograh.test", 443),
        },
        receive,
    )


# ---------------------------------------------------------------------------
# Provider: destination leg
# ---------------------------------------------------------------------------


async def test_transfer_call_dials_destination_into_conversation(
    vonage_provider, fake_http
):
    fake = fake_http(_Resp(201, {"uuid": DEST_UUID, "status": "started"}))
    provider = vonage_provider()

    result = await provider.transfer_call(
        destination="+14155550123",
        transfer_id="t-1",
        conference_name=f"transfer-{CALLER_UUID}",
        timeout=25,
    )

    body = fake.requests[0]["json"]
    assert fake.requests[0]["url"] == "https://api.nexmo.com/v1/calls"
    assert body["to"] == [{"type": "phone", "number": "14155550123"}]
    assert body["from"] == {"type": "phone", "number": "15551230002"}
    assert body["ringing_timer"] == 25
    assert body["event_url"] == [
        "https://dograh.test/api/v1/telephony/vonage/transfer-events/t-1"
    ]
    assert body["ncco"][-1] == {
        "action": "conversation",
        "name": f"transfer-{CALLER_UUID}",
        "startOnEnter": True,
        "endOnExit": True,
    }
    assert result["call_sid"] == DEST_UUID
    assert result["provider"] == "vonage"
    assert provider.supports_transfers() is True


async def test_transfer_ringing_timer_is_clamped(vonage_provider, fake_http):
    fake = fake_http(_Resp(201, {"uuid": "a"}), _Resp(201, {"uuid": "b"}))
    provider = vonage_provider()
    await provider.transfer_call("+14155550123", "t", "c", timeout=999)
    await provider.transfer_call("+14155550123", "t", "c", timeout=0)
    assert fake.requests[0]["json"]["ringing_timer"] == 120
    assert fake.requests[1]["json"]["ringing_timer"] == 1


async def test_transfer_to_invalid_destination_fails_fast(vonage_provider, fake_http):
    from api.services.telephony.providers.vonage.errors import VonageAPIError

    fake = fake_http()
    with pytest.raises(VonageAPIError):
        await vonage_provider().transfer_call("not-a-number", "t", "c")
    assert fake.requests == []


async def test_cancel_transfer_call_hangs_up_leg(vonage_provider, fake_http):
    fake = fake_http(_Resp(204))
    await vonage_provider().cancel_transfer_call(DEST_UUID)
    assert fake.requests == [
        {
            "method": "PUT",
            "url": f"https://api.nexmo.com/v1/calls/{DEST_UUID}",
            "json": {"action": "hangup"},
        }
    ]


# ---------------------------------------------------------------------------
# Transfer events route
# ---------------------------------------------------------------------------


async def _store_context(manager, transfer_id, call_sid=DEST_UUID, run_id=123):
    ctx = TransferContext(
        transfer_id=transfer_id,
        call_sid=call_sid,
        target_number="+14155550123",
        tool_uuid="tool",
        original_call_sid=CALLER_UUID,
        conference_name=f"transfer-{CALLER_UUID}",
        initiated_at=0.0,
        workflow_run_id=run_id,
    )
    await manager.store_transfer_context(ctx)
    return ctx


@pytest.fixture
def events_env(vonage_provider, make_workflow_run, transfer_manager):
    provider = vonage_provider()
    run = make_workflow_run(call_id=CALLER_UUID)
    with (
        patch.object(
            vonage_routes,
            "db_client",
            SimpleNamespace(get_workflow_run_by_id=AsyncMock(return_value=run)),
        ),
        patch.object(
            vonage_routes,
            "get_telephony_provider_for_run",
            new=AsyncMock(return_value=provider),
        ),
        patch.object(
            vonage_routes,
            "get_call_transfer_manager",
            new=AsyncMock(return_value=transfer_manager),
        ),
    ):
        yield SimpleNamespace(provider=provider, run=run, manager=transfer_manager)


async def _post_transfer_event(
    transfer_id, status, signed_headers, leg=DEST_UUID, **header_kw
):
    body = json.dumps({"uuid": leg, "status": status, "conversation_uuid": "CON"})
    return await vonage_routes.handle_vonage_transfer_events(
        transfer_id,
        _event_request(body, signed_headers(body, **header_kw), transfer_id),
    )


@pytest.mark.parametrize(
    "status,expected_type,reason",
    [
        ("answered", TransferEventType.DESTINATION_ANSWERED, None),
        ("busy", TransferEventType.TRANSFER_FAILED, "busy"),
        ("timeout", TransferEventType.TRANSFER_FAILED, "no_answer"),
        ("unanswered", TransferEventType.TRANSFER_FAILED, "no_answer"),
        ("rejected", TransferEventType.TRANSFER_FAILED, "call_failed"),
        ("failed", TransferEventType.TRANSFER_FAILED, "call_failed"),
        ("cancelled", TransferEventType.TRANSFER_FAILED, "call_failed"),
    ],
)
async def test_transfer_events_publish_outcomes(
    events_env, signed_headers, status, expected_type, reason
):
    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(events_env.manager, transfer_id)

    waiter = asyncio.create_task(
        events_env.manager.wait_for_transfer_completion(transfer_id, 5)
    )
    await asyncio.sleep(0.2)  # let the subscriber attach
    result = await _post_transfer_event(transfer_id, status, signed_headers)
    event = await waiter

    assert result == {"status": "completed"}
    assert event.type == expected_type
    assert event.reason == reason
    assert event.original_call_sid == CALLER_UUID
    assert event.transfer_call_sid == DEST_UUID


@pytest.mark.parametrize("status", ["started", "ringing", "completed"])
async def test_transfer_intermediate_states_pending(events_env, signed_headers, status):
    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(events_env.manager, transfer_id)
    assert (await _post_transfer_event(transfer_id, status, signed_headers)) == {
        "status": "pending"
    }


async def test_transfer_event_invalid_signature_401(events_env, signed_headers):
    from fastapi import HTTPException

    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(events_env.manager, transfer_id)
    with pytest.raises(HTTPException) as exc:
        await _post_transfer_event(
            transfer_id, "answered", signed_headers, signature_secret="bad"
        )
    assert exc.value.status_code == 401


async def test_transfer_event_for_unrelated_leg_ignored(events_env, signed_headers):
    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(events_env.manager, transfer_id)
    result = await _post_transfer_event(
        transfer_id, "answered", signed_headers, leg="other"
    )
    assert result == {"status": "ignored"}


async def test_transfer_event_without_context_ignored(events_env, signed_headers):
    result = await _post_transfer_event("unknown-transfer", "answered", signed_headers)
    assert result == {"status": "ignored"}


# ---------------------------------------------------------------------------
# Strategies + serializer
# ---------------------------------------------------------------------------


@pytest.fixture
def strategy_context(rsa_keypair):
    return {
        "call_uuid": CALLER_UUID,
        "application_id": "aaaaaaaa-bbbb-cccc-dddd-0123456789ab",
        "private_key": rsa_keypair[0],
    }


@pytest.fixture
def patched_manager(transfer_manager):
    with patch(
        "api.services.telephony.call_transfer_manager.get_call_transfer_manager",
        new=AsyncMock(return_value=transfer_manager),
    ):
        yield transfer_manager


async def test_transfer_strategy_moves_caller_into_conversation(
    fake_http, strategy_context, patched_manager
):
    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(patched_manager, transfer_id)
    fake = fake_http(_Resp(204))

    assert await VonageConversationTransferStrategy().execute_transfer(strategy_context)

    req = fake.requests[0]
    assert req["method"] == "PUT"
    assert req["url"] == f"https://api.nexmo.com/v1/calls/{CALLER_UUID}"
    assert req["json"] == {
        "action": "transfer",
        "destination": {
            "type": "ncco",
            "ncco": [
                {
                    "action": "conversation",
                    "name": f"transfer-{CALLER_UUID}",
                    "startOnEnter": True,
                    "endOnExit": True,
                }
            ],
        },
    }
    assert await patched_manager.get_transfer_context(transfer_id) is None


async def test_transfer_strategy_failure_releases_destination(
    fake_http, strategy_context, patched_manager
):
    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(patched_manager, transfer_id)
    # Caller leg is gone (400), destination hangup succeeds.
    fake = fake_http(_Resp(400, {"title": "Call already completed"}), _Resp(204))

    assert not await VonageConversationTransferStrategy().execute_transfer(
        strategy_context
    )

    assert fake.requests[1] == {
        "method": "PUT",
        "url": f"https://api.nexmo.com/v1/calls/{DEST_UUID}",
        "json": {"action": "hangup"},
    }
    assert await patched_manager.get_transfer_context(transfer_id) is None


async def test_transfer_strategy_without_context_fails(
    fake_http, strategy_context, patched_manager
):
    fake = fake_http()
    ctx = dict(strategy_context, call_uuid="no-transfer-here")
    assert not await VonageConversationTransferStrategy().execute_transfer(ctx)
    assert fake.requests == []


async def test_hangup_strategy_releases_pending_transfer_leg(
    fake_http, strategy_context, patched_manager
):
    """Caller hangs up while the destination is still ringing: no orphan leg."""
    transfer_id = f"t-{uuid.uuid4()}"
    await _store_context(patched_manager, transfer_id)
    fake = fake_http(_Resp(204), _Resp(204))

    assert await VonageHangupStrategy().execute_hangup(strategy_context)

    urls = [r["url"] for r in fake.requests]
    assert urls == [
        f"https://api.nexmo.com/v1/calls/{DEST_UUID}",
        f"https://api.nexmo.com/v1/calls/{CALLER_UUID}",
    ]
    assert await patched_manager.get_transfer_context(transfer_id) is None


async def test_hangup_strategy_plain_call(fake_http, strategy_context, patched_manager):
    fake = fake_http(_Resp(204))
    assert await VonageHangupStrategy().execute_hangup(strategy_context)
    assert [r["url"] for r in fake.requests] == [
        f"https://api.nexmo.com/v1/calls/{CALLER_UUID}"
    ]


def _serializer():
    transfer = Mock()
    transfer.execute_transfer = AsyncMock(return_value=True)
    hangup = Mock()
    hangup.execute_hangup = AsyncMock(return_value=True)
    s = VonageFrameSerializer(
        call_uuid=CALLER_UUID,
        application_id="app",
        private_key="key",
        transfer_strategy=transfer,
        hangup_strategy=hangup,
    )
    return s, transfer, hangup


async def test_serializer_transfer_reason_runs_transfer_and_never_hangs_up():
    s, transfer, hangup = _serializer()
    assert await s.serialize(EndFrame(reason=EndTaskReason.TRANSFER_CALL.value)) is None
    assert await s.serialize(CancelFrame()) is None
    transfer.execute_transfer.assert_awaited_once()
    assert transfer.execute_transfer.await_args.args[0]["call_uuid"] == CALLER_UUID
    hangup.execute_hangup.assert_not_awaited()


async def test_serializer_normal_end_hangs_up_once():
    s, transfer, hangup = _serializer()
    await s.serialize(EndFrame(reason=EndTaskReason.USER_HANGUP.value))
    await s.serialize(CancelFrame())
    hangup.execute_hangup.assert_awaited_once()
    transfer.execute_transfer.assert_not_awaited()


# ---------------------------------------------------------------------------
# Generic Transfer Call tool on a Vonage call (end-to-end)
# ---------------------------------------------------------------------------


def _engine():
    engine = Mock()
    engine._workflow_run_id = 123
    engine._call_context_vars = {}
    engine._gathered_context = {}
    engine._fetch_recording_audio = None
    engine._audio_config = SimpleNamespace(transport_out_sample_rate=16000)
    engine._transport_output = SimpleNamespace(queue_frame=AsyncMock())
    engine._get_organization_id = AsyncMock(return_value=11)
    engine.set_mute_pipeline = Mock()
    engine.end_call_with_reason = AsyncMock()
    engine.call_hygiene = None
    return engine


def _tool(timeout=5):
    return SimpleNamespace(
        tool_uuid="transfer-tool-uuid",
        name="Transfer Call",
        description="Transfer the caller",
        category="transfer_call",
        definition={
            "schema_version": 1,
            "type": "transfer_call",
            "config": {"destination": "+14155550123", "timeout": timeout},
        },
    )


async def _run_transfer_tool(
    provider, transfer_manager, run, *, on_dial=None, timeout=5
):
    from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

    engine = _engine()
    manager = CustomToolManager(engine)
    handler, _ = manager._create_handler(_tool(timeout), "transfer_call")
    results = []

    async def result_callback(result, properties=None):
        results.append(result)

    params = Mock()
    params.arguments = {}
    params.result_callback = result_callback

    original_transfer = provider.transfer_call

    async def transfer_and_signal(**kwargs):
        out = await original_transfer(**kwargs)
        if on_dial:
            asyncio.get_running_loop().call_later(
                0.3, lambda: asyncio.ensure_future(on_dial(kwargs["transfer_id"]))
            )
        return out

    with (
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.db_client.get_workflow_run_by_id",
            new=AsyncMock(return_value=run),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.get_telephony_provider_for_run",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.get_call_transfer_manager",
            new=AsyncMock(return_value=transfer_manager),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.play_audio_loop",
            new=AsyncMock(return_value=None),
        ),
        patch.object(provider, "transfer_call", side_effect=transfer_and_signal),
    ):
        await handler(params)
    return engine, results


async def test_transfer_tool_success_on_vonage(events_env, fake_http, signed_headers):
    fake = fake_http(_Resp(201, {"uuid": DEST_UUID}))
    run = SimpleNamespace(
        mode="vonage",
        gathered_context={"call_id": CALLER_UUID},
        initial_context={"telephony_configuration_id": 5},
    )

    async def destination_answers(transfer_id):
        await _post_transfer_event(transfer_id, "answered", signed_headers)

    engine, results = await _run_transfer_tool(
        events_env.provider, events_env.manager, run, on_dial=destination_answers
    )

    assert fake.requests[0]["json"]["ncco"][-1]["name"] == f"transfer-{CALLER_UUID}"
    assert results[-1]["status"] == "transfer_success"
    engine.end_call_with_reason.assert_awaited_once_with(
        EndTaskReason.TRANSFER_CALL.value, abort_immediately=False
    )
    engine.set_mute_pipeline.assert_called_with(False)


@pytest.mark.parametrize("status,reason", [("busy", "busy"), ("timeout", "no_answer")])
async def test_transfer_tool_failure_returns_to_agent(
    events_env, fake_http, signed_headers, status, reason
):
    fake_http(_Resp(201, {"uuid": DEST_UUID}))
    run = SimpleNamespace(
        mode="vonage", gathered_context={"call_id": CALLER_UUID}, initial_context={}
    )

    async def destination_fails(transfer_id):
        await _post_transfer_event(transfer_id, status, signed_headers)

    engine, results = await _run_transfer_tool(
        events_env.provider, events_env.manager, run, on_dial=destination_fails
    )

    assert results[-1]["status"] == "transfer_failed"
    assert results[-1]["reason"] == reason
    engine.end_call_with_reason.assert_not_awaited()
    engine.set_mute_pipeline.assert_called_with(False)
    assert engine._transfer_handoff_started is False


async def test_transfer_tool_provider_failure(events_env, fake_http):
    fake_http(_Resp(403, {"title": "Forbidden"}))
    run = SimpleNamespace(
        mode="vonage", gathered_context={"call_id": CALLER_UUID}, initial_context={}
    )

    engine, results = await _run_transfer_tool(
        events_env.provider, events_env.manager, run
    )

    assert results[-1]["status"] == "transfer_failed"
    assert results[-1]["reason"] == "provider_error"
    assert "number_not_authorized" in results[-1]["message"]
    engine.set_mute_pipeline.assert_called_with(False)
    assert await events_env.manager.find_transfer_context_for_call(CALLER_UUID) is None


async def test_transfer_tool_timeout_cancels_destination_leg(events_env, fake_http):
    """No answer within the tool timeout: the destination leg is hung up and
    the context removed, so a late answer can't strand the destination."""
    fake = fake_http(_Resp(201, {"uuid": DEST_UUID}), _Resp(204))
    run = SimpleNamespace(
        mode="vonage", gathered_context={"call_id": CALLER_UUID}, initial_context={}
    )

    engine, results = await _run_transfer_tool(
        events_env.provider, events_env.manager, run, timeout=1
    )

    assert results[-1]["reason"] == "timeout"
    assert fake.requests[-1] == {
        "method": "PUT",
        "url": f"https://api.nexmo.com/v1/calls/{DEST_UUID}",
        "json": {"action": "hangup"},
    }
    assert await events_env.manager.find_transfer_context_for_call(CALLER_UUID) is None
