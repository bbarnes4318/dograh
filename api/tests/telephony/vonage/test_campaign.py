"""Campaigns pinned to a Vonage configuration (DB-backed dispatcher)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from api.services.campaign import campaign_call_dispatcher as dispatcher_module
from api.services.campaign.campaign_call_dispatcher import CampaignCallDispatcher
from api.services.telephony.providers.vonage import client as client_module
from api.services.telephony.providers.vonage.provider import VonageProvider

SECRET = "campaign-signature-secret"


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.headers = {}

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def vonage_http(monkeypatch):
    requests = []
    counter = {"n": 0}

    def session(*a, **kw):
        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(self, method, url, json=None, headers=None, **kw):
                requests.append({"method": method, "url": url, "json": json})
                counter["n"] += 1
                return _Resp(
                    201, {"uuid": f"campaign-call-{counter['n']}", "status": "started"}
                )

        return _S()

    monkeypatch.setattr(client_module, "_new_session", session)
    return requests


@pytest.fixture
def infra_patches():
    endpoints = AsyncMock(return_value=("https://dograh.test", "wss://dograh.test"))
    with (
        patch.object(dispatcher_module, "get_backend_endpoints", new=endpoints),
        patch(
            "api.services.telephony.providers.vonage.provider.get_backend_endpoints",
            new=endpoints,
        ),
        patch.object(
            dispatcher_module,
            "authorize_workflow_run_start",
            new=AsyncMock(
                return_value=SimpleNamespace(has_quota=True, error_message=None)
            ),
        ),
        patch.object(dispatcher_module, "call_concurrency") as concurrency,
        patch.object(dispatcher_module, "rate_limiter") as limiter,
        patch.object(dispatcher_module, "circuit_breaker") as breaker,
    ):
        concurrency.bind_workflow_run = AsyncMock()
        concurrency.release_slot = AsyncMock()
        concurrency.release_workflow_run_slot = AsyncMock()
        limiter.store_workflow_from_number_mapping = AsyncMock()
        limiter.release_from_number = AsyncMock()
        breaker.record_and_evaluate = AsyncMock()
        yield SimpleNamespace(concurrency=concurrency, limiter=limiter)


def _vonage_credentials(rsa_pem, api_key="campaign-key"):
    return {
        "api_key": api_key,
        "api_secret": "s",
        "application_id": "aaaaaaaa-bbbb-cccc-dddd-0123456789ab",
        "private_key": rsa_pem,
        "signature_secret": SECRET,
    }


async def _setup(db, suffix, rsa_pem, *, vonage_default: bool):
    user, _ = await db.get_or_create_user_by_provider_id(
        f"vonage_campaign_user_{suffix}"
    )
    org, _ = await db.get_or_create_organization_by_provider_id(
        f"vonage_campaign_org_{suffix}", user.id
    )
    workflow = await db.create_workflow(
        f"Campaign {suffix}", {"nodes": [], "edges": []}, user.id, org.id
    )
    twilio = await db.create_telephony_configuration(
        organization_id=org.id,
        name="Twilio",
        provider="twilio",
        credentials={"account_sid": "AC123", "auth_token": "t"},
        is_default_outbound=not vonage_default,
    )
    await db.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=twilio.id,
        address="+15550000001",
    )
    vonage = await db.create_telephony_configuration(
        organization_id=org.id,
        name="Vonage",
        provider="vonage",
        credentials=_vonage_credentials(rsa_pem, api_key=f"key-{suffix}"),
        is_default_outbound=vonage_default,
    )
    if vonage_default:
        await db.set_default_telephony_configuration(vonage.id, org.id)
    await db.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=vonage.id,
        address="+15559990001",
    )
    return SimpleNamespace(
        user=user, org=org, workflow=workflow, twilio=twilio, vonage=vonage
    )


async def _campaign(db, env, telephony_configuration_id):
    return await db.create_campaign(
        name="Vonage campaign",
        workflow_id=env.workflow.id,
        source_type="csv",
        source_id="src",
        user_id=env.user.id,
        organization_id=env.org.id,
        telephony_configuration_id=telephony_configuration_id,
    )


async def test_explicit_vonage_config_beats_org_default(db_session, rsa_keypair):
    env = await _setup(db_session, "explicit", rsa_keypair[0], vonage_default=False)
    campaign = await _campaign(db_session, env, env.vonage.id)

    provider = await CampaignCallDispatcher().get_provider_for_campaign(campaign)

    assert isinstance(provider, VonageProvider)
    assert provider.from_numbers == ["+15559990001"]


async def test_org_default_vonage_config_used_for_legacy_campaign(
    db_session, rsa_keypair
):
    env = await _setup(db_session, "default", rsa_keypair[0], vonage_default=True)
    campaign = await _campaign(db_session, env, None)

    provider = await CampaignCallDispatcher().get_provider_for_campaign(campaign)

    assert isinstance(provider, VonageProvider)


@pytest.mark.parametrize("is_retry", [False, True])
async def test_campaign_call_and_retry_dial_through_vonage(
    db_session, rsa_keypair, vonage_http, infra_patches, is_retry
):
    env = await _setup(
        db_session, f"dispatch_{is_retry}", rsa_keypair[0], vonage_default=False
    )
    campaign = await _campaign(db_session, env, env.vonage.id)
    context = {"phone_number": "+14155550123"}
    if is_retry:
        context.update({"is_retry": True, "retry_reason": "busy"})
    queued = await db_session.create_queued_run(
        campaign_id=campaign.id,
        source_uuid=f"src-{is_retry}",
        context_variables=context,
        retry_count=1 if is_retry else 0,
        retry_reason="busy" if is_retry else None,
    )

    dispatcher = CampaignCallDispatcher()
    slot = object()
    with patch.object(
        dispatcher, "acquire_from_number", new=AsyncMock(return_value="+15559990001")
    ):
        run = await dispatcher.dispatch_call(queued, campaign, slot)

    # Placed through Vonage's Voice API with the campaign's Vonage caller ID.
    call = vonage_http[0]
    assert call["url"] == "https://api.nexmo.com/v1/calls"
    assert call["json"]["from"] == {"type": "phone", "number": "15559990001"}
    assert call["json"]["to"] == [{"type": "phone", "number": "14155550123"}]
    assert "/api/v1/telephony/ncco?" in call["json"]["answer_url"][0]
    assert f"workflow_run_id={run.id}" in call["json"]["answer_url"][0]
    assert call["json"]["event_url"] == [
        f"https://dograh.test/api/v1/telephony/vonage/events/{run.id}"
    ]

    stored = await db_session.get_workflow_run(run.id, organization_id=env.org.id)
    assert stored.mode == "vonage"
    assert stored.initial_context["telephony_configuration_id"] == env.vonage.id
    assert stored.initial_context["caller_number"] == "+15559990001"
    assert stored.gathered_context["call_id"].startswith("campaign-call-")
    assert stored.gathered_context["call_uuid"] == stored.gathered_context["call_id"]
    # Concurrency is enforced by the provider-agnostic slot machinery.
    infra_patches.concurrency.bind_workflow_run.assert_awaited_once_with(slot, run.id)


async def test_campaign_call_completion_via_signed_vonage_event(
    db_session, rsa_keypair, vonage_http, infra_patches, signed_headers
):
    from api.services.telephony import status_processor
    from api.services.telephony.providers.vonage import routes as vonage_routes

    env = await _setup(db_session, "complete", rsa_keypair[0], vonage_default=False)
    campaign = await _campaign(db_session, env, env.vonage.id)
    queued = await db_session.create_queued_run(
        campaign_id=campaign.id,
        source_uuid="src",
        context_variables={"phone_number": "+14155550123"},
    )
    dispatcher = CampaignCallDispatcher()
    with patch.object(
        dispatcher, "acquire_from_number", new=AsyncMock(return_value="+15559990001")
    ):
        run = await dispatcher.dispatch_call(queued, campaign, object())
    stored = await db_session.get_workflow_run(run.id, organization_id=env.org.id)
    call_uuid = stored.gathered_context["call_id"]

    body = json.dumps(
        {
            "uuid": call_uuid,
            "status": "completed",
            "duration": "37",
            "direction": "outbound",
            "timestamp": "2026-10-01T12:00:00.000Z",
        }
    )
    headers = signed_headers(body, signature_secret=SECRET, api_key="key-complete")

    async def receive():
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/telephony/vonage/events/{run.id}",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "scheme": "https",
            "server": ("dograh.test", 443),
        },
        receive,
    )

    with (
        patch.object(status_processor, "campaign_call_dispatcher") as slot_owner,
        patch.object(status_processor, "circuit_breaker") as breaker,
        patch.object(status_processor, "_record_caller_id_outcome", new=AsyncMock()),
    ):
        slot_owner.release_call_slot = AsyncMock()
        breaker.record_and_evaluate = AsyncMock()
        result = await vonage_routes.handle_vonage_events(request, run.id)

    assert result == {"status": "ok"}
    done = await db_session.get_workflow_run(run.id, organization_id=env.org.id)
    assert done.state == "completed"
    assert done.is_completed is True
    callbacks = done.logs["telephony_status_callbacks"]
    assert callbacks[-1]["status"] == "completed"
    assert callbacks[-1]["vonage_status"] == "completed"
    slot_owner.release_call_slot.assert_awaited_once_with(run.id)
    breaker.record_and_evaluate.assert_awaited_once_with(campaign.id, is_failure=False)
