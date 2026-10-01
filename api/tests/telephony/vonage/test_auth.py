"""Signed-callback and media-WebSocket authentication for Vonage."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.providers.vonage.auth import (
    SIGNED_CALLBACK_LEEWAY_SECONDS,
    SIGNED_CALLBACK_MAX_AGE_SECONDS,
    WS_TOKEN_HEADER,
    make_ws_token,
    verify_ws_token,
)

SIGNATURE_SECRET = "vonage-signature-secret-0123456789"
URL = "https://example.test/api/v1/telephony/vonage/events/123"
BODY = json.dumps({"uuid": "u-1", "status": "answered"}, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Signed webhooks
# ---------------------------------------------------------------------------


async def test_valid_signed_webhook(vonage_provider, signed_headers):
    provider = vonage_provider()
    assert await provider.verify_inbound_signature(
        URL, json.loads(BODY), signed_headers(BODY), BODY
    )


async def test_expired_jwt_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    old = time.time() - 3600
    headers = signed_headers(BODY, iat=old, exp=old + 60)
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_stale_iat_rejected_as_replay(vonage_provider, signed_headers):
    provider = vonage_provider()
    stale = time.time() - (
        SIGNED_CALLBACK_MAX_AGE_SECONDS + SIGNED_CALLBACK_LEEWAY_SECONDS + 5
    )
    assert not await provider.verify_inbound_signature(
        URL, {}, signed_headers(BODY, iat=stale), BODY
    )


async def test_recent_iat_within_window_accepted(vonage_provider, signed_headers):
    provider = vonage_provider()
    recent = time.time() - (SIGNED_CALLBACK_MAX_AGE_SECONDS - 30)
    assert await provider.verify_inbound_signature(
        URL, {}, signed_headers(BODY, iat=recent), BODY
    )


async def test_future_iat_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    future = time.time() + SIGNED_CALLBACK_LEEWAY_SECONDS + 120
    assert not await provider.verify_inbound_signature(
        URL, {}, signed_headers(BODY, iat=future), BODY
    )


async def test_wrong_signature_secret_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    headers = signed_headers(BODY, signature_secret="another-secret")
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_wrong_api_key_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    headers = signed_headers(BODY, api_key="someone-else")
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_missing_api_key_claim_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    headers = signed_headers(BODY, api_key=None)
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_wrong_application_id_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    headers = signed_headers(
        BODY, application_id="bbbbbbbb-bbbb-cccc-dddd-0123456789ab"
    )
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_wrong_issuer_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    headers = signed_headers(BODY, iss="Mallory")
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_modified_body_rejected(vonage_provider, signed_headers):
    provider = vonage_provider()
    headers = signed_headers(BODY)
    tampered = BODY.replace("answered", "completed")
    assert not await provider.verify_inbound_signature(URL, {}, headers, tampered)


async def test_missing_authorization_header_rejected(vonage_provider):
    provider = vonage_provider()
    assert not await provider.verify_inbound_signature(URL, {}, {}, BODY)


async def test_non_bearer_authorization_rejected(vonage_provider):
    provider = vonage_provider()
    headers = {"authorization": "Basic dXNlcjpwYXNz"}
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_alg_none_token_rejected(vonage_provider):
    import jwt as pyjwt

    provider = vonage_provider()
    token = pyjwt.encode(
        {"iss": "Vonage", "api_key": "abcd1234", "iat": int(time.time())},
        key="",
        algorithm="none",
    )
    headers = {"authorization": f"Bearer {token}"}
    assert not await provider.verify_inbound_signature(URL, {}, headers, BODY)


async def test_missing_signature_secret_fails_closed(vonage_provider, signed_headers):
    provider = vonage_provider(signature_secret=None)
    assert not await provider.verify_inbound_signature(
        URL, {}, signed_headers(BODY), BODY
    )


async def test_verify_webhook_signature_uses_same_rules(
    vonage_provider, signed_headers
):
    provider = vonage_provider()
    token = signed_headers(None, include_payload_hash=False)["authorization"].split()[1]
    assert await provider.verify_webhook_signature(URL, {}, token)
    assert not await provider.verify_webhook_signature(URL, {}, token + "x")


async def test_secrets_never_logged(vonage_provider, signed_headers, rsa_keypair):
    from loguru import logger

    messages = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        provider = vonage_provider()
        headers = signed_headers(BODY, signature_secret="wrong")
        await provider.verify_inbound_signature(URL, {}, headers, BODY)
    finally:
        logger.remove(handler_id)
    joined = "\n".join(messages)
    token = headers["authorization"].split()[1]
    assert token not in joined
    assert SIGNATURE_SECRET not in joined
    assert "PRIVATE KEY" not in joined


# ---------------------------------------------------------------------------
# Media WebSocket
# ---------------------------------------------------------------------------


def _ws(headers: dict, first_message):
    ws = MagicMock()
    ws.headers = headers
    if isinstance(first_message, Exception):
        ws.receive = AsyncMock(side_effect=first_message)
    else:
        ws.receive = AsyncMock(return_value=first_message)
    ws.close = AsyncMock()
    return ws


def _connected(token, **extra):
    msg = {"event": "websocket:connected", "content-type": "audio/l16;rate=16000"}
    if token is not None:
        msg[WS_TOKEN_HEADER] = token
    msg.update(extra)
    return {"type": "websocket.receive", "text": json.dumps(msg)}


def _token(org=11, workflow=7, run=123, cfg=5, secret=SIGNATURE_SECRET):
    return make_ws_token(
        secret,
        organization_id=org,
        workflow_id=workflow,
        workflow_run_id=run,
        telephony_configuration_id=cfg,
    )


async def _auth(provider, ws, run, workflow_id=7, organization_id=11):
    return await provider.authenticate_websocket(
        ws, workflow_run=run, workflow_id=workflow_id, organization_id=organization_id
    )


async def test_ws_valid_vonage_connection(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws(signed_headers(None, include_payload_hash=False), _connected(_token()))
    assert await _auth(provider, ws, make_workflow_run())
    assert provider._ws_connected_message["event"] == "websocket:connected"


async def test_ws_missing_auth_rejected_without_reading(
    vonage_provider, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws({}, _connected(_token()))
    assert not await _auth(provider, ws, make_workflow_run())
    ws.receive.assert_not_awaited()


async def test_ws_invalid_auth_rejected(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws(
        signed_headers(None, signature_secret="nope", include_payload_hash=False),
        _connected(_token()),
    )
    assert not await _auth(provider, ws, make_workflow_run())


async def test_ws_missing_run_token_rejected(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws(signed_headers(None, include_payload_hash=False), _connected(None))
    assert not await _auth(provider, ws, make_workflow_run())


@pytest.mark.parametrize(
    "token_kwargs,label",
    [
        ({"org": 99}, "wrong organization"),
        ({"workflow": 99}, "wrong workflow"),
        ({"run": 999}, "wrong workflow run"),
        ({"cfg": 6}, "mismatched telephony configuration"),
        ({"secret": "other-config-secret"}, "other configuration's secret"),
    ],
)
async def test_ws_token_bound_to_run(
    vonage_provider, signed_headers, make_workflow_run, token_kwargs, label
):
    provider = vonage_provider()
    ws = _ws(
        signed_headers(None, include_payload_hash=False),
        _connected(_token(**token_kwargs)),
    )
    assert not await _auth(provider, ws, make_workflow_run()), label


async def test_ws_token_valid_for_other_run_cannot_attach(
    vonage_provider, signed_headers, make_workflow_run
):
    """A token issued for run 123 is useless against run 124."""
    provider = vonage_provider()
    ws = _ws(
        signed_headers(None, include_payload_hash=False), _connected(_token(run=123))
    )
    assert not await _auth(provider, ws, make_workflow_run(124))


async def test_ws_malformed_first_event_rejected(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws(
        signed_headers(None, include_payload_hash=False),
        {"type": "websocket.receive", "text": "{not json"},
    )
    assert not await _auth(provider, ws, make_workflow_run())


async def test_ws_binary_first_frame_rejected(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws(
        signed_headers(None, include_payload_hash=False),
        {"type": "websocket.receive", "bytes": b"\x00\x00" * 320},
    )
    assert not await _auth(provider, ws, make_workflow_run())


async def test_ws_wrong_first_event_rejected(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    msg = {"type": "websocket.receive", "text": json.dumps({"event": "websocket:dtmf"})}
    ws = _ws(signed_headers(None, include_payload_hash=False), msg)
    assert not await _auth(provider, ws, make_workflow_run())


async def test_ws_idle_connection_times_out(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    ws = _ws(signed_headers(None, include_payload_hash=False), {})
    with patch(
        "api.services.telephony.providers.vonage.provider.WS_CONNECTED_TIMEOUT_SECONDS",
        0.01,
    ):

        async def _never_sends():
            await asyncio.sleep(1)

        ws.receive = AsyncMock(side_effect=_never_sends)
        assert not await _auth(provider, ws, make_workflow_run())


def test_ws_token_helpers_constant_time_and_strict():
    token = _token()
    assert verify_ws_token(
        token,
        SIGNATURE_SECRET,
        organization_id=11,
        workflow_id=7,
        workflow_run_id=123,
        telephony_configuration_id=5,
    )
    assert not verify_ws_token(
        None,
        SIGNATURE_SECRET,
        organization_id=11,
        workflow_id=7,
        workflow_run_id=123,
        telephony_configuration_id=5,
    )
    assert not verify_ws_token(
        token,
        None,
        organization_id=11,
        workflow_id=7,
        workflow_run_id=123,
        telephony_configuration_id=5,
    )


# ---------------------------------------------------------------------------
# Shared telephony websocket route: auth happens before the run is consumed
# ---------------------------------------------------------------------------


async def test_route_rejects_unauthenticated_socket_before_state_flip(
    vonage_provider, make_workflow_run
):
    from api.routes import telephony as telephony_routes

    provider = vonage_provider()
    run = make_workflow_run()
    ws = _ws({}, _connected(_token()))

    with (
        patch.object(telephony_routes, "db_client") as db_client,
        patch.object(
            telephony_routes,
            "get_telephony_provider_for_run",
            new=AsyncMock(return_value=provider),
        ),
    ):
        db_client.get_workflow_run = AsyncMock(return_value=run)
        db_client.get_workflow = AsyncMock(
            return_value=SimpleNamespace(id=7, organization_id=11)
        )
        db_client.update_workflow_run = AsyncMock()

        await telephony_routes._handle_telephony_websocket(ws, 7, 11, 123)

    db_client.update_workflow_run.assert_not_awaited()  # still "initialized"
    ws.close.assert_awaited_with(code=4401, reason="Unauthorized")


async def test_route_runs_pipeline_for_authenticated_socket(
    vonage_provider, signed_headers, make_workflow_run
):
    from api.routes import telephony as telephony_routes

    provider = vonage_provider()
    run = make_workflow_run()
    ws = _ws(signed_headers(None, include_payload_hash=False), _connected(_token()))

    with (
        patch.object(telephony_routes, "db_client") as db_client,
        patch.object(
            telephony_routes,
            "get_telephony_provider_for_run",
            new=AsyncMock(return_value=provider),
        ),
        patch.object(provider, "handle_websocket", new=AsyncMock()) as handle,
    ):
        db_client.get_workflow_run = AsyncMock(return_value=run)
        db_client.get_workflow = AsyncMock(
            return_value=SimpleNamespace(id=7, organization_id=11)
        )
        db_client.update_workflow_run = AsyncMock()

        await telephony_routes._handle_telephony_websocket(ws, 7, 11, 123)

    db_client.update_workflow_run.assert_awaited_once_with(run_id=123, state="running")
    handle.assert_awaited_once()


async def test_handle_websocket_refuses_unauthenticated_socket(vonage_provider):
    provider = vonage_provider()
    ws = _ws({}, _connected(None))
    await provider.handle_websocket(ws, 7, 11, 123)
    ws.close.assert_awaited_with(code=4401, reason="Unauthorized")


async def test_handle_websocket_starts_pipeline_with_call_uuid(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    run = make_workflow_run()
    ws = _ws(signed_headers(None, include_payload_hash=False), _connected(_token()))
    assert await _auth(provider, ws, run)

    from api.db import db_client as real_db_client

    with (
        patch.object(
            real_db_client, "get_workflow_run", new=AsyncMock(return_value=run)
        ),
        patch(
            "api.services.pipecat.run_pipeline.run_pipeline_telephony",
            new=AsyncMock(),
        ) as run_pipeline,
    ):
        await provider.handle_websocket(ws, 7, 11, 123)

    kwargs = run_pipeline.await_args.kwargs
    assert kwargs["provider_name"] == "vonage"
    assert kwargs["call_id"] == run.gathered_context["call_id"]
    assert kwargs["transport_kwargs"] == {"call_uuid": run.gathered_context["call_id"]}
    assert kwargs["organization_id"] == 11


async def test_handle_websocket_falls_back_to_call_uuid_header(
    vonage_provider, signed_headers, make_workflow_run
):
    provider = vonage_provider()
    run = make_workflow_run(call_id=None)
    ws = _ws(
        signed_headers(None, include_payload_hash=False),
        _connected(_token(), call_uuid="from-ncco-header"),
    )
    assert await _auth(provider, ws, run)

    from api.db import db_client as real_db_client

    with (
        patch.object(
            real_db_client, "get_workflow_run", new=AsyncMock(return_value=run)
        ),
        patch(
            "api.services.pipecat.run_pipeline.run_pipeline_telephony",
            new=AsyncMock(),
        ) as run_pipeline,
    ):
        await provider.handle_websocket(ws, 7, 11, 123)

    assert run_pipeline.await_args.kwargs["call_id"] == "from-ncco-header"
