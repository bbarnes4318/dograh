"""Unit tests for the inbound routing resolver
(api/services/telephony/inbound_routing.py)."""

import random
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.inbound_routing import (
    caller_bucket,
    normalize_caller,
    order_targets,
    resolve_inbound_workflow_id,
)

ORG = 11


def _phone(group_id=None, fallback=15, org=ORG):
    return SimpleNamespace(
        id=501,
        organization_id=org,
        inbound_workflow_id=fallback,
        inbound_routing_group_id=group_id,
    )


def _group(weights, *, is_active=True, sticky=True, org=ORG, group_id=7):
    return SimpleNamespace(
        id=group_id,
        organization_id=org,
        name="Campaign callbacks",
        is_active=is_active,
        sticky_by_caller=sticky,
        targets=[SimpleNamespace(workflow_id=w, weight=p) for w, p in weights],
    )


def _workflows(*active_ids, archived=(), org=ORG):
    """get_workflow stand-in: active/archived workflows in ``org``."""

    async def get_workflow(workflow_id, organization_id=None, user_id=None):
        if organization_id != org:
            return None
        if workflow_id in active_ids:
            return SimpleNamespace(id=workflow_id, status="active")
        if workflow_id in archived:
            return SimpleNamespace(id=workflow_id, status="archived")
        return None

    return AsyncMock(side_effect=get_workflow)


def _random_callers(n, seed=1234):
    rng = random.Random(seed)
    return [f"+1{rng.randrange(2_000_000_000, 9_999_999_999)}" for _ in range(n)]


@pytest.fixture
def mock_db():
    with patch("api.services.telephony.inbound_routing.db_client") as db:
        yield db


async def test_no_group_falls_back_to_inbound_workflow_id(mock_db):
    workflow_id, meta = await resolve_inbound_workflow_id(_phone(), "+15551230000")

    assert workflow_id == 15
    assert meta is None  # nothing new recorded for ungrouped numbers
    mock_db.get_inbound_routing_group_with_targets.assert_not_called()


async def test_no_group_and_no_fallback_returns_none(mock_db):
    workflow_id, meta = await resolve_inbound_workflow_id(
        _phone(fallback=None), "+15551230000"
    )
    assert workflow_id is None and meta is None


async def test_100_0_split_routes_everything_to_one_agent(mock_db):
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(15, 0), (25, 100)])
    )
    mock_db.get_workflow = _workflows(15, 25)

    results = Counter()
    for caller in _random_callers(500):
        workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), caller)
        results[workflow_id] += 1

    assert results == {25: 500}
    assert meta["source"] == "group"
    assert meta["weights"] == {"15": 0, "25": 100}
    assert meta["group_name"] == "Campaign callbacks"


async def test_50_50_sticky_same_caller_same_agent(mock_db):
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(15, 50), (25, 50)], sticky=True)
    )
    mock_db.get_workflow = _workflows(15, 25)

    for caller in _random_callers(50):
        first, meta = await resolve_inbound_workflow_id(_phone(7), caller)
        assert meta["mode"] == "sticky"
        for _ in range(5):
            again, _ = await resolve_inbound_workflow_id(_phone(7), caller)
            assert again == first

    # Formatting differences of the same number hash the same.
    a, _ = await resolve_inbound_workflow_id(_phone(7), "+1 (555) 201-3344")
    b, _ = await resolve_inbound_workflow_id(_phone(7), "+15552013344")
    assert a == b


def test_50_50_sticky_distribution_over_10000_callers():
    counts = Counter()
    for caller in _random_callers(10_000):
        ordered, pick = order_targets([(15, 50), (25, 50)], caller, True)
        assert pick["mode"] == "sticky"
        counts[ordered[0]] += 1

    for workflow_id in (15, 25):
        share = counts[workflow_id] / 10_000
        assert 0.48 <= share <= 0.52, counts


def test_70_30_random_distribution():
    rng = random.Random(42)
    counts = Counter()
    for _ in range(10_000):
        ordered, pick = order_targets(
            [(15, 70), (25, 30)], "+15551230000", False, rng=rng
        )
        assert pick["mode"] == "random"
        counts[ordered[0]] += 1

    assert 0.68 <= counts[15] / 10_000 <= 0.72, counts
    assert 0.28 <= counts[25] / 10_000 <= 0.32, counts


def test_sticky_mapping_ignores_saved_target_order():
    caller = "+15559876543"
    a, _ = order_targets([(15, 50), (25, 50)], caller, True)
    b, _ = order_targets([(25, 50), (15, 50)], caller, True)
    assert a == b


def test_sticky_without_caller_id_uses_random():
    _, pick = order_targets([(15, 50), (25, 50)], "unknown", True)
    assert pick["mode"] == "random_no_caller_id"
    assert normalize_caller("anonymous") is None
    assert 0 <= caller_bucket("+15551230000") < 100


async def test_invalid_target_falls_back_to_next_target(mock_db):
    # 100% on 99, which doesn't exist; 25 has weight too and takes the call.
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(99, 60), (25, 40)])
    )
    mock_db.get_workflow = _workflows(15, 25)

    for caller in _random_callers(200):
        workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), caller)
        assert workflow_id == 25
        assert meta["source"] == "group"
        if meta["picked_workflow_id"] == 99:
            assert meta["skipped_targets"][0]["workflow_id"] == 99


async def test_archived_and_foreign_targets_fall_back_to_inbound_workflow_id(mock_db):
    # 30 is archived; 40 belongs to another org (get_workflow scoped to ORG
    # returns None for it).
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(30, 50), (40, 50)])
    )
    mock_db.get_workflow = _workflows(15, archived=(30,))

    workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), "+15551230000")

    assert workflow_id == 15
    assert meta["source"] == "fallback"
    assert meta["fallback_reason"] == "no_usable_target"
    assert {s["workflow_id"] for s in meta["skipped_targets"]} == {30, 40}


async def test_inactive_group_falls_back(mock_db):
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(25, 100)], is_active=False)
    )
    mock_db.get_workflow = _workflows(15, 25)

    workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), "+15551230000")

    assert workflow_id == 15
    assert meta["fallback_reason"] == "group_inactive"


async def test_group_with_only_zero_weights_falls_back(mock_db):
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(25, 0)])
    )
    workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), "+15551230000")
    assert workflow_id == 15
    assert meta["fallback_reason"] == "group_has_no_weighted_targets"


async def test_group_from_another_org_is_ignored(mock_db):
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        return_value=_group([(25, 100)], org=999)
    )
    workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), "+15551230000")
    assert workflow_id == 15
    assert meta["fallback_reason"] == "group_not_found"


async def test_db_error_never_drops_the_call(mock_db):
    mock_db.get_inbound_routing_group_with_targets = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    workflow_id, meta = await resolve_inbound_workflow_id(_phone(7), "+15551230000")
    assert workflow_id == 15
    assert meta["fallback_reason"] == "resolver_error"
