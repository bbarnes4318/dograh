"""Inbound Vonage calls through the real dispatcher and database."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.providers.vonage.auth import verify_ws_token

SECRET_A = "org-a-signature-secret"
SECRET_B = "org-b-signature-secret"


def _vonage_credentials(rsa_pem, api_key, secret):
    return {
        "api_key": api_key,
        "api_secret": "api-secret",
        "application_id": "aaaaaaaa-bbbb-cccc-dddd-0123456789ab",
        "private_key": rsa_pem,
        "signature_secret": secret,
    }


async def _tenant(db, suffix, rsa_pem, api_key, secret, number):
    user, _ = await db.get_or_create_user_by_provider_id(f"vonage_in_user_{suffix}")
    org, _ = await db.get_or_create_organization_by_provider_id(
        f"vonage_in_org_{suffix}", user.id
    )
    await db.update_user_selected_organization(user.id, org.id)
    workflow = await db.create_workflow(
        f"Inbound {suffix}", {"nodes": [], "edges": []}, user.id, org.id
    )
    cfg = await db.create_telephony_configuration(
        organization_id=org.id,
        name=f"Vonage {suffix}",
        provider="vonage",
        credentials=_vonage_credentials(rsa_pem, api_key, secret),
        is_default_outbound=True,
    )
    phone = await db.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=cfg.id,
        address=number,
        inbound_workflow_id=workflow.id,
    )
    return SimpleNamespace(user=user, org=org, workflow=workflow, cfg=cfg, phone=phone)


@pytest.fixture
def dispatcher_patches():
    from api.routes import telephony as telephony_routes

    slot = object()
    with (
        patch.object(
            telephony_routes,
            "authorize_workflow_run_start",
            new=AsyncMock(
                return_value=SimpleNamespace(has_quota=True, error_message=None)
            ),
        ),
        patch.object(
            telephony_routes,
            "get_backend_endpoints",
            new=AsyncMock(return_value=("https://dograh.test", "wss://dograh.test")),
        ),
        patch.object(telephony_routes, "call_concurrency") as concurrency,
    ):
        concurrency.acquire_org_slot = AsyncMock(return_value=slot)
        concurrency.bind_workflow_run = AsyncMock()
        concurrency.release_workflow_run_slot = AsyncMock()
        concurrency.release_slot = AsyncMock()
        yield concurrency


def _answer_body(to="14155550100", uuid="in-call-uuid"):
    return json.dumps(
        {
            "to": to,
            "from": "14155559999",
            "uuid": uuid,
            "conversation_uuid": "CON-in",
            "region_url": "https://api-us-3.vonage.com",
        },
        separators=(",", ":"),
    )


async def _post(client, body, headers):
    return await client.post(
        "/api/v1/telephony/inbound/run",
        content=body,
        headers={**headers, "content-type": "application/json"},
    )


async def test_inbound_call_resolves_tenant_and_returns_authenticated_ncco(
    test_client_factory, db_session, rsa_keypair, signed_headers, dispatcher_patches
):
    a = await _tenant(
        db_session, "a", rsa_keypair[0], "key-a", SECRET_A, "+14155550100"
    )
    body = _answer_body()

    async with test_client_factory(a.user) as client:
        resp = await _post(
            client,
            body,
            signed_headers(body, signature_secret=SECRET_A, api_key="key-a"),
        )

    assert resp.status_code == 200, resp.text
    ncco = resp.json()
    assert ncco[0]["action"] == "connect"
    endpoint = ncco[0]["endpoint"][0]
    assert endpoint["authorization"] == {"type": "vonage"}
    assert endpoint["content-type"] == "audio/l16;rate=16000"
    assert endpoint["headers"]["call_uuid"] == "in-call-uuid"
    run_id = int(endpoint["headers"]["workflow_run_id"])
    assert endpoint["uri"] == (
        f"wss://dograh.test/api/v1/telephony/ws/{a.workflow.id}/{a.org.id}/{run_id}"
    )
    assert ncco[0]["eventUrl"] == [
        f"https://dograh.test/api/v1/telephony/vonage/events/{run_id}"
    ]

    run = await db_session.get_workflow_run(run_id, organization_id=a.org.id)
    assert run.workflow_id == a.workflow.id
    assert run.mode == "vonage"
    assert run.initial_context["telephony_configuration_id"] == a.cfg.id
    assert run.initial_context["caller_number"] == "+14155559999"
    assert run.initial_context["called_number"] == "+14155550100"
    assert run.gathered_context["call_id"] == "in-call-uuid"

    assert verify_ws_token(
        endpoint["headers"]["dograh_ws_token"],
        SECRET_A,
        organization_id=a.org.id,
        workflow_id=a.workflow.id,
        workflow_run_id=run_id,
        telephony_configuration_id=a.cfg.id,
    )


async def test_inbound_rejects_unsigned_webhook(
    test_client_factory, db_session, rsa_keypair, dispatcher_patches
):
    a = await _tenant(
        db_session, "unsigned", rsa_keypair[0], "key-u", SECRET_A, "+14155550101"
    )
    body = _answer_body(to="14155550101")
    async with test_client_factory(a.user) as client:
        resp = await _post(client, body, {})
    # No signed claims -> no account id -> no route; hang up with NCCO.
    ncco = resp.json()
    assert ncco[-1] == {"action": "hangup"}
    dispatcher_patches.acquire_org_slot.assert_not_awaited()


async def test_inbound_signed_by_other_tenant_secret_rejected(
    test_client_factory, db_session, rsa_keypair, signed_headers, dispatcher_patches
):
    a = await _tenant(
        db_session, "victim", rsa_keypair[0], "key-v", SECRET_A, "+14155550102"
    )
    await _tenant(
        db_session, "attacker", rsa_keypair[0], "key-x", SECRET_B, "+14155550103"
    )
    body = _answer_body(to="14155550102")

    async with test_client_factory(a.user) as client:
        # Claims org A's api_key, but signed with org B's secret.
        resp = await _post(
            client,
            body,
            signed_headers(body, signature_secret=SECRET_B, api_key="key-v"),
        )
    assert resp.json()[-1] == {"action": "hangup"}
    dispatcher_patches.acquire_org_slot.assert_not_awaited()


async def test_inbound_other_tenant_account_cannot_reach_number(
    test_client_factory, db_session, rsa_keypair, signed_headers, dispatcher_patches
):
    a = await _tenant(
        db_session, "owner", rsa_keypair[0], "key-o", SECRET_A, "+14155550104"
    )
    await _tenant(
        db_session, "other", rsa_keypair[0], "key-p", SECRET_B, "+14155550105"
    )
    body = _answer_body(to="14155550104")

    async with test_client_factory(a.user) as client:
        # Validly signed by org B's account, dialing org A's number.
        resp = await _post(
            client,
            body,
            signed_headers(body, signature_secret=SECRET_B, api_key="key-p"),
        )
    assert resp.json()[-1] == {"action": "hangup"}
    dispatcher_patches.acquire_org_slot.assert_not_awaited()


async def test_inbound_number_without_workflow_hangs_up(
    test_client_factory, db_session, rsa_keypair, signed_headers, dispatcher_patches
):
    a = await _tenant(
        db_session, "noflow", rsa_keypair[0], "key-n", SECRET_A, "+14155550106"
    )
    await db_session.create_phone_number(
        organization_id=a.org.id,
        telephony_configuration_id=a.cfg.id,
        address="+14155550107",
    )
    body = _answer_body(to="14155550107")
    async with test_client_factory(a.user) as client:
        resp = await _post(
            client,
            body,
            signed_headers(body, signature_secret=SECRET_A, api_key="key-n"),
        )
    ncco = resp.json()
    assert ncco[0]["action"] == "talk"
    assert ncco[-1] == {"action": "hangup"}


# ---------------------------------------------------------------------------
# configure_inbound: shared application safety
# ---------------------------------------------------------------------------


class _AppAPI:
    def __init__(self, app_data, put_status=200):
        self.app_data = app_data
        self.put_status = put_status
        self.puts = []

    def session(self, *a, **kw):
        api = self

        class _Resp:
            def __init__(self, status, data=None):
                self.status = status
                self._data = data

            async def json(self):
                return self._data

            async def text(self):
                return json.dumps(self._data or {})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, auth=None):
                return _Resp(200, json.loads(json.dumps(api.app_data)))

            def put(self, url, json=None, auth=None):
                api.puts.append(json)
                return _Resp(api.put_status, {})

        return _S()


@pytest.fixture
def app_api(monkeypatch):
    from api.services.telephony.providers.vonage import provider as provider_module

    monkeypatch.setattr(
        provider_module,
        "get_backend_endpoints",
        AsyncMock(return_value=("https://dograh.test", "wss://dograh.test")),
    )

    def _install(app_data, put_status=200):
        api = _AppAPI(app_data, put_status)
        monkeypatch.setattr(provider_module.aiohttp, "ClientSession", api.session)
        return api

    return _install


async def test_configure_inbound_sets_answer_url_and_signed_callbacks(
    vonage_provider, app_api
):
    api = app_api({"name": "app", "capabilities": {"voice": {"webhooks": {}}}})
    result = await vonage_provider().configure_inbound(
        "+15551230002", "https://dograh.test/api/v1/telephony/inbound/run"
    )
    assert result.ok
    voice = api.puts[0]["capabilities"]["voice"]
    assert voice["signed_callbacks"] is True
    assert voice["webhooks"]["answer_url"] == {
        "address": "https://dograh.test/api/v1/telephony/inbound/run",
        "http_method": "POST",
    }
    assert (
        voice["webhooks"]["event_url"]["address"]
        == "https://dograh.test/api/v1/telephony/vonage/events"
    )


async def test_configure_inbound_is_idempotent_for_shared_application(
    vonage_provider, app_api
):
    already = {
        "name": "app",
        "capabilities": {
            "voice": {
                "signed_callbacks": True,
                "webhooks": {
                    "answer_url": {
                        "address": "https://dograh.test/api/v1/telephony/inbound/run",
                        "http_method": "POST",
                    },
                    "event_url": {
                        "address": "https://dograh.test/api/v1/telephony/vonage/events",
                        "http_method": "POST",
                    },
                },
            }
        },
    }
    api = app_api(already)
    result = await vonage_provider().configure_inbound(
        "+15551230003", "https://dograh.test/api/v1/telephony/inbound/run"
    )
    assert result.ok
    assert api.puts == []  # second config sharing the app writes nothing


async def test_configure_inbound_clear_never_touches_shared_application(
    vonage_provider, app_api
):
    api = app_api({"name": "app"})
    result = await vonage_provider().configure_inbound("+15551230002", None)
    assert result.ok
    assert api.puts == []


async def test_configure_inbound_reports_application_api_auth_error(
    vonage_provider, app_api
):
    api = app_api({"name": "app", "capabilities": {}}, put_status=401)
    result = await vonage_provider().configure_inbound(
        "+15551230002", "https://dograh.test/api/v1/telephony/inbound/run"
    )
    assert not result.ok
    assert "401" in result.message
    assert "api-secret" not in result.message
