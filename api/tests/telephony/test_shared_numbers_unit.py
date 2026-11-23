"""Pure pieces of shared-number routing: which row answers a DID held in
several workspaces, the sticky key, labels, and the stats explanation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from api.schemas.inbound_routing import InboundRoutingStatsResponse, RoutingTargetStats
from api.services.telephony import linked_workspaces
from api.services.telephony.inbound_routing import caller_bucket, normalize_caller
from api.services.telephony.inbound_routing_groups import enrich_stats, sample_note
from api.services.telephony.linked_workspaces import parse_self_label
from api.services.telephony.shared_numbers import choose_inbound_row

FE, RE, HV = 1, 3, 7


def _row(org, workflow=None, group=None):
    return SimpleNamespace(
        organization_id=org,
        address="+17205550101",
        inbound_workflow_id=workflow,
        inbound_routing_group_id=group,
    )


def test_routed_row_beats_caller_id_only_copy():
    copy, owner = _row(HV), _row(FE, workflow=15)
    assert choose_inbound_row([copy, owner], {}) is owner


def test_owner_group_beats_a_linked_workspaces_own_agent_in_any_order():
    owner, hv_copy = _row(FE, workflow=15, group=9), _row(HV, workflow=40)
    links = {FE: [RE, HV], HV: []}
    assert choose_inbound_row([hv_copy, owner], links) is owner
    assert choose_inbound_row([owner, hv_copy], links) is owner


def test_owner_that_linked_the_other_wins_when_both_have_groups():
    owner, hv_copy = _row(FE, group=9), _row(HV, group=12)
    links = {FE: [HV], HV: []}
    assert choose_inbound_row([hv_copy, owner], links) is owner


def test_unrelated_conflict_is_deterministic_lowest_org():
    a, b = _row(5, workflow=1), _row(4, workflow=2)
    assert choose_inbound_row([a, b], {}) is b
    assert choose_inbound_row([b, a], {}) is b


def test_no_routed_rows_keeps_first_match_and_empty_is_none():
    first, second = _row(HV), _row(FE)
    assert choose_inbound_row([first, second], {}) is first
    assert choose_inbound_row([], {}) is None


def test_ten_digit_and_e164_callers_share_a_sticky_bucket():
    keys = {
        normalize_caller(x)
        for x in ("+13035550199", "13035550199", "3035550199", "(303) 555-0199")
    }
    assert keys == {"+13035550199"}
    assert caller_bucket("+13035550199", 100) == caller_bucket(
        normalize_caller("3035550199"), 100
    )
    assert normalize_caller("anonymous") is None


def test_self_label_default_and_value():
    assert parse_self_label({"self_label": " Final Expense "}, FE) == "Final Expense"
    assert parse_self_label({}, FE) == "Workspace 1"
    assert parse_self_label(None, FE) == "Workspace 1"


async def test_linking_workspaces_is_the_reverse_of_the_link(monkeypatch):
    rows = [
        SimpleNamespace(
            organization_id=FE,
            value={
                "self_label": "Final Expense",
                "workspaces": [
                    {"organization_id": RE, "label": "Real Estate"},
                    {"organization_id": HV, "label": "Holiday Voices"},
                ],
            },
        ),
        SimpleNamespace(organization_id=RE, value={"workspaces": []}),
    ]
    monkeypatch.setattr(
        linked_workspaces,
        "db_client",
        SimpleNamespace(get_configurations_by_key=AsyncMock(return_value=rows)),
    )
    (owner,) = await linked_workspaces.linking_workspaces(HV)
    assert (owner.organization_id, owner.label, owner.my_label) == (
        FE,
        "Final Expense",
        "Holiday Voices",
    )
    assert await linked_workspaces.linking_workspaces(FE) == []


async def test_linking_workspaces_unreadable_means_nothing_shared(monkeypatch):
    monkeypatch.setattr(
        linked_workspaces,
        "db_client",
        SimpleNamespace(
            get_configurations_by_key=AsyncMock(side_effect=RuntimeError("db"))
        ),
    )
    assert await linked_workspaces.linking_workspaces(HV) == []


def _stats(rows):
    return InboundRoutingStatsResponse(
        group_id=9,
        hours=24,
        since=datetime.now(UTC),
        total_calls=sum(r.calls for r in rows),
        targets=rows,
    )


def test_enrich_stats_counts_picks_fallbacks_and_refusals():
    rows = [
        RoutingTargetStats(
            workflow_id=15, calls=2, actual_percent=66.7, target_percent=50
        ),
        RoutingTargetStats(
            workflow_id=40, calls=1, actual_percent=33.3, target_percent=25
        ),
        RoutingTargetStats(
            workflow_id=70, calls=0, actual_percent=0, target_percent=25
        ),
    ]
    decisions = [
        {"workflow_id": 15, "meta": {"picked_workflow_id": 15, "source": "group"}},
        {"workflow_id": 40, "meta": {"picked_workflow_id": 40, "source": "group"}},
        {
            "workflow_id": 15,
            "meta": {
                "picked_workflow_id": 70,
                "source": "group",
                "changed_workspace": True,
                "skipped_targets": [
                    {
                        "workflow_id": 70,
                        "organization_id": HV,
                        "reason": "status is archived",
                    }
                ],
            },
        },
    ]
    refusals = [
        {
            "reason": "workspace at its concurrent call limit",
            "organization_id": HV,
            "workflow_id": 70,
        }
    ]
    details = {
        15: {
            "agent_name": "Alex",
            "organization_id": FE,
            "organization_label": "Final Expense",
        },
        70: {
            "agent_name": "Shopping & Phone Orders",
            "organization_id": HV,
            "organization_label": "Holiday Voices",
        },
    }
    out = enrich_stats(
        _stats(rows), decisions, refusals, details, sticky_by_caller=False
    )
    by_id = {r.workflow_id: r for r in out.targets}
    assert by_id[70].picked == 1 and by_id[70].calls == 0
    assert by_id[70].unavailable == 2
    assert by_id[70].unavailable_reasons == {
        "status is archived": 1,
        "workspace at its concurrent call limit": 1,
    }
    assert by_id[70].organization_label == "Holiday Voices"
    assert by_id[15].fallback_calls == 1
    assert out.fallback_calls == 1 and out.cross_workspace_fallbacks == 1
    assert out.refused_calls == 1 and out.eligible_calls == 4
    assert out.refusals[0].organization_id == HV


def test_sample_note_is_honest_about_small_samples_and_sticky():
    small = sample_note(12, sticky_by_caller=True)
    assert "Only 12 call(s)" in small and "25 of every 100" in small
    assert "repeat callers return to the same agent" in small
    big = sample_note(5000, sticky_by_caller=False)
    assert "drift a few points" in big and "Sticky" not in big
