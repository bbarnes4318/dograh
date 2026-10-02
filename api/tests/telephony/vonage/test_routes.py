"""Vonage answer-URL and event-webhook routes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.enums import TelephonyCallStatus
from api.services.telephony.providers.vonage import routes as vonage_routes
from api.services.telephony.providers.vonage.provider import normalize_vonage_status

CALL_UUID = "aaaaaaaa-bbbb-cccc-dddd-0123456789ab"


def _request(
    body: str,
    headers: dict,
    path="/api/v1/telephony/vonage/events/123",
    method="POST",
    query=b"",
):
    async def receive():
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "scheme": "https",
            "server": ("dograh.test", 443),
        },
        receive,
    )


def _event(status="answered", uuid=CALL_UUID, **extra):
    data = {
        "uuid": uuid,
        "conversation_uuid": "CON-1",
        "status": status,
        "direction": "outbound",
        "from": "15551230002",
        "to": "14155551212",
        "timestamp": "2026-10-01T12:00:00.000Z",
    }
    data.update(extra)
    return json.dumps(data, separators=(",", ":"))


@pytest.fixture
def route_env(vonage_provider, make_workflow_run):
    """Patch DB + provider resolution for the events route."""

    def _install(run=None, provider=None):
        run = run or make_workflow_run()
        provider = provider or vonage_provider()
        process = AsyncMock()
        db = SimpleNamespace(
            get_workflow_run_by_id=AsyncMock(return_value=run),
            get_workflow_by_id=AsyncMock(
                return_value=SimpleNamespace(id=run.workflow_id, organization_id=11)
            ),
            get_workflow_run=AsyncMock(return_value=run),
            get_workflow_run_by_call_id=AsyncMock(return_value=run),
            update_workflow_run=AsyncMock(),
        )
        patches = [
            patch.object(vonage_routes, "db_client", db),
            patch.object(
                vonage_routes,
                "get_telephony_provider_for_run",
                new=AsyncMock(return_value=provider),
            ),
            patch(
                "api.services.telephony.status_processor._process_status_update",
                new=process,
            ),
        ]
        for p in patches:
            p.start()
        return SimpleNamespace(
            run=run, provider=provider, process=process, db=db, patches=patches
        )

    envs = []

    def _factory(**kw):
        env = _install(**kw)
        envs.append(env)
        return env

    yield _factory
    for env in envs:
        for p in env.patches:
            p.stop()


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,detail,expected",
    [
        ("started", None, TelephonyCallStatus.INITIATED),
        ("ringing", None, TelephonyCallStatus.RINGING),
        ("answered", None, TelephonyCallStatus.ANSWERED),
        ("completed", None, TelephonyCallStatus.COMPLETED),
        ("complete", None, TelephonyCallStatus.COMPLETED),
        ("disconnected", None, TelephonyCallStatus.COMPLETED),
        ("busy", None, TelephonyCallStatus.BUSY),
        ("rejected", None, TelephonyCallStatus.FAILED),
        ("rejected", "declined", TelephonyCallStatus.BUSY),
        ("failed", "unavailable", TelephonyCallStatus.NO_ANSWER),
        ("failed", "invalid_number", TelephonyCallStatus.FAILED),
        ("timeout", None, TelephonyCallStatus.NO_ANSWER),
        ("unanswered", None, TelephonyCallStatus.NO_ANSWER),
        ("cancelled", None, TelephonyCallStatus.CANCELED),
        ("failed", None, TelephonyCallStatus.FAILED),
        ("COMPLETED", None, TelephonyCallStatus.COMPLETED),
        ("human", None, None),
        ("", None, None),
    ],
)
def test_normalize_vonage_status(status, detail, expected):
    assert normalize_vonage_status(status, detail) == expected


def test_parse_status_callback_normalizes_numbers(vonage_provider):
    parsed = vonage_provider().parse_status_callback(
        json.loads(_event("completed", duration=42))
    )
    assert parsed["status"] == TelephonyCallStatus.COMPLETED
    assert parsed["from_number"] == "+15551230002"
    assert parsed["to_number"] == "+14155551212"
    assert parsed["duration"] == "42"


def test_parse_status_callback_non_lifecycle_is_none(vonage_provider):
    assert (
        vonage_provider().parse_status_callback({"uuid": "x", "status": "machine"})[
            "status"
        ]
        is None
    )


def test_parse_status_callback_unknown_state_passes_through(vonage_provider):
    assert (
        vonage_provider().parse_status_callback({"uuid": "x", "status": "weird"})[
            "status"
        ]
        == "weird"
    )


# ---------------------------------------------------------------------------
# Event webhook
# ---------------------------------------------------------------------------


async def test_valid_signed_event_is_processed(route_env, signed_headers):
    env = route_env()
    body = _event("completed", duration=17)
    result = await vonage_routes.handle_vonage_events(
        _request(body, signed_headers(body)), 123
    )

    assert result == {"status": "ok"}
    run_id, update = env.process.await_args.args
    assert run_id == 123
    assert update.status == TelephonyCallStatus.COMPLETED
    assert update.call_id == CALL_UUID
    assert update.duration == "17"
    assert update.extra["vonage_status"] == "completed"
    assert "status" not in update.extra  # normalized status wins in the log


@pytest.mark.parametrize(
    "header_kwargs",
    [
        {"signature_secret": "wrong"},
        {"api_key": "other"},
        {"application_id": "bbbbbbbb-bbbb-cccc-dddd-0123456789ab"},
    ],
)
async def test_invalid_signature_returns_401(route_env, signed_headers, header_kwargs):
    env = route_env()
    body = _event()
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_vonage_events(
            _request(body, signed_headers(body, **header_kwargs)), 123
        )
    assert exc.value.status_code == 401
    env.process.assert_not_awaited()


async def test_bad_payload_hash_returns_401(route_env, signed_headers):
    env = route_env()
    signed_for = _event("busy")
    sent = _event("completed")
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_vonage_events(
            _request(sent, signed_headers(signed_for)), 123
        )
    assert exc.value.status_code == 401
    env.process.assert_not_awaited()


async def test_missing_authorization_returns_401(route_env):
    env = route_env()
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_vonage_events(_request(_event(), {}), 123)
    assert exc.value.status_code == 401
    env.process.assert_not_awaited()


async def test_websocket_leg_events_do_not_drive_run_lifecycle(
    route_env, signed_headers
):
    env = route_env()
    body = _event("completed", uuid="websocket-leg-uuid")
    result = await vonage_routes.handle_vonage_events(
        _request(body, signed_headers(body)), 123
    )
    assert result["ignored"] == "secondary_leg"
    env.process.assert_not_awaited()


async def test_duplicate_event_is_skipped(route_env, signed_headers, make_workflow_run):
    logs = {
        "telephony_status_callbacks": [
            {
                "status": "completed",
                "uuid": CALL_UUID,
                "vonage_status": "completed",
                "timestamp_vonage": "2026-10-01T12:00:00.000Z",
            }
        ]
    }
    env = route_env(run=make_workflow_run(logs=logs))
    body = _event("completed")
    result = await vonage_routes.handle_vonage_events(
        _request(body, signed_headers(body)), 123
    )
    assert result.get("duplicate") is True
    env.process.assert_not_awaited()


async def test_amd_result_persisted_not_treated_as_status(route_env, signed_headers):
    env = route_env()
    body = _event("machine", sub_state="beep_start")
    result = await vonage_routes.handle_vonage_events(
        _request(body, signed_headers(body)), 123
    )
    assert result == {"status": "ok"}
    env.db.update_workflow_run.assert_awaited_once_with(
        run_id=123, gathered_context={"answered_by": "machine_beep_start"}
    )
    env.process.assert_not_awaited()


async def test_event_without_status_ignored(route_env, signed_headers):
    env = route_env()
    body = json.dumps(
        {"uuid": CALL_UUID, "conversation_uuid_from": "a", "type": "transfer"}
    )
    result = await vonage_routes.handle_vonage_events(
        _request(body, signed_headers(body)), 123
    )
    assert result == {"status": "ok"}
    env.process.assert_not_awaited()


async def test_application_level_event_resolves_run_by_uuid(route_env, signed_headers):
    env = route_env()
    body = _event("answered")
    result = await vonage_routes.handle_vonage_events_without_run(
        _request(body, signed_headers(body), path="/api/v1/telephony/vonage/events")
    )
    assert result == {"status": "ok"}
    env.db.get_workflow_run_by_call_id.assert_awaited_once_with(CALL_UUID)
    env.process.assert_awaited_once()


async def test_non_json_body_rejected(route_env, signed_headers):
    route_env()
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_vonage_events(
            _request("not json", signed_headers("not json")), 123
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Answer URL (/ncco)
# ---------------------------------------------------------------------------


async def test_ncco_requires_signature(route_env):
    route_env()
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_ncco_webhook(
            7, 123, 11, _request("", {}, path="/api/v1/telephony/ncco", method="GET")
        )
    assert exc.value.status_code == 401


async def test_ncco_rejects_workflow_mismatch(route_env, signed_headers):
    route_env()
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_ncco_webhook(
            999,
            123,
            11,
            _request(
                "", signed_headers(None, include_payload_hash=False), method="GET"
            ),
        )
    assert exc.value.status_code == 404


async def test_ncco_rejects_run_from_other_org(route_env, signed_headers):
    env = route_env()
    env.db.get_workflow_run.return_value = None  # org-scoped lookup misses
    with pytest.raises(HTTPException) as exc:
        await vonage_routes.handle_ncco_webhook(
            7,
            123,
            99,
            _request(
                "", signed_headers(None, include_payload_hash=False), method="GET"
            ),
        )
    assert exc.value.status_code == 404


async def test_ncco_shape_for_signed_answer(route_env, signed_headers):
    from api.services.telephony.providers.vonage.auth import verify_ws_token

    route_env()
    with patch(
        "api.services.telephony.providers.vonage.provider.get_backend_endpoints",
        new=AsyncMock(return_value=("https://dograh.test", "wss://dograh.test")),
    ):
        ncco = await vonage_routes.handle_ncco_webhook(
            7,
            123,
            11,
            _request(
                "", signed_headers(None, include_payload_hash=False), method="GET"
            ),
            uuid=CALL_UUID,
        )

    assert len(ncco) == 1 and ncco[0]["action"] == "connect"
    endpoint = ncco[0]["endpoint"][0]
    assert endpoint["type"] == "websocket"
    assert endpoint["uri"] == "wss://dograh.test/api/v1/telephony/ws/7/11/123"
    assert endpoint["content-type"] == "audio/l16;rate=16000"
    assert endpoint["authorization"] == {"type": "vonage"}
    assert endpoint["headers"]["call_uuid"] == CALL_UUID
    assert verify_ws_token(
        endpoint["headers"]["dograh_ws_token"],
        "vonage-signature-secret-0123456789",
        organization_id=11,
        workflow_id=7,
        workflow_run_id=123,
        telephony_configuration_id=5,
    )
    # No credential material in the NCCO.
    serialized = json.dumps(ncco)
    assert "PRIVATE KEY" not in serialized
    assert "vonage-signature-secret" not in serialized


def test_validation_error_response_is_ncco_not_twiml():
    from api.errors.telephony_errors import TelephonyError
    from api.services.telephony.providers.vonage.provider import VonageProvider

    response = VonageProvider.generate_validation_error_response(
        TelephonyError.PHONE_NUMBER_NOT_CONFIGURED
    )
    assert response.media_type == "application/json"
    ncco = json.loads(response.body)
    assert ncco[-1] == {"action": "hangup"}
    assert ncco[0]["action"] == "talk"
