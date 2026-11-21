"""Controlled HopSTT migration: pure helpers and DB round trips (test DB)."""

from __future__ import annotations

import copy

import pytest

from api.db.models import OrganizationModel, UserModel
from api.enums import OrganizationConfigurationKey
from api.services.configuration.ai_model_configuration import (
    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
)
from api.services.configuration.hopstt_migration import (
    HopSTTMigrationError,
    apply_plan,
    backup_document,
    build_hopstt_stt_block,
    diff_text,
    plan_rollback,
    plan_switch,
    restore_v2_stt,
    switch_v2_stt,
)

CARTESIA_STT = {
    "provider": "cartesia",
    "api_key": "cartesia-key-abcd",
    "model": "ink-whisper",
    "language": "es",
}
BYOK = {
    "version": 2,
    "mode": "byok",
    "byok": {
        "mode": "pipeline",
        "pipeline": {
            "llm": {"provider": "openai", "api_key": "sk-llm-1234", "model": "gpt-4.1"},
            "tts": {
                "provider": "cartesia",
                "api_key": "cartesia-key-abcd",
                "model": "sonic-3",
                "voice": "a0e99841-438c-4a64-b679-ae501e7d6091",
            },
            "stt": CARTESIA_STT,
        },
    },
}
GRAPH = {
    "nodes": [
        {
            "id": "1",
            "type": "startCall",
            "position": {"x": 0, "y": 0},
            "data": {"name": "Start", "prompt": "Hi", "is_start": True},
        },
        {
            "id": "2",
            "type": "endCall",
            "position": {"x": 0, "y": 100},
            "data": {"name": "End", "prompt": "Bye"},
        },
    ],
    "edges": [{"id": "e1", "source": "1", "target": "2", "data": {"label": "End"}}],
}


def test_switch_keeps_language_and_round_trips():
    new_stt = build_hopstt_stt_block(CARTESIA_STT)
    assert new_stt["provider"] == "hopstt" and new_stt["language"] == "es"
    after, prior = switch_v2_stt(BYOK, new_stt)
    assert prior == CARTESIA_STT
    assert after["byok"]["pipeline"]["stt"]["provider"] == "hopstt"
    assert after["byok"]["pipeline"]["llm"] == BYOK["byok"]["pipeline"]["llm"]
    assert BYOK["byok"]["pipeline"]["stt"] == CARTESIA_STT  # input untouched
    restored = restore_v2_stt(after, prior)
    assert restored == BYOK


def test_refuses_dograh_mode_and_double_switch():
    with pytest.raises(HopSTTMigrationError, match="BYOK"):
        switch_v2_stt({"version": 2, "mode": "dograh", "dograh": {"api_key": "x"}}, {})
    after, _ = switch_v2_stt(BYOK, build_hopstt_stt_block(CARTESIA_STT))
    with pytest.raises(HopSTTMigrationError, match="already hopstt"):
        switch_v2_stt(after, build_hopstt_stt_block(None))


def test_rollback_refuses_if_changed_since_unless_forced():
    with pytest.raises(HopSTTMigrationError, match="refusing"):
        restore_v2_stt(BYOK, CARTESIA_STT)
    assert restore_v2_stt(BYOK, CARTESIA_STT, force=True) == BYOK


def test_diff_masks_secrets():
    after, _ = switch_v2_stt(BYOK, build_hopstt_stt_block(CARTESIA_STT))
    text = diff_text(BYOK, after, "organization 1")
    assert "cartesia-key-abcd" not in text and "sk-llm-1234" not in text
    assert any(
        line.startswith("+") and '"provider": "hopstt"' in line
        for line in text.splitlines()
    )


@pytest.fixture
async def org_user(async_session):
    org = OrganizationModel(provider_id="test-org-hopstt-migration")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(
        provider_id="test-user-hopstt-migration", selected_organization_id=org.id
    )
    async_session.add(user)
    await async_session.flush()
    return org, user


async def test_org_switch_dry_run_apply_and_rollback(db_session, org_user):
    org, _ = org_user
    key = OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    await db_session.upsert_configuration(org.id, key, copy.deepcopy(BYOK))

    plan = await plan_switch(organization_id=org.id)
    assert len(plan.changes) == 1
    # Planning (dry run) writes nothing.
    assert (await db_session.get_configuration(org.id, key)).value == BYOK

    backup = backup_document(plan)
    await apply_plan(plan)
    stored = (await db_session.get_configuration(org.id, key)).value
    assert stored["byok"]["pipeline"]["stt"]["provider"] == "hopstt"
    assert stored["byok"]["pipeline"]["llm"] == BYOK["byok"]["pipeline"]["llm"]

    await apply_plan(await plan_rollback(backup))
    assert (await db_session.get_configuration(org.id, key)).value == BYOK


async def test_workflow_override_switch_and_rollback(db_session, org_user):
    org, user = org_user
    workflow = await db_session.create_workflow(
        name="HopSTT wf",
        workflow_definition=GRAPH,
        user_id=user.id,
        organization_id=org.id,
    )
    with pytest.raises(
        HopSTTMigrationError, match="no model_configuration_v2_override"
    ):
        await plan_switch(organization_id=org.id, workflow_id=workflow.id)

    released = (
        await db_session.get_workflow(workflow.id, organization_id=org.id)
    ).released_definition
    configs = {
        "max_call_duration": 300,
        WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: copy.deepcopy(BYOK),
    }
    from sqlalchemy import update

    from api.db.models import WorkflowDefinitionModel

    async with db_session.async_session() as session:
        await session.execute(
            update(WorkflowDefinitionModel)
            .where(WorkflowDefinitionModel.id == released.id)
            .values(workflow_configurations=configs)
        )
        await session.commit()

    # Another org cannot target this workflow.
    with pytest.raises(HopSTTMigrationError, match="not found"):
        await plan_switch(organization_id=org.id + 999, workflow_id=workflow.id)

    plan = await plan_switch(
        organization_id=org.id, workflow_id=workflow.id, language="en"
    )
    assert [c.id for c in plan.changes] == [released.id]
    backup = backup_document(plan)
    await apply_plan(plan)

    wf = await db_session.get_workflow(workflow.id, organization_id=org.id)
    stored = wf.released_definition.workflow_configurations
    stt = stored[WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY]["byok"]["pipeline"][
        "stt"
    ]
    assert stt["provider"] == "hopstt" and stt["language"] == "en"
    assert stored["max_call_duration"] == 300

    await apply_plan(await plan_rollback(backup))
    wf = await db_session.get_workflow(workflow.id, organization_id=org.id)
    assert wf.released_definition.workflow_configurations == configs
