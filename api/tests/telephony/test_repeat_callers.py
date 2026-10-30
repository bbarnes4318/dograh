"""One inbound call per customer number; handoffs between AI agents always
answered. See api/services/telephony/repeat_callers.py."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.models import OrganizationModel, UserModel
from api.enums import CallType
from api.services.telephony import ari_manager, repeat_callers
from api.services.telephony.ari_manager import ARIConnection
from api.services.telephony.repeat_callers import check_inbound_caller

GRAPH = {
    "nodes": [
        {"id": "1", "type": "startCall", "data": {"name": "Start", "prompt": "Hi"}},
        {"id": "2", "type": "endCall", "data": {"name": "End", "prompt": "Bye"}},
    ],
    "edges": [{"id": "e1", "source": "1", "target": "2", "data": {"label": "End"}}],
}


def _fake_db(*, ours=False, live=False, answered=False):
    return SimpleNamespace(
        is_organization_phone_number=AsyncMock(return_value=ours),
        number_on_live_call=AsyncMock(return_value=live),
        inbound_caller_has_answered_call=AsyncMock(return_value=answered),
    )


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


async def test_first_call_is_answered(monkeypatch):
    monkeypatch.setattr(repeat_callers, "db_client", _fake_db())
    decision = await check_inbound_caller(11, "+15557650000")
    assert decision.admit and not decision.ai_transfer


async def test_second_call_from_the_same_number_is_refused(monkeypatch):
    db = _fake_db(answered=True)
    monkeypatch.setattr(repeat_callers, "db_client", db)
    decision = await check_inbound_caller(11, "(555) 765-0000")
    assert not decision.admit
    db.inbound_caller_has_answered_call.assert_awaited_once_with(11, "5557650000")


async def test_transfer_from_another_ai_agent_is_answered(monkeypatch):
    # The customer is still on the call with the agent doing the transfer.
    monkeypatch.setattr(repeat_callers, "db_client", _fake_db(live=True, answered=True))
    decision = await check_inbound_caller(11, "+15557650000")
    assert decision.admit and decision.ai_transfer


async def test_transfer_showing_our_own_number_is_answered(monkeypatch):
    db = _fake_db(ours=True, answered=True)
    monkeypatch.setattr(repeat_callers, "db_client", db)
    decision = await check_inbound_caller(11, "17207940675")
    assert decision.admit and decision.ai_transfer
    db.is_organization_phone_number.assert_awaited_once_with(11, "+17207940675")


@pytest.mark.parametrize(
    "caller", [None, "", "unknown", "anonymous", "sip:a@b", "1000"]
)
async def test_numbers_that_are_not_a_customer_number_are_not_checked(
    monkeypatch, caller
):
    db = _fake_db(answered=True)
    monkeypatch.setattr(repeat_callers, "db_client", db)
    assert (await check_inbound_caller(11, caller)).admit
    db.inbound_caller_has_answered_call.assert_not_awaited()


async def test_rule_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("INBOUND_ONE_CALL_PER_CALLER", "false")
    monkeypatch.setattr(repeat_callers, "db_client", _fake_db(answered=True))
    assert (await check_inbound_caller(11, "+15557650000")).admit


async def test_database_error_answers_the_call(monkeypatch):
    db = _fake_db()
    db.inbound_caller_has_answered_call = AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr(repeat_callers, "db_client", db)
    assert (await check_inbound_caller(11, "+15557650000")).admit


# ---------------------------------------------------------------------------
# ARI inbound path
# ---------------------------------------------------------------------------


class _InboundARIConnection(ARIConnection):
    def __init__(self):
        super().__init__(
            organization_id=11,
            telephony_configuration_id=10,
            ari_endpoint="http://asterisk.test:8088",
            app_name="dograh",
            app_password="secret",
        )
        self.deleted_channels = []
        self.started = []

    async def _delete_channel(self, channel_id):
        self.deleted_channels.append(channel_id)

    async def _capture_external_pbx_call(self, channel_id, channel_name):
        return None

    async def _answer_channel(self, channel_id):
        pass

    async def _handle_stasis_start(self, channel_id, state, run_id, workflow_id):
        self.started.append(channel_id)


def _patch_ari_inbound(monkeypatch, decision):
    fake_db = SimpleNamespace(
        find_active_phone_number_for_inbound=AsyncMock(
            return_value=SimpleNamespace(id=1, address="+17207940675")
        ),
        get_workflow=AsyncMock(return_value=SimpleNamespace(id=15, user_id=99)),
        create_workflow_run=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    monkeypatch.setattr(ari_manager, "db_client", fake_db)
    monkeypatch.setattr(
        ari_manager, "check_inbound_caller", AsyncMock(return_value=decision)
    )
    monkeypatch.setattr(ari_manager, "ari_inbound_admitted", lambda *a, **k: True)
    monkeypatch.setattr(
        ari_manager, "screening_required", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        ari_manager,
        "resolve_inbound_workflow_id",
        AsyncMock(return_value=(15, None)),
    )
    monkeypatch.setattr(
        ari_manager,
        "call_concurrency",
        SimpleNamespace(
            acquire_org_slot=AsyncMock(return_value=object()),
            bind_workflow_run=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        ari_manager,
        "prepare_workflow_run_inputs",
        AsyncMock(return_value=SimpleNamespace(definition_id=3)),
    )
    monkeypatch.setattr(
        ari_manager,
        "authorize_workflow_run_start",
        AsyncMock(return_value=SimpleNamespace(has_quota=True)),
    )
    return fake_db


_EVENT = {
    "channel": {
        "name": "PJSIP/hopwhistle-callback-0001",
        "caller": {"number": "+15557650000"},
        "dialplan": {"exten": "+17207940675"},
    }
}


async def test_ari_hangs_up_a_repeat_caller(monkeypatch):
    fake_db = _patch_ari_inbound(
        monkeypatch, repeat_callers.InboundCallerDecision(admit=False, reason="again")
    )
    conn = _InboundARIConnection()
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)
    assert conn.deleted_channels == ["chan-1"]
    assert conn.started == []
    fake_db.create_workflow_run.assert_not_awaited()


async def test_ari_answers_an_ai_transfer_and_marks_the_run(monkeypatch):
    fake_db = _patch_ari_inbound(
        monkeypatch,
        repeat_callers.InboundCallerDecision(admit=True, ai_transfer=True),
    )
    conn = _InboundARIConnection()
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)
    assert conn.started == ["chan-1"]
    context = fake_db.create_workflow_run.await_args.kwargs["initial_context"]
    assert context["ai_transfer"] is True


async def test_ari_customer_call_is_not_marked_a_transfer(monkeypatch):
    fake_db = _patch_ari_inbound(
        monkeypatch, repeat_callers.InboundCallerDecision(admit=True)
    )
    conn = _InboundARIConnection()
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)
    context = fake_db.create_workflow_run.await_args.kwargs["initial_context"]
    assert "ai_transfer" not in context


# ---------------------------------------------------------------------------
# The queries, against the test database
# ---------------------------------------------------------------------------


@pytest.fixture
async def org_workflow(db_session, async_session):
    org = OrganizationModel(provider_id="test-org-repeat-callers")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(
        provider_id="test-user-repeat-callers", selected_organization_id=org.id
    )
    async_session.add(user)
    await async_session.flush()
    workflow = await db_session.create_workflow(
        name="Inbound agent",
        workflow_definition=GRAPH,
        user_id=user.id,
        organization_id=org.id,
    )
    return org, user, workflow


async def _run(db, org, user, workflow, call_type, context, completed=True):
    run = await db.create_workflow_run(
        name="run",
        workflow_id=workflow.id,
        mode="ari",
        user_id=user.id,
        call_type=call_type,
        initial_context=context,
        organization_id=org.id,
    )
    if completed:
        await db.update_workflow_run(run_id=run.id, is_completed=True)
    return run


async def test_query_finds_an_earlier_inbound_call_in_any_format(
    db_session, org_workflow
):
    org, user, workflow = org_workflow
    db = db_session
    assert not await db.inbound_caller_has_answered_call(org.id, "5557650000")

    await _run(
        db,
        org,
        user,
        workflow,
        CallType.INBOUND,
        {"caller_number": "+1 (555) 765-0000"},
    )
    assert await db.inbound_caller_has_answered_call(org.id, "5557650000")
    assert not await db.inbound_caller_has_answered_call(org.id, "5557651111")
    assert not await db.inbound_caller_has_answered_call(org.id + 999, "5557650000")


async def test_query_ignores_ai_transfers_and_outbound_calls(db_session, org_workflow):
    org, user, workflow = org_workflow
    db = db_session
    await _run(
        db,
        org,
        user,
        workflow,
        CallType.INBOUND,
        {"caller_number": "+15557650000", "ai_transfer": True},
    )
    await _run(
        db, org, user, workflow, CallType.OUTBOUND, {"called_number": "+15557650000"}
    )
    assert not await db.inbound_caller_has_answered_call(org.id, "5557650000")


async def test_live_call_query(db_session, org_workflow):
    org, user, workflow = org_workflow
    db = db_session
    since = datetime.now(UTC) - timedelta(hours=2)
    await _run(
        db, org, user, workflow, CallType.INBOUND, {"caller_number": "+15557650000"}
    )
    assert not await db.number_on_live_call(org.id, "5557650000", since)

    # An outbound AI call to the customer, still in progress.
    await _run(
        db,
        org,
        user,
        workflow,
        CallType.OUTBOUND,
        {"called_number": "15557650000"},
        completed=False,
    )
    assert await db.number_on_live_call(org.id, "5557650000", since)
    assert not await db.number_on_live_call(
        org.id, "5557650000", datetime.now(UTC) + timedelta(minutes=1)
    )


async def test_own_phone_number_query(db_session, org_workflow):
    org, _, _ = org_workflow
    config = await db_session.create_telephony_configuration(
        organization_id=org.id, name="ari", provider="ari", credentials={}
    )
    await db_session.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=config.id,
        address="+17207940675",
        country_code="US",
    )
    assert await db_session.is_organization_phone_number(org.id, "+17207940675")
    assert not await db_session.is_organization_phone_number(org.id, "+15557650000")
