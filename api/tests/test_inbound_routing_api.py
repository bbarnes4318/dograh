"""API tests for /api/v1/inbound-routing-groups against the test database,
plus the resolver reading real rows."""

from types import SimpleNamespace

from api.enums import CallType
from api.services.telephony.inbound_routing import resolve_inbound_workflow_id

BASE = "/api/v1/inbound-routing-groups"


async def _make_user(db_session, slug: str):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"{slug}_user")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"{slug}_org", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    return await db_session.get_user_by_id(user.id)


async def _workflow(db_session, user, name):
    return await db_session.create_workflow(
        name, {"nodes": [], "edges": []}, user.id, user.selected_organization_id
    )


async def _config_with_numbers(db_session, user, name, numbers, workflow_id):
    config = await db_session.create_telephony_configuration(
        user.selected_organization_id, name, "ari", {}
    )
    rows = [
        await db_session.create_phone_number(
            user.selected_organization_id,
            config.id,
            number,
            inbound_workflow_id=workflow_id,
        )
        for number in numbers
    ]
    return config, rows


async def test_rejects_weights_not_summing_to_100(test_client_factory, db_session):
    user = await _make_user(db_session, "routing_sum")
    a = await _workflow(db_session, user, "Alex")
    b = await _workflow(db_session, user, "Robert")

    async with test_client_factory(user) as client:
        bad = await client.post(
            BASE,
            json={
                "name": "Split",
                "targets": [
                    {"workflow_id": a.id, "weight": 60},
                    {"workflow_id": b.id, "weight": 30},
                ],
            },
        )
        over = await client.post(
            BASE,
            json={"name": "Split", "targets": [{"workflow_id": a.id, "weight": 101}]},
        )
        dup = await client.post(
            BASE,
            json={
                "name": "Split",
                "targets": [
                    {"workflow_id": a.id, "weight": 50},
                    {"workflow_id": a.id, "weight": 50},
                ],
            },
        )
        ok = await client.post(
            BASE,
            json={"name": "Split", "targets": [{"workflow_id": a.id, "weight": 100}]},
        )
        bad_patch = await client.patch(
            f"{BASE}/{ok.json()['id']}",
            json={"targets": [{"workflow_id": a.id, "weight": 99}]},
        )

    assert bad.status_code == 422
    assert "exactly 100" in bad.text
    assert over.status_code == 422
    assert dup.status_code == 422
    assert ok.status_code == 200, ok.text
    assert bad_patch.status_code == 422


async def test_rejects_workflow_from_another_org(test_client_factory, db_session):
    user = await _make_user(db_session, "routing_org_a")
    other = await _make_user(db_session, "routing_org_b")
    mine = await _workflow(db_session, user, "Mine")
    theirs = await _workflow(db_session, other, "Theirs")

    async with test_client_factory(user) as client:
        response = await client.post(
            BASE,
            json={
                "name": "Split",
                "targets": [
                    {"workflow_id": mine.id, "weight": 50},
                    {"workflow_id": theirs.id, "weight": 50},
                ],
            },
        )
        assert response.status_code == 422
        assert str(theirs.id) in response.text

    # Groups from one org are invisible to another.
    async with test_client_factory(user) as client:
        created = await client.post(
            BASE,
            json={"name": "Mine", "targets": [{"workflow_id": mine.id, "weight": 100}]},
        )
    async with test_client_factory(other) as client:
        assert (await client.get(f"{BASE}/{created.json()['id']}")).status_code == 404
        assert (await client.get(BASE)).json()["groups"] == []


async def test_group_lifecycle_attach_route_stats_audit_detach(
    test_client_factory, db_session
):
    user = await _make_user(db_session, "routing_flow")
    org_id = user.selected_organization_id
    alex = await _workflow(db_session, user, "Alex")
    robert = await _workflow(db_session, user, "Robert")
    other_agent = await _workflow(db_session, user, "Support")

    config_1, numbers_1 = await _config_with_numbers(
        db_session, user, "cfg1", ["+15550000001", "+15550000002"], alex.id
    )
    config_4, numbers_4 = await _config_with_numbers(
        db_session, user, "cfg4", ["+15550000003"], alex.id
    )
    _, untouched = await _config_with_numbers(
        db_session, user, "cfg-other", ["+15550000009"], other_agent.id
    )

    async with test_client_factory(user) as client:
        created = await client.post(
            BASE,
            json={
                "name": "Campaign callbacks",
                "sticky_by_caller": True,
                "targets": [
                    {"workflow_id": alex.id, "weight": 50},
                    {"workflow_id": robert.id, "weight": 50},
                ],
            },
        )
        assert created.status_code == 200, created.text
        group = created.json()
        group_id = group["id"]
        assert group["number_count"] == 0
        assert {t["workflow_name"] for t in group["targets"]} == {"Alex", "Robert"}

        selection = {"telephony_configuration_ids": [config_1.id, config_4.id]}
        preview = await client.post(
            f"{BASE}/{group_id}/numbers", json={**selection, "dry_run": True}
        )
        assert preview.json()["affected"] == 3
        assert preview.json()["number_count"] == 0  # dry run wrote nothing

        attached = await client.post(f"{BASE}/{group_id}/numbers", json=selection)
        assert attached.json() == {"affected": 3, "dry_run": False, "number_count": 3}

        summary = (await client.get(f"{BASE}/{group_id}/numbers")).json()
        by_config = {
            c["telephony_configuration_id"]: c for c in summary["configurations"]
        }
        assert summary["number_count"] == 3
        assert by_config[config_1.id]["in_this_group"] == 2
        assert by_config[config_4.id]["in_this_group"] == 1

        listed = (await client.get(BASE)).json()["groups"]
        assert [g["number_count"] for g in listed] == [3]

    # Number outside the group is untouched: routes to its own workflow.
    untouched_row = await db_session.get_phone_number(untouched[0].id)
    assert untouched_row.inbound_routing_group_id is None
    assert await resolve_inbound_workflow_id(untouched_row, "+15559990000") == (
        other_agent.id,
        None,
    )

    # Grouped numbers route through the group, read fresh from the DB.
    grouped_row = await db_session.get_phone_number(numbers_1[0].id)
    workflow_id, meta = await resolve_inbound_workflow_id(grouped_row, "+15557770000")
    assert workflow_id in (alex.id, robert.id)
    assert meta["group_id"] == group_id and meta["mode"] == "sticky"

    # Record a few inbound runs the way the call paths do, then check stats.
    for wid, n in ((alex.id, 3), (robert.id, 1)):
        for i in range(n):
            await db_session.create_workflow_run(
                f"run-{wid}-{i}",
                wid,
                "ari",
                user_id=user.id,
                call_type=CallType.INBOUND,
                initial_context={
                    "inbound_routing": {"group_id": group_id, "workflow_id": wid}
                },
                organization_id=org_id,
            )
    # A run not routed through the group isn't counted.
    await db_session.create_workflow_run(
        "ungrouped", alex.id, "ari", user_id=user.id, organization_id=org_id
    )

    async with test_client_factory(user) as client:
        stats = (await client.get(f"{BASE}/{group_id}/stats?hours=24")).json()
        assert stats["total_calls"] == 4
        rows = {r["workflow_id"]: r for r in stats["targets"]}
        assert rows[alex.id]["calls"] == 3 and rows[alex.id]["actual_percent"] == 75.0
        assert rows[robert.id]["target_percent"] == 50

        # Route everything to Robert, then deactivate.
        patched = await client.patch(
            f"{BASE}/{group_id}",
            json={"targets": [{"workflow_id": robert.id, "weight": 100}]},
        )
        assert patched.status_code == 200, patched.text
        assert [(t["workflow_id"], t["weight"]) for t in patched.json()["targets"]] == [
            (robert.id, 100)
        ]

    workflow_id, _ = await resolve_inbound_workflow_id(
        await db_session.get_phone_number(numbers_4[0].id), "+15551112222"
    )
    assert workflow_id == robert.id  # next call sees the change

    async with test_client_factory(user) as client:
        await client.patch(f"{BASE}/{group_id}", json={"is_active": False})

    workflow_id, meta = await resolve_inbound_workflow_id(
        await db_session.get_phone_number(numbers_4[0].id), "+15551112222"
    )
    assert workflow_id == alex.id and meta["fallback_reason"] == "group_inactive"

    async with test_client_factory(user) as client:
        audit = (await client.get(f"{BASE}/audit?group_id={group_id}")).json()
        actions = [e["action"] for e in audit["entries"]]
        assert actions == [
            "group_updated",
            "group_updated",
            "numbers_attached",
            "group_created",
        ]
        assert audit["entries"][0]["details"]["is_active"] == {
            "from": True,
            "to": False,
        }
        assert audit["entries"][0]["user_email"] is None or isinstance(
            audit["entries"][0]["user_email"], str
        )

        detached = await client.post(
            f"{BASE}/{group_id}/numbers/detach", json={"detach_all": True}
        )
        assert detached.json()["affected"] == 3
        assert detached.json()["number_count"] == 0

        assert (await client.delete(f"{BASE}/{group_id}")).status_code == 200
        assert (await client.get(f"{BASE}/{group_id}")).status_code == 404

    row = await db_session.get_phone_number(numbers_1[0].id)
    assert row.inbound_routing_group_id is None
    assert row.inbound_workflow_id == alex.id


async def test_attach_by_inbound_workflow_and_ids_is_org_scoped(
    test_client_factory, db_session
):
    user = await _make_user(db_session, "routing_scope_a")
    other = await _make_user(db_session, "routing_scope_b")
    agent = await _workflow(db_session, user, "Agent")
    other_agent = await _workflow(db_session, other, "Other")
    _, mine = await _config_with_numbers(
        db_session, user, "cfg", ["+15550000101", "+15550000102"], agent.id
    )
    other_config, theirs = await _config_with_numbers(
        db_session, other, "cfg", ["+15550000201"], other_agent.id
    )

    async with test_client_factory(user) as client:
        group_id = (
            await client.post(
                BASE,
                json={
                    "name": "G",
                    "targets": [{"workflow_id": agent.id, "weight": 100}],
                },
            )
        ).json()["id"]

        by_workflow = await client.post(
            f"{BASE}/{group_id}/numbers", json={"inbound_workflow_id": agent.id}
        )
        assert by_workflow.json()["affected"] == 2

        # Another org's number id is silently out of scope; its config is a 404.
        foreign_ids = await client.post(
            f"{BASE}/{group_id}/numbers", json={"phone_number_ids": [theirs[0].id]}
        )
        assert foreign_ids.json()["affected"] == 0
        foreign_config = await client.post(
            f"{BASE}/{group_id}/numbers",
            json={"telephony_configuration_ids": [other_config.id]},
        )
        assert foreign_config.status_code == 404

        detached = await client.post(
            f"{BASE}/{group_id}/numbers/detach", json={"phone_number_ids": [mine[0].id]}
        )
        assert detached.json() == {"affected": 1, "dry_run": False, "number_count": 1}

    assert (
        await db_session.get_phone_number(theirs[0].id)
    ).inbound_routing_group_id is None


def test_phone_rows_without_group_attribute_still_resolve():
    # Older call sites / fixtures may pass rows without the new column.
    import asyncio

    row = SimpleNamespace(id=1, organization_id=1, inbound_workflow_id=15)
    assert asyncio.run(resolve_inbound_workflow_id(row, "+1555")) == (15, None)
