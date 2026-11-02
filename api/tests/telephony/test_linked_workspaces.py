"""Linked workspaces and shared Asterisk apps.

- A routing group's split can send calls to an agent in a linked workspace;
  that call's run is created in the agent's workspace.
- Several orgs can share one Asterisk ARI app. Asterisk hands each call to one
  of their connections, so a call is processed for the org it belongs to (its
  run or its called number), never assumed to be the connection's.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.telephony import (
    ari_manager,
    inbound_routing,
    inbound_routing_groups,
    linked_workspaces,
    repeat_callers,
)
from api.services.telephony.ari_manager import ARIConnection, ARIManager
from api.services.telephony.inbound_routing import resolve_inbound_workflow_id
from api.services.telephony.inbound_routing_groups import (
    RoutingGroupValidationError,
    validate_target_workflows,
    workflow_names,
)
from api.services.telephony.linked_workspaces import parse_linked_workspaces
from api.services.telephony.repeat_callers import (
    InboundCallerDecision,
    check_inbound_caller,
)

MAIN = 1  # owns the numbers and the routing group
REAL_ESTATE = 3  # linked workspace with the real estate agent

# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_parse_linked_workspaces_ignores_bad_entries_and_itself():
    value = {
        "workspaces": [
            {"organization_id": 3, "label": "Real estate"},
            {"organization_id": "4"},
            {"organization_id": 3, "label": "duplicate"},
            {"organization_id": MAIN, "label": "self"},
            {"organization_id": "abc"},
            {"organization_id": True},
            {"organization_id": -2},
            "junk",
        ]
    }
    links = parse_linked_workspaces(value, MAIN)
    assert [(link.organization_id, link.label) for link in links] == [
        (3, "Real estate"),
        (4, "Workspace 4"),
    ]


@pytest.mark.parametrize("value", [None, [], {}, {"workspaces": "3"}, "x"])
def test_parse_linked_workspaces_empty_for_missing_or_malformed(value):
    assert parse_linked_workspaces(value, MAIN) == []


async def test_unreadable_setting_links_nothing(monkeypatch):
    monkeypatch.setattr(
        linked_workspaces,
        "db_client",
        SimpleNamespace(get_configuration=AsyncMock(side_effect=RuntimeError("db"))),
    )
    assert await linked_workspaces.linked_organization_ids(MAIN) == []


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def _phone():
    return SimpleNamespace(
        id=501,
        organization_id=MAIN,
        inbound_workflow_id=15,
        inbound_routing_group_id=7,
    )


def _group(weights):
    return SimpleNamespace(
        id=7,
        organization_id=MAIN,
        name="Inbound mix",
        is_active=True,
        sticky_by_caller=True,
        targets=[SimpleNamespace(workflow_id=w, weight=p) for w, p in weights],
    )


def _fake_routing_db(monkeypatch, group, workflows_by_org):
    async def get_workflow(workflow_id, organization_id=None, user_id=None):
        if workflow_id in workflows_by_org.get(organization_id, ()):
            return SimpleNamespace(id=workflow_id, status="active")
        return None

    monkeypatch.setattr(
        inbound_routing,
        "db_client",
        SimpleNamespace(
            get_inbound_routing_group_with_targets=AsyncMock(return_value=group),
            get_workflow=AsyncMock(side_effect=get_workflow),
        ),
    )


async def test_split_to_linked_workspace_agent_names_that_workspace(monkeypatch):
    # 100% to the real estate agent (workflow 40, in the linked workspace).
    _fake_routing_db(monkeypatch, _group([(40, 100)]), {MAIN: {15}, REAL_ESTATE: {40}})
    workflow_id, meta = await resolve_inbound_workflow_id(
        _phone(), "+15557650000", linked_organization_ids=[REAL_ESTATE]
    )
    assert workflow_id == 40
    assert meta["source"] == "group"
    assert meta["organization_id"] == REAL_ESTATE
    assert meta["number_organization_id"] == MAIN


async def test_own_agent_pick_records_no_other_workspace(monkeypatch):
    _fake_routing_db(monkeypatch, _group([(15, 100)]), {MAIN: {15}, REAL_ESTATE: {40}})
    workflow_id, meta = await resolve_inbound_workflow_id(
        _phone(), "+15557650000", linked_organization_ids=[REAL_ESTATE]
    )
    assert workflow_id == 15
    assert "organization_id" not in meta


async def test_linked_agent_skipped_where_the_path_cannot_route_across(monkeypatch):
    # The webhook path passes no linked workspaces: the linked agent is skipped
    # and the call stays in the number's workspace.
    _fake_routing_db(monkeypatch, _group([(40, 100)]), {MAIN: {15}, REAL_ESTATE: {40}})
    workflow_id, meta = await resolve_inbound_workflow_id(_phone(), "+15557650000")
    assert workflow_id == 15
    assert meta["source"] == "fallback"
    assert meta["skipped_targets"][0]["workflow_id"] == 40


async def test_fifty_fifty_split_across_workspaces(monkeypatch):
    _fake_routing_db(
        monkeypatch, _group([(15, 50), (40, 50)]), {MAIN: {15}, REAL_ESTATE: {40}}
    )
    picks = []
    for i in range(400):
        workflow_id, meta = await resolve_inbound_workflow_id(
            _phone(), f"+1555{i:07d}", linked_organization_ids=[REAL_ESTATE]
        )
        picks.append((workflow_id, meta.get("organization_id", MAIN)))
    assert set(picks) == {(15, MAIN), (40, REAL_ESTATE)}
    share = sum(1 for wid, _ in picks if wid == 40) / len(picks)
    assert 0.4 < share < 0.6


# ---------------------------------------------------------------------------
# Routing group management
# ---------------------------------------------------------------------------


def _fake_group_db(monkeypatch, workflows_by_org, links):
    async def get_workflows_by_ids(ids, organization_id):
        names = workflows_by_org.get(organization_id, {})
        return [SimpleNamespace(id=i, name=names[i]) for i in ids if i in names]

    monkeypatch.setattr(
        inbound_routing_groups,
        "db_client",
        SimpleNamespace(
            get_workflows_by_ids=AsyncMock(side_effect=get_workflows_by_ids)
        ),
    )
    monkeypatch.setattr(
        linked_workspaces,
        "db_client",
        SimpleNamespace(
            get_configuration=AsyncMock(
                return_value=SimpleNamespace(value={"workspaces": links})
                if links
                else None
            )
        ),
    )


async def test_group_may_target_linked_workspace_agents(monkeypatch):
    _fake_group_db(
        monkeypatch,
        {MAIN: {15: "Final Expense"}, REAL_ESTATE: {40: "Home Seller"}},
        [{"organization_id": REAL_ESTATE, "label": "Real estate"}],
    )
    targets = [SimpleNamespace(workflow_id=15), SimpleNamespace(workflow_id=40)]
    await validate_target_workflows(targets, MAIN)
    assert await workflow_names([15, 40], MAIN) == {
        15: "Final Expense",
        40: "Home Seller (Real estate)",
    }


async def test_group_may_not_target_unlinked_workspace(monkeypatch):
    _fake_group_db(
        monkeypatch,
        {MAIN: {15: "Final Expense"}, REAL_ESTATE: {40: "Home Seller"}},
        [],
    )
    with pytest.raises(RoutingGroupValidationError):
        await validate_target_workflows([SimpleNamespace(workflow_id=40)], MAIN)


# ---------------------------------------------------------------------------
# One call per customer, across linked workspaces
# ---------------------------------------------------------------------------


def _fake_caller_db(monkeypatch, answered_in=(), live_in=(), number_of=()):
    monkeypatch.setattr(
        repeat_callers,
        "db_client",
        SimpleNamespace(
            is_organization_phone_number=AsyncMock(
                side_effect=lambda org, _n: org in number_of
            ),
            number_on_live_call=AsyncMock(
                side_effect=lambda org, _d, _s: org in live_in
            ),
            inbound_caller_has_answered_call=AsyncMock(
                side_effect=lambda org, _d: org in answered_in
            ),
        ),
    )


async def test_caller_answered_by_linked_agent_has_had_their_call(monkeypatch):
    _fake_caller_db(monkeypatch, answered_in={REAL_ESTATE})
    decision = await check_inbound_caller(
        MAIN, "+15557650000", also_organization_ids=[REAL_ESTATE]
    )
    assert decision.admit is False


async def test_live_call_in_linked_workspace_is_a_handoff(monkeypatch):
    _fake_caller_db(monkeypatch, answered_in={MAIN}, live_in={REAL_ESTATE})
    decision = await check_inbound_caller(
        MAIN, "+15557650000", also_organization_ids=[REAL_ESTATE]
    )
    assert decision.admit is True and decision.ai_transfer is True


async def test_without_links_only_the_number_workspace_counts(monkeypatch):
    _fake_caller_db(monkeypatch, answered_in={REAL_ESTATE})
    decision = await check_inbound_caller(MAIN, "+15557650000")
    assert decision.admit is True


# ---------------------------------------------------------------------------
# ARI: a shared Asterisk app
# ---------------------------------------------------------------------------


class _Conn(ARIConnection):
    """Org 3's connection on the shared app, with network/Redis stubbed."""

    def __init__(self, organization_id=REAL_ESTATE, config_id=7):
        super().__init__(
            organization_id=organization_id,
            telephony_configuration_id=config_id,
            ari_endpoint="http://asterisk.test:8088",
            app_name="dograh",
            app_password="secret",
            ws_client_name="dograh-ws",
        )
        self.app_configs = {
            MAIN: [(1, "hopwhistle"), (3, "Vonage"), (4, "Mash Telecom")],
            REAL_ESTATE: [(6, "Vonage"), (7, "hopwhistle"), (8, "Mash Telecom")],
        }
        self.deleted_channels = []
        self.started = []

    async def _delete_channel(self, channel_id):
        self.deleted_channels.append(channel_id)

    async def _capture_external_pbx_call(self, channel_id, channel_name):
        return None

    async def _answer_channel(self, channel_id):
        pass


class _InboundConn(_Conn):
    async def _handle_stasis_start(
        self, channel_id, state, run_id, workflow_id, organization_id=None
    ):
        self.started.append((channel_id, run_id, workflow_id, organization_id))


_EVENT = {
    "channel": {
        "name": "PJSIP/hopwhistle-0001",
        "caller": {"number": "+15557650000"},
        "dialplan": {"exten": "+17207940675"},
    }
}


def _patch_inbound(monkeypatch, routing, links=(REAL_ESTATE,)):
    phone_row = SimpleNamespace(
        id=501,
        organization_id=MAIN,
        telephony_configuration_id=1,  # MAIN's "hopwhistle" config
        address="+17207940675",
        inbound_workflow_id=15,
        inbound_routing_group_id=7,
    )

    async def find_number(org_id, called, provider):
        return phone_row if org_id == MAIN else None

    workflows = {15: MAIN, 40: REAL_ESTATE}

    async def get_workflow(workflow_id, organization_id=None, **_):
        if workflows.get(workflow_id) == organization_id:
            return SimpleNamespace(id=workflow_id, user_id=100 + organization_id)
        return None

    fake_db = SimpleNamespace(
        find_active_phone_number_for_inbound=AsyncMock(side_effect=find_number),
        get_workflow=AsyncMock(side_effect=get_workflow),
        create_workflow_run=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    resolver = AsyncMock(return_value=routing)
    caller_check = AsyncMock(return_value=InboundCallerDecision(admit=True))
    concurrency = SimpleNamespace(
        acquire_org_slot=AsyncMock(return_value=object()),
        bind_workflow_run=AsyncMock(),
    )
    monkeypatch.setattr(ari_manager, "db_client", fake_db)
    monkeypatch.setattr(ari_manager, "resolve_inbound_workflow_id", resolver)
    monkeypatch.setattr(ari_manager, "check_inbound_caller", caller_check)
    monkeypatch.setattr(
        ari_manager, "linked_organization_ids", AsyncMock(return_value=list(links))
    )
    monkeypatch.setattr(ari_manager, "ari_inbound_admitted", lambda *a, **k: True)
    monkeypatch.setattr(
        ari_manager, "screening_required", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(ari_manager, "call_concurrency", concurrency)
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
    return SimpleNamespace(
        db=fake_db,
        resolver=resolver,
        caller_check=caller_check,
        concurrency=concurrency,
        phone_row=phone_row,
    )


async def test_main_number_answered_on_another_orgs_connection(monkeypatch):
    # The call to MAIN's number lands on org 3's connection (shared app). It
    # must be answered as MAIN's call, as it was before org 3 existed.
    fx = _patch_inbound(monkeypatch, (15, None), links=())
    conn = _InboundConn()
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)

    assert conn.deleted_channels == []
    kwargs = fx.db.create_workflow_run.await_args.kwargs
    assert kwargs["organization_id"] == MAIN
    assert kwargs["user_id"] == 100 + MAIN
    assert kwargs["initial_context"]["telephony_configuration_id"] == 1
    fx.concurrency.acquire_org_slot.assert_awaited_once()
    assert fx.concurrency.acquire_org_slot.await_args.args[0] == MAIN
    assert conn.started == [("chan-1", "900", "15", MAIN)]


async def test_split_to_linked_agent_creates_the_run_in_that_workspace(monkeypatch):
    meta = {"group_id": 7, "workflow_id": 40, "organization_id": REAL_ESTATE}
    fx = _patch_inbound(monkeypatch, (40, meta))
    conn = _InboundConn(organization_id=MAIN, config_id=1)
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)

    fx.resolver.assert_awaited_once_with(
        fx.phone_row, "+15557650000", linked_organization_ids=[REAL_ESTATE]
    )
    fx.caller_check.assert_awaited_once_with(
        MAIN, "+15557650000", also_organization_ids=[REAL_ESTATE]
    )
    kwargs = fx.db.create_workflow_run.await_args.kwargs
    assert kwargs["organization_id"] == REAL_ESTATE
    assert kwargs["workflow_id"] == 40
    assert kwargs["user_id"] == 100 + REAL_ESTATE
    # Org 3's config with the same name as the number's ("hopwhistle").
    assert kwargs["initial_context"]["telephony_configuration_id"] == 7
    assert kwargs["initial_context"]["inbound_routing"] == meta
    assert conn.started == [("chan-1", "900", "40", REAL_ESTATE)]


async def test_linked_workspace_without_a_config_on_this_app_is_not_offered(
    monkeypatch,
):
    fx = _patch_inbound(monkeypatch, (15, None), links=(REAL_ESTATE, 9))
    conn = _InboundConn(organization_id=MAIN, config_id=1)
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)
    assert fx.resolver.await_args.kwargs["linked_organization_ids"] == [REAL_ESTATE]


async def test_routed_row_beats_a_caller_id_only_copy_in_this_org(monkeypatch):
    # Org 3 holds a copy of MAIN's number for outbound caller ID, with no
    # inbound route. A call handed to org 3's connection must still go to
    # MAIN, which routes the number, not to org 3's unrouted copy.
    fx = _patch_inbound(monkeypatch, (15, None), links=())
    copy = SimpleNamespace(
        id=601,
        organization_id=REAL_ESTATE,
        telephony_configuration_id=8,
        address="+17207940675",
        inbound_workflow_id=None,
        inbound_routing_group_id=None,
    )

    async def find_number(org_id, called, provider):
        return {MAIN: fx.phone_row, REAL_ESTATE: copy}.get(org_id)

    fx.db.find_active_phone_number_for_inbound.side_effect = find_number
    conn = _InboundConn()  # org 3's connection
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)

    assert conn.deleted_channels == []
    assert fx.db.create_workflow_run.await_args.kwargs["organization_id"] == MAIN
    assert conn.started == [("chan-1", "900", "15", MAIN)]


async def test_unrouted_copy_is_still_found_when_no_org_routes_it(monkeypatch):
    fx = _patch_inbound(monkeypatch, (15, None), links=())
    fx.phone_row.inbound_workflow_id = None
    fx.phone_row.inbound_routing_group_id = None
    conn = _InboundConn()
    assert await conn._find_inbound_number("+17207940675") is fx.phone_row


async def test_unknown_number_is_hung_up(monkeypatch):
    fx = _patch_inbound(monkeypatch, (15, None))
    fx.db.find_active_phone_number_for_inbound.side_effect = None
    fx.db.find_active_phone_number_for_inbound.return_value = None
    conn = _InboundConn()
    await conn._handle_inbound_stasis_start("chan-1", "Ring", _EVENT)
    assert conn.deleted_channels == ["chan-1"]
    fx.db.create_workflow_run.assert_not_awaited()


class _MediaConn(_Conn):
    """Stubs Redis and ARI REST so _handle_stasis_start runs end to end."""

    def __init__(self):
        super().__init__()
        self.ari_calls = []

    async def _set_channel_run(self, *a):
        pass

    async def _mark_ext_channel(self, *a):
        pass

    async def _set_pending_bridge(self, *a):
        pass

    async def _pop_pending_bridge(self, *a):
        return None

    async def _ari_request(self, method, path, **kwargs):
        self.ari_calls.append((method, path, kwargs))
        return {"id": kwargs["params"]["channelId"]}


async def test_outbound_answer_uses_the_runs_org(monkeypatch):
    # MAIN's outbound call answered, event handed to org 3's connection: the
    # media websocket must be told MAIN, or it can't find the run (the
    # "Workflow run ... not found for org 3" failure).
    run = SimpleNamespace(workflow=SimpleNamespace(organization_id=MAIN))
    monkeypatch.setattr(
        ari_manager,
        "db_client",
        SimpleNamespace(
            get_workflow_run_by_id=AsyncMock(return_value=run),
            update_workflow_run=AsyncMock(),
        ),
    )
    conn = _MediaConn()
    await conn._handle_stasis_start("chan-9", "Up", "421435", "77")

    method, path, kwargs = conn.ari_calls[0]
    assert path == "/channels/externalMedia"
    assert (
        kwargs["params"]["transport_data"]
        == f"v(workflow_id=77,organization_id={MAIN},workflow_run_id=421435)"
    )


async def test_outbound_answer_falls_back_to_this_org_if_run_unreadable(monkeypatch):
    monkeypatch.setattr(
        ari_manager,
        "db_client",
        SimpleNamespace(
            get_workflow_run_by_id=AsyncMock(side_effect=RuntimeError("db")),
            update_workflow_run=AsyncMock(),
        ),
    )
    conn = _MediaConn()
    await conn._handle_stasis_start("chan-9", "Up", "5", "77")
    assert (
        f"organization_id={REAL_ESTATE},"
        in conn.ari_calls[0][2]["params"]["transport_data"]
    )


async def test_manager_tells_connections_who_shares_their_app(monkeypatch):
    def cfg(org, cid, name, app="dograh"):
        return {
            "organization_id": org,
            "telephony_configuration_id": cid,
            "name": name,
            "ari_endpoint": "http://asterisk.test:8088/",
            "app_name": app,
            "app_password": "secret",
            "ws_client_name": "dograh-ws",
            "external_pbx": None,
        }

    configs = [
        cfg(MAIN, 1, "hopwhistle"),
        cfg(MAIN, 4, "Mash Telecom"),
        cfg(REAL_ESTATE, 7, "hopwhistle"),
        cfg(5, 20, "other", app="dograh-other"),
    ]
    monkeypatch.setattr(ARIConnection, "start", AsyncMock())
    manager = ARIManager()
    monkeypatch.setattr(manager, "_load_ari_configs", AsyncMock(return_value=configs))
    await manager._refresh_connections()

    shared = manager._connections["config:7"].app_configs
    assert shared == {
        MAIN: [(1, "hopwhistle"), (4, "Mash Telecom")],
        REAL_ESTATE: [(7, "hopwhistle")],
    }
    assert manager._connections["config:20"].app_configs == {5: [(20, "other")]}
