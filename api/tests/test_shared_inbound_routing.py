"""Shared phone numbers and cross-workspace inbound routing, end to end
against the test database (Postgres + Redis).

Three separate workspaces, as in production:

* Final Expense (the insurance workspace): owns the DIDs and the routing
  group, and has linked the other two (operator setting).
* Real Estate: its own agent, linked.
* Holiday Voices: its own "Shopping & Phone Orders" agent, linked.

All three have an ARI configuration on the same Asterisk app (the voice
app's clone_workspace.sql copies it), which is how production's
FreeSWITCH -> Asterisk -> Stasis path reaches them.

Only Asterisk's network calls (answer, hang up, external media) and the
hosted-quota check are stubbed; routing, linking, run creation, concurrency
(Redis) and the stats/audit queries are the real code on real rows.
"""

import random
from collections import Counter
from types import SimpleNamespace

import pytest

from api.enums import CallType, WorkflowStatus
from api.services.call_concurrency import CallConcurrencyLimitError
from api.services.telephony import ari_manager
from api.services.telephony.ari_manager import ARIConnection
from api.services.telephony.inbound_routing import resolve_inbound_workflow_id
from api.services.telephony.inbound_routing_groups import CALL_REFUSED_ACTION
from api.services.telephony.linked_workspaces import LINKED_WORKSPACES_KEY

BASE = "/api/v1/inbound-routing-groups"
PHONE_NUMBERS = "/api/v1/organizations/telephony-configs/{}/phone-numbers"
ARI_CREDENTIALS = {
    "ari_endpoint": "http://asterisk.test:8088",
    "app_name": "dograh",
    "app_password": "secret",
    "ws_client_name": "dograh-ws",
}
SHARED_DID = "+17205550101"
SECOND_DID = "+17205550102"


async def _workspace(db_session, slug):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"{slug}_user")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"{slug}_org", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    user = await db_session.get_user_by_id(user.id)
    config = await db_session.create_telephony_configuration(
        org.id, "hopwhistle", "ari", dict(ARI_CREDENTIALS)
    )
    return SimpleNamespace(user=user, org_id=org.id, config=config)


async def _agent(db_session, ws, name):
    return await db_session.create_workflow(
        name, {"nodes": [], "edges": []}, ws.user.id, ws.org_id
    )


async def _link(db_session, owner, linked, self_label="Final Expense"):
    await db_session.upsert_configuration(
        owner.org_id,
        LINKED_WORKSPACES_KEY,
        {
            "self_label": self_label,
            "workspaces": [
                {"organization_id": ws.org_id, "label": label} for ws, label in linked
            ],
        },
    )


@pytest.fixture
async def platform(db_session, test_client_factory):
    """The three workspaces, their agents, two shared DIDs in one group at
    50 / 25 / 25, linked by the operator."""
    tag = random.randint(10_000, 99_999)
    fe = await _workspace(db_session, f"fe{tag}")
    re_ = await _workspace(db_session, f"re{tag}")
    hv = await _workspace(db_session, f"hv{tag}")
    alex = await _agent(db_session, fe, "Alex")
    fe_own = await _agent(db_session, fe, "Final Expense Inbound")
    re_agent = await _agent(db_session, re_, "Riley")
    hv_agent = await _agent(db_session, hv, "Shopping & Phone Orders")
    hv_other = await _agent(db_session, hv, "Customer Support")

    numbers = [
        await db_session.create_phone_number(
            fe.org_id, fe.config.id, did, inbound_workflow_id=fe_own.id
        )
        for did in (SHARED_DID, SECOND_DID)
    ]
    await _link(db_session, fe, [(re_, "Real Estate"), (hv, "Holiday Voices")])

    async with test_client_factory(fe.user) as client:
        created = await client.post(
            BASE,
            json={
                "name": "Main AI Inbound Distribution",
                "sticky_by_caller": False,
                "targets": [
                    {"workflow_id": alex.id, "weight": 50},
                    {"workflow_id": re_agent.id, "weight": 25},
                    {"workflow_id": hv_agent.id, "weight": 25},
                ],
            },
        )
        assert created.status_code == 200, created.text
        group = created.json()
        attached = await client.post(
            f"{BASE}/{group['id']}/numbers",
            json={"phone_number_ids": [n.id for n in numbers]},
        )
        assert attached.json()["affected"] == 2

    return SimpleNamespace(
        fe=fe,
        re=re_,
        hv=hv,
        alex=alex,
        fe_own=fe_own,
        re_agent=re_agent,
        hv_agent=hv_agent,
        hv_other=hv_other,
        numbers=numbers,
        group=group,
    )


async def _row(db_session, p, index=0):
    return await db_session.get_phone_number(p.numbers[index].id)


def _linked(p):
    return [p.re.org_id, p.hv.org_id]


# ---------------------------------------------------------------------------
# 1, 2, 3, 6: one group, two DIDs, 50 / 25 / 25, independent weighted picks
# ---------------------------------------------------------------------------


async def test_editor_shows_agents_with_their_workspace(platform, test_client_factory):
    p = platform
    targets = {t["workflow_id"]: t for t in p.group["targets"]}
    assert targets[p.alex.id]["agent_name"] == "Alex"
    assert targets[p.alex.id]["organization_label"] == "Final Expense"
    assert targets[p.alex.id]["is_linked_workspace"] is False
    assert targets[p.re_agent.id]["organization_label"] == "Real Estate"
    assert targets[p.re_agent.id]["organization_id"] == p.re.org_id
    assert targets[p.hv_agent.id]["agent_name"] == "Shopping & Phone Orders"
    assert targets[p.hv_agent.id]["organization_label"] == "Holiday Voices"
    assert targets[p.hv_agent.id]["organization_id"] == p.hv.org_id
    assert targets[p.hv_agent.id]["is_linked_workspace"] is True
    assert sum(t["weight"] for t in p.group["targets"]) == 100

    async with test_client_factory(p.fe.user) as client:
        picker = (await client.get(f"{BASE}/linked-agents")).json()
    assert picker["self_label"] == "Final Expense"
    by_label = {w["label"]: w for w in picker["workspaces"]}
    assert {a["name"] for a in by_label["Holiday Voices"]["agents"]} == {
        "Shopping & Phone Orders",
        "Customer Support",
    }
    assert [a["name"] for a in by_label["Real Estate"]["agents"]] == ["Riley"]


async def test_non_sticky_split_follows_the_weights_on_every_did(
    platform, db_session, monkeypatch
):
    p = platform
    random.seed(20261123)
    counts = Counter()
    orgs = Counter()
    calls = 4000
    for i in range(calls):
        row = await _row(db_session, p, i % 2)  # both DIDs, same group
        workflow_id, meta = await resolve_inbound_workflow_id(
            row, f"+1303555{i:04d}", linked_organization_ids=_linked(p)
        )
        counts[workflow_id] += 1
        orgs[meta.get("organization_id", row.organization_id)] += 1
        assert meta["mode"] == "random" and meta["source"] == "group"
        assert meta["group_name"] == "Main AI Inbound Distribution"
    share = {wid: n / calls for wid, n in counts.items()}
    # 4000 independent draws: 3 standard deviations is under 2.5 points.
    assert abs(share[p.alex.id] - 0.50) < 0.03
    assert abs(share[p.re_agent.id] - 0.25) < 0.03
    assert abs(share[p.hv_agent.id] - 0.25) < 0.03
    assert set(orgs) == {p.fe.org_id, p.re.org_id, p.hv.org_id}
    assert p.fe_own.id not in counts  # the number's own agent never needed


# ---------------------------------------------------------------------------
# 4, 5: edits apply to the next call; sticky keeps a caller on one agent
# ---------------------------------------------------------------------------


async def test_editing_percentages_applies_to_the_next_call(
    platform, db_session, test_client_factory
):
    p = platform
    async with test_client_factory(p.fe.user) as client:
        changed = await client.patch(
            f"{BASE}/{p.group['id']}",
            json={
                "expected_updated_at": p.group["updated_at"],
                "targets": [
                    {"workflow_id": p.alex.id, "weight": 50},
                    {"workflow_id": p.re_agent.id, "weight": 50},
                    # 0% pauses Holiday Voices without removing it.
                    {"workflow_id": p.hv_agent.id, "weight": 0},
                ],
            },
        )
        assert changed.status_code == 200, changed.text
        assert {t["workflow_id"]: t["weight"] for t in changed.json()["targets"]}[
            p.hv_agent.id
        ] == 0

        # A second editor still holding the old version can't overwrite it.
        stale = await client.patch(
            f"{BASE}/{p.group['id']}",
            json={
                "expected_updated_at": p.group["updated_at"],
                "targets": [{"workflow_id": p.alex.id, "weight": 100}],
            },
        )
        assert stale.status_code == 409
        assert "Someone else saved" in stale.json()["detail"]["message"]

        audit = (await client.get(f"{BASE}/audit?group_id={p.group['id']}")).json()
    assert audit["entries"][0]["action"] == "group_updated"
    assert audit["entries"][0]["details"]["weights"]["to"][str(p.hv_agent.id)] == 0

    random.seed(7)
    picked = Counter()
    for i in range(400):
        workflow_id, _ = await resolve_inbound_workflow_id(
            await _row(db_session, p), f"+1303666{i:04d}", _linked(p)
        )
        picked[workflow_id] += 1
    assert p.hv_agent.id not in picked
    assert set(picked) == {p.alex.id, p.re_agent.id}


async def test_sticky_caller_returns_to_the_same_agent(
    platform, db_session, test_client_factory
):
    p = platform
    async with test_client_factory(p.fe.user) as client:
        r = await client.patch(
            f"{BASE}/{p.group['id']}", json={"sticky_by_caller": True}
        )
        assert r.status_code == 200
    row = await _row(db_session, p)
    first, meta = await resolve_inbound_workflow_id(row, "+13035550199", _linked(p))
    assert meta["mode"] == "sticky"
    for _ in range(25):
        again, _ = await resolve_inbound_workflow_id(row, "(303) 555-0199", _linked(p))
        assert again == first
    # Different callers still spread across all three businesses.
    spread = {
        (await resolve_inbound_workflow_id(row, f"+1720444{i:04d}", _linked(p)))[0]
        for i in range(300)
    }
    assert spread == {p.alex.id, p.re_agent.id, p.hv_agent.id}


# ---------------------------------------------------------------------------
# 7, 8, 12: the call runs in Holiday Voices over the production transport
# ---------------------------------------------------------------------------


class _Asterisk(ARIConnection):
    """Final Expense's connection on the shared Stasis app; only Asterisk's
    HTTP calls are stubbed."""

    def __init__(self, p, app_orgs):
        super().__init__(
            organization_id=p.fe.org_id,
            telephony_configuration_id=p.fe.config.id,
            ari_endpoint=ARI_CREDENTIALS["ari_endpoint"],
            app_name="dograh",
            app_password="secret",
            ws_client_name="dograh-ws",
        )
        self.app_configs = {
            ws.org_id: [(ws.config.id, "hopwhistle")] for ws in app_orgs
        }
        self.deleted, self.answered, self.media = [], [], []

    async def _delete_channel(self, channel_id):
        self.deleted.append(channel_id)

    async def _answer_channel(self, channel_id):
        self.answered.append(channel_id)

    async def _capture_external_pbx_call(self, channel_id, channel_name):
        return None

    async def _handle_stasis_start(
        self, channel_id, state, run_id, workflow_id, organization_id=None
    ):
        self.media.append((channel_id, int(run_id), int(workflow_id), organization_id))


def _stasis(caller, did=SHARED_DID, channel="PJSIP/hopwhistle-callback-00000001"):
    # Production: FracTEL -> FreeSWITCH (Hopwhistle screens the ANI) ->
    # Asterisk endpoint hopwhistle-callback -> Stasis.
    return {
        "channel": {
            "name": channel,
            "caller": {"number": caller},
            "dialplan": {"exten": did},
        }
    }


@pytest.fixture
def quota_ok(monkeypatch):
    async def ok(**_):
        return SimpleNamespace(has_quota=True)

    monkeypatch.setattr(ari_manager, "authorize_workflow_run_start", ok)


async def _only_holiday_voices(client, p, sticky=False):
    r = await client.patch(
        f"{BASE}/{p.group['id']}",
        json={
            "sticky_by_caller": sticky,
            "targets": [
                {"workflow_id": p.alex.id, "weight": 0},
                {"workflow_id": p.re_agent.id, "weight": 0},
                {"workflow_id": p.hv_agent.id, "weight": 100},
            ],
        },
    )
    assert r.status_code == 200, r.text


async def test_holiday_voices_call_runs_and_is_stored_in_holiday_voices(
    platform, db_session, test_client_factory, quota_ok
):
    p = platform
    async with test_client_factory(p.fe.user) as client:
        await _only_holiday_voices(client, p)

    conn = _Asterisk(p, [p.fe, p.re, p.hv])
    await conn._handle_inbound_stasis_start(
        "chan-hv-1", "Ring", _stasis("+13035550123")
    )

    assert conn.deleted == [] and conn.answered == ["chan-hv-1"]
    ((_, run_id, workflow_id, media_org),) = conn.media
    assert workflow_id == p.hv_agent.id
    assert media_org == p.hv.org_id  # media websocket opened for Holiday Voices

    run = await db_session.get_workflow_run_by_id(run_id)
    assert run.workflow.organization_id == p.hv.org_id
    assert run.call_type == CallType.INBOUND.value or run.call_type == CallType.INBOUND
    ctx = run.initial_context
    # Holiday Voices' own ARI config handles hang-up and transfers.
    assert ctx["telephony_configuration_id"] == p.hv.config.id
    assert ctx["called_number"] == SHARED_DID
    routing = ctx["inbound_routing"]
    assert routing["group_name"] == "Main AI Inbound Distribution"
    assert routing["organization_id"] == p.hv.org_id
    assert routing["number_organization_id"] == p.fe.org_id

    # Transcript/recording live on the run, which Final Expense can't read.
    assert await db_session.get_workflow_run(run_id, organization_id=p.hv.org_id)
    assert (
        await db_session.get_workflow_run(run_id, organization_id=p.fe.org_id) is None
    )
    async with test_client_factory(p.fe.user) as client:
        foreign = await client.get(f"/api/v1/workflow/{p.hv_agent.id}/runs/{run_id}")
        assert foreign.status_code in (403, 404)

    # The owner still sees it in the group's stats, with its workspace.
    async with test_client_factory(p.fe.user) as client:
        stats = (await client.get(f"{BASE}/{p.group['id']}/stats")).json()
    hv_row = {r["workflow_id"]: r for r in stats["targets"]}[p.hv_agent.id]
    assert hv_row["calls"] == 1 and hv_row["organization_label"] == "Holiday Voices"


async def test_existing_final_expense_route_is_unchanged(
    platform, db_session, quota_ok
):
    """A Final Expense number outside the shared group answers exactly as
    before: its own agent, its own workspace, no routing decision."""
    p = platform
    other = await db_session.create_phone_number(
        p.fe.org_id, p.fe.config.id, "+17205550199", inbound_workflow_id=p.fe_own.id
    )
    conn = _Asterisk(p, [p.fe, p.re, p.hv])
    await conn._handle_inbound_stasis_start(
        "chan-fe", "Ring", _stasis("+13035550124", did=other.address)
    )
    ((_, run_id, workflow_id, media_org),) = conn.media
    assert (workflow_id, media_org) == (p.fe_own.id, p.fe.org_id)
    run = await db_session.get_workflow_run_by_id(run_id)
    assert run.workflow.organization_id == p.fe.org_id
    assert "inbound_routing" not in run.initial_context


# ---------------------------------------------------------------------------
# 9, 10, 11, 15: unavailable targets fall back explicitly and are recorded
# ---------------------------------------------------------------------------


async def test_archived_holiday_voices_agent_falls_back_with_reason(
    platform, db_session, test_client_factory
):
    p = platform
    async with test_client_factory(p.fe.user) as client:
        await _only_holiday_voices(client, p)
    await db_session.update_workflow_status(
        p.hv_agent.id, WorkflowStatus.ARCHIVED.value, p.hv.org_id
    )
    workflow_id, meta = await resolve_inbound_workflow_id(
        await _row(db_session, p), "+13035550125", _linked(p)
    )
    # 0% targets are never tried; the number's own agent answers.
    assert workflow_id == p.fe_own.id
    assert meta["source"] == "fallback"
    assert meta["fallback_reason"] == "no_usable_target"
    assert meta["skipped_targets"] == [
        {
            "workflow_id": p.hv_agent.id,
            "organization_id": p.hv.org_id,
            "reason": "status is archived",
        }
    ]
    assert meta["changed_workspace"] is True


async def test_unlinked_workspace_is_skipped_and_the_change_recorded(
    platform, db_session
):
    p = platform
    # Operator unlinks Holiday Voices (only Real Estate stays linked).
    await _link(db_session, p.fe, [(p.re, "Real Estate")])
    random.seed(3)
    seen = Counter()
    for i in range(200):
        workflow_id, meta = await resolve_inbound_workflow_id(
            await _row(db_session, p), f"+1303777{i:04d}", [p.re.org_id]
        )
        seen[workflow_id] += 1
        if meta["picked_workflow_id"] == p.hv_agent.id:
            assert meta["skipped_targets"][0]["workflow_id"] == p.hv_agent.id
            assert "not linked" in meta["skipped_targets"][0]["reason"]
            assert meta["skipped_targets"][0]["organization_id"] == p.hv.org_id
            # It went to another business, and the run says so.
            assert meta["changed_workspace"] is True
    assert p.hv_agent.id not in seen


async def test_holiday_voices_without_an_ari_config_on_the_app(
    platform, db_session, test_client_factory, quota_ok
):
    """ARI mismatch: Holiday Voices is linked but has no ARI config on this
    Asterisk app, so it can't take the call here. The reason names it."""
    p = platform
    async with test_client_factory(p.fe.user) as client:
        await _only_holiday_voices(client, p)
    conn = _Asterisk(p, [p.fe, p.re])  # Holiday Voices isn't on this app
    await conn._handle_inbound_stasis_start("chan-x", "Ring", _stasis("+13035550126"))
    ((_, run_id, workflow_id, _),) = conn.media
    assert workflow_id == p.fe_own.id  # the number's own agent, not dropped
    routing = (await db_session.get_workflow_run_by_id(run_id)).initial_context[
        "inbound_routing"
    ]
    assert routing["skipped_targets"] == [
        {
            "workflow_id": p.hv_agent.id,
            "organization_id": p.hv.org_id,
            "reason": (
                f"linked workspace {p.hv.org_id} has no ARI configuration "
                f"on this Asterisk app"
            ),
        }
    ]
    async with test_client_factory(p.fe.user) as client:
        stats = (await client.get(f"{BASE}/{p.group['id']}/stats")).json()
    assert stats["fallback_calls"] == 1 and stats["cross_workspace_fallbacks"] == 1
    hv_row = {r["workflow_id"]: r for r in stats["targets"]}[p.hv_agent.id]
    assert hv_row["picked"] == 1 and hv_row["calls"] == 0
    assert hv_row["unavailable"] == 1


async def test_holiday_voices_at_its_call_limit_is_refused_and_recorded(
    platform, db_session, test_client_factory, quota_ok, monkeypatch
):
    p = platform
    async with test_client_factory(p.fe.user) as client:
        await _only_holiday_voices(client, p)

    real_acquire = ari_manager.call_concurrency.acquire_org_slot

    async def acquire(org_id, **kw):
        if org_id == p.hv.org_id:
            raise CallConcurrencyLimitError(
                organization_id=org_id,
                source=kw.get("source", ""),
                wait_time=0,
                max_concurrent=1,
            )
        return await real_acquire(org_id, **kw)

    monkeypatch.setattr(ari_manager.call_concurrency, "acquire_org_slot", acquire)
    conn = _Asterisk(p, [p.fe, p.re, p.hv])
    await conn._handle_inbound_stasis_start(
        "chan-full", "Ring", _stasis("+13035550127")
    )
    assert conn.deleted == ["chan-full"] and conn.media == []

    async with test_client_factory(p.fe.user) as client:
        stats = (await client.get(f"{BASE}/{p.group['id']}/stats")).json()
        audit = (await client.get(f"{BASE}/audit?group_id={p.group['id']}")).json()
    assert stats["refused_calls"] == 1 and stats["eligible_calls"] == 1
    assert stats["refusals"][0]["reason"] == "workspace at its concurrent call limit"
    assert stats["refusals"][0]["organization_id"] == p.hv.org_id
    refused = [e for e in audit["entries"] if e["action"] == CALL_REFUSED_ACTION]
    assert refused[0]["details"]["called_number"] == SHARED_DID
    assert "caller" not in str(refused[0]["details"]).lower().replace("called", "")


async def test_stats_explain_targets_actuals_and_small_samples(
    platform, db_session, test_client_factory
):
    p = platform
    random.seed(11)
    for i in range(40):
        row = await _row(db_session, p, i % 2)
        workflow_id, meta = await resolve_inbound_workflow_id(
            row, f"+1303888{i:04d}", _linked(p)
        )
        org = meta.get("organization_id", p.fe.org_id)
        owner = {p.fe.org_id: p.fe, p.re.org_id: p.re, p.hv.org_id: p.hv}[org]
        await db_session.create_workflow_run(
            f"call-{i}",
            workflow_id,
            "ari",
            user_id=owner.user.id,
            call_type=CallType.INBOUND,
            initial_context={"called_number": row.address, "inbound_routing": meta},
            organization_id=org,
        )
    async with test_client_factory(p.fe.user) as client:
        stats = (await client.get(f"{BASE}/{p.group['id']}/stats")).json()
    assert stats["total_calls"] == stats["eligible_calls"] == 40
    rows = {r["workflow_id"]: r for r in stats["targets"]}
    assert {r["target_percent"] for r in rows.values()} == {50, 25}
    assert sum(r["calls"] for r in rows.values()) == 40
    assert rows[p.hv_agent.id]["organization_label"] == "Holiday Voices"
    assert rows[p.alex.id]["organization_label"] == "Final Expense"
    assert stats["fallback_calls"] == 0
    assert "does not mean exactly 25 of every 100" in stats["sample_note"]


# ---------------------------------------------------------------------------
# Shared-number inventory, both sides
# ---------------------------------------------------------------------------


async def test_both_workspaces_see_the_same_shared_numbers(
    platform, test_client_factory
):
    p = platform
    async with test_client_factory(p.fe.user) as client:
        owner_view = (await client.get(f"{BASE}/shared-numbers")).json()
    assert owner_view["organization_label"] == "Final Expense"
    assert owner_view["shared_with_me"] == []
    owned = {n["address"]: n for n in owner_view["owned"]}
    assert set(owned) == {SHARED_DID, SECOND_DID}
    shared = owned[SHARED_DID]
    assert shared["routing_group_name"] == "Main AI Inbound Distribution"
    assert {(s["label"], s["share_percent"]) for s in shared["shared_with"]} == {
        ("Real Estate", 25.0),
        ("Holiday Voices", 25.0),
    }

    async with test_client_factory(p.hv.user) as client:
        hv_view = (await client.get(f"{BASE}/shared-numbers")).json()
    assert hv_view["owned"] == []
    mine = {n["address"]: n for n in hv_view["shared_with_me"]}
    assert set(mine) == {SHARED_DID, SECOND_DID}  # same real numbers, no copies
    n = mine[SHARED_DID]
    assert n["owner_label"] == "Final Expense"
    assert n["owner_organization_id"] == p.fe.org_id
    assert n["routing_group_name"] == "Main AI Inbound Distribution"
    assert (n["my_share_percent"], n["other_share_percent"]) == (25.0, 75.0)
    assert [a["name"] for a in n["my_agents"]] == ["Shopping & Phone Orders"]
    # Nothing of Final Expense's or Real Estate's agents, nor carrier settings.
    text = str(hv_view)
    assert "Alex" not in text and "Riley" not in text
    assert "secret" not in text and "app_password" not in text


async def test_unlinking_hides_the_numbers_immediately(
    platform, db_session, test_client_factory
):
    p = platform
    await _link(db_session, p.fe, [(p.re, "Real Estate")])
    async with test_client_factory(p.hv.user) as client:
        assert (await client.get(f"{BASE}/shared-numbers")).json()[
            "shared_with_me"
        ] == []


# ---------------------------------------------------------------------------
# 13: Holiday Voices can't change, read or take over Final Expense's routing
# ---------------------------------------------------------------------------


async def test_holiday_voices_cannot_touch_the_owners_routing(
    platform, db_session, test_client_factory
):
    p = platform
    gid = p.group["id"]
    async with test_client_factory(p.hv.user) as client:
        assert (await client.get(BASE)).json()["groups"] == []
        assert (await client.get(f"{BASE}/{gid}")).status_code == 404
        assert (await client.get(f"{BASE}/{gid}/stats")).status_code == 404
        assert (await client.get(f"{BASE}/{gid}/numbers")).status_code == 404
        hijack = await client.patch(
            f"{BASE}/{gid}",
            json={"targets": [{"workflow_id": p.hv_agent.id, "weight": 100}]},
        )
        assert hijack.status_code == 404
        assert (await client.delete(f"{BASE}/{gid}")).status_code == 404
        steal = await client.post(
            f"{BASE}/{gid}/numbers", json={"phone_number_ids": [p.numbers[0].id]}
        )
        assert steal.status_code == 404
        assert (await client.get(f"{BASE}/audit?group_id={gid}")).json()[
            "entries"
        ] == []

        # Holiday Voices has linked nobody: Final Expense's agents aren't
        # targets it may pick, and it can't add arbitrary workspaces.
        own = await client.post(
            BASE,
            json={
                "name": "Try",
                "targets": [{"workflow_id": p.alex.id, "weight": 100}],
            },
        )
        assert own.status_code == 422
        assert (await client.get(f"{BASE}/linked-agents")).json()["workspaces"] == []

        # Putting its own agent on the shared DID would compete with the
        # owner's routing: refused.
        taken = await client.post(
            PHONE_NUMBERS.format(p.hv.config.id),
            json={"address": SHARED_DID, "inbound_workflow_id": p.hv_agent.id},
        )
        assert taken.status_code == 409, taken.text
        assert "routed by Final Expense" in taken.json()["detail"]

    # Unchanged.
    row = await _row(db_session, p)
    assert row.inbound_routing_group_id == gid


async def test_owner_row_wins_if_a_linked_copy_is_routed_anyway(
    platform, db_session, test_client_factory, quota_ok
):
    """A copy of the DID routed in Holiday Voices (e.g. set before linking)
    never wins over the owner's routing group, whichever connection Asterisk
    hands the call to."""
    p = platform
    await db_session.create_phone_number(
        p.hv.org_id, p.hv.config.id, SHARED_DID, inbound_workflow_id=p.hv_other.id
    )
    async with test_client_factory(p.fe.user) as client:
        r = await client.patch(
            f"{BASE}/{p.group['id']}",
            json={
                "targets": [
                    {"workflow_id": p.alex.id, "weight": 100},
                    {"workflow_id": p.re_agent.id, "weight": 0},
                    {"workflow_id": p.hv_agent.id, "weight": 0},
                ]
            },
        )
        assert r.status_code == 200
    for connection_org in (p.hv, p.fe):
        conn = _Asterisk(p, [p.fe, p.re, p.hv])
        conn.organization_id = connection_org.org_id
        conn.telephony_configuration_id = connection_org.config.id
        await conn._handle_inbound_stasis_start(
            f"chan-{connection_org.org_id}", "Ring", _stasis("+13035550128")
        )
        ((_, _, workflow_id, media_org),) = conn.media
        assert (workflow_id, media_org) == (p.alex.id, p.fe.org_id)


# ---------------------------------------------------------------------------
# 16: screening stays centralized
# ---------------------------------------------------------------------------


async def test_unscreened_channel_is_refused_before_any_routing(
    platform, db_session, monkeypatch, quota_ok
):
    """With the owner's "answer only Hopwhistle-screened callers" on, a call
    that reached Stasis any way but the hopwhistle-callback endpoint is hung
    up before routing picks a workspace, so no linked workspace can be used
    to bypass Hopwhistle's screening."""
    p = platform

    async def required(org_id):
        return org_id == p.fe.org_id

    monkeypatch.setattr(ari_manager, "screening_required", required)
    resolver_calls = []
    real = ari_manager.resolve_inbound_workflow_id

    async def spy(*a, **kw):
        resolver_calls.append(a)
        return await real(*a, **kw)

    monkeypatch.setattr(ari_manager, "resolve_inbound_workflow_id", spy)
    conn = _Asterisk(p, [p.fe, p.re, p.hv])
    await conn._handle_inbound_stasis_start(
        "chan-carrier", "Ring", _stasis("+13035550129", channel="PJSIP/fractel-0001")
    )
    assert conn.deleted == ["chan-carrier"] and resolver_calls == []

    await conn._handle_inbound_stasis_start("chan-ok", "Ring", _stasis("+13035550130"))
    assert len(resolver_calls) == 1  # one screened call -> one routing decision
    assert len(conn.media) == 1
