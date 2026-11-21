"""Controlled, reversible switch of ONE organization's or ONE workflow's STT to HopSTT.

Nothing switches automatically. An operator runs ``scripts/hopstt_migrate.py``
for a single target; it is a dry run (diff only) unless ``--apply``. On apply,
the prior STT JSON of every touched row is written to a backup file so the
change can be rolled back exactly.

Targets:
  * organization: the org's ``MODEL_CONFIGURATION_V2`` row. Must be BYOK
    pipeline mode (Dograh-managed and realtime configs have no STT block to
    switch). Affects every workflow of the org that has no own override.
  * workflow: the ``model_configuration_v2_override`` of the workflow's
    released (live) definition, plus its open draft if it has one, so the next
    publish does not silently revert it. Runs pin a definition, so this is the
    row calls read. A workflow without a v2 override is refused: switch the
    org, or give the workflow an override in the UI first.

Every new config is validated with the same pydantic models the API uses
before anything is written, so a row can never become the "silently empty"
invalid org config (see ai_model_configuration._parse_...).
"""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from api.db import db_client
from api.db.models import WorkflowDefinitionModel
from api.enums import OrganizationConfigurationKey
from api.schemas.ai_model_configuration import OrganizationAIModelConfigurationV2
from api.services.configuration.ai_model_configuration import (
    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
)
from api.services.configuration.masking import SERVICE_SECRET_FIELDS, mask_key
from api.services.configuration.registry import (
    HOPSTT_STT_LANGUAGES,
    HopSTTSTTConfiguration,
)


class HopSTTMigrationError(ValueError):
    pass


@dataclass
class TargetChange:
    kind: str  # "organization" | "workflow_definition"
    id: int
    before: dict
    after: dict
    prior_stt: dict


@dataclass
class MigrationPlan:
    organization_id: int
    workflow_id: int | None
    changes: list[TargetChange] = field(default_factory=list)


# ------------------------------------------------------------ pure helpers


def build_hopstt_stt_block(
    previous_stt: dict | None,
    *,
    endpoint: str | None = None,
    model: str | None = None,
    language: str | None = None,
    api_key: str | None = None,
) -> dict:
    """A HopSTT STT block. Keeps the previous language when HopSTT supports it."""
    prev_language = (previous_stt or {}).get("language")
    block: dict[str, Any] = {"provider": "hopstt"}
    block["model"] = model or "nemotron-3.5-streaming"
    if language:
        block["language"] = language
    elif prev_language in HOPSTT_STT_LANGUAGES and prev_language != "auto":
        block["language"] = prev_language
    else:
        block["language"] = "en"
    if endpoint:
        block["endpoint"] = endpoint
    if api_key:
        block["api_key"] = api_key
    return HopSTTSTTConfiguration.model_validate(block).model_dump(
        mode="json", exclude_none=True
    )


def _pipeline_of(config: dict) -> dict:
    if config.get("mode") != "byok":
        raise HopSTTMigrationError(
            "only BYOK configurations have an STT block to switch "
            f"(mode is {config.get('mode')!r})"
        )
    byok = config.get("byok") or {}
    if byok.get("mode") != "pipeline" or not isinstance(byok.get("pipeline"), dict):
        raise HopSTTMigrationError(
            "only BYOK pipeline configurations use an STT provider "
            f"(byok.mode is {byok.get('mode')!r})"
        )
    return byok["pipeline"]


def _validated(config: dict) -> dict:
    try:
        OrganizationAIModelConfigurationV2.model_validate(config)
    except Exception as exc:
        raise HopSTTMigrationError(
            f"resulting configuration is invalid: {exc}"
        ) from exc
    return config


def switch_v2_stt(config: dict, new_stt: dict) -> tuple[dict, dict]:
    """Return (new_config, prior_stt) with the pipeline STT replaced."""
    updated = copy.deepcopy(config)
    pipeline = _pipeline_of(updated)
    prior = copy.deepcopy(pipeline.get("stt") or {})
    if prior.get("provider") == "hopstt":
        raise HopSTTMigrationError("STT is already hopstt; nothing to do")
    pipeline["stt"] = copy.deepcopy(new_stt)
    return _validated(updated), prior


def restore_v2_stt(config: dict, prior_stt: dict, *, force: bool = False) -> dict:
    updated = copy.deepcopy(config)
    pipeline = _pipeline_of(updated)
    current = (pipeline.get("stt") or {}).get("provider")
    if current != "hopstt" and not force:
        raise HopSTTMigrationError(
            f"current STT provider is {current!r}, not hopstt; refusing to "
            "overwrite a change made since the migration (use --force)"
        )
    pipeline["stt"] = copy.deepcopy(prior_stt)
    return _validated(updated)


def _masked(value):
    if isinstance(value, dict):
        return {
            k: (
                ([mask_key(x) for x in v] if isinstance(v, list) else mask_key(v))
                if k in SERVICE_SECRET_FIELDS and v
                else _masked(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def diff_text(before: dict, after: dict, label: str) -> str:
    """Unified diff of two configs, secrets masked."""
    a = json.dumps(_masked(before), indent=2, sort_keys=True).splitlines()
    b = json.dumps(_masked(after), indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(a, b, f"{label} (before)", f"{label} (after)", lineterm="")
    )


# ---------------------------------------------------------------- planning


async def _org_config(organization_id: int) -> dict:
    row = await db_client.get_configuration(
        organization_id, OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    )
    if row is None or not row.value:
        raise HopSTTMigrationError(
            f"organization {organization_id} has no v2 model configuration"
        )
    return copy.deepcopy(row.value)


async def _workflow_definitions(workflow_id: int, organization_id: int):
    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    if workflow is None:
        raise HopSTTMigrationError(
            f"workflow {workflow_id} not found in organization {organization_id}"
        )
    definitions = []
    if workflow.released_definition is not None:
        definitions.append(workflow.released_definition)
    draft = await db_client.get_draft_version(workflow_id)
    if draft is not None and all(d.id != draft.id for d in definitions):
        definitions.append(draft)
    return definitions


async def plan_switch(
    *,
    organization_id: int,
    workflow_id: int | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    language: str | None = None,
) -> MigrationPlan:
    plan = MigrationPlan(organization_id=organization_id, workflow_id=workflow_id)
    if workflow_id is None:
        before = await _org_config(organization_id)
        prior = _pipeline_of(before).get("stt")
        new_stt = build_hopstt_stt_block(
            prior, endpoint=endpoint, model=model, language=language
        )
        after, prior = switch_v2_stt(before, new_stt)
        plan.changes.append(
            TargetChange("organization", organization_id, before, after, prior)
        )
        return plan

    for definition in await _workflow_definitions(workflow_id, organization_id):
        configs = copy.deepcopy(definition.workflow_configurations or {})
        override = configs.get(WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY)
        if not override:
            continue
        prior = _pipeline_of(override).get("stt")
        new_stt = build_hopstt_stt_block(
            prior, endpoint=endpoint, model=model, language=language
        )
        new_override, prior = switch_v2_stt(override, new_stt)
        after = {**configs, WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: new_override}
        plan.changes.append(
            TargetChange("workflow_definition", definition.id, configs, after, prior)
        )
    if not plan.changes:
        raise HopSTTMigrationError(
            f"workflow {workflow_id} has no {WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY}; "
            "it follows the organization config. Switch the organization, or give "
            "the workflow its own model configuration in the UI first."
        )
    return plan


async def plan_rollback(backup: dict, *, force: bool = False) -> MigrationPlan:
    organization_id = int(backup["organization_id"])
    plan = MigrationPlan(
        organization_id=organization_id, workflow_id=backup.get("workflow_id")
    )
    definitions = {}
    if backup.get("workflow_id") is not None:
        definitions = {
            d.id: d
            for d in await _workflow_definitions(
                int(backup["workflow_id"]), organization_id
            )
        }
    for target in backup["targets"]:
        if target["kind"] == "organization":
            if int(target["id"]) != organization_id:
                raise HopSTTMigrationError(
                    "backup target does not match its organization"
                )
            before = await _org_config(organization_id)
            after = restore_v2_stt(before, target["prior_stt"], force=force)
        else:
            definition = definitions.get(int(target["id"]))
            if definition is None:
                raise HopSTTMigrationError(
                    f"workflow definition {target['id']} is no longer the live or draft "
                    "version of this workflow; restore it by hand"
                )
            before = copy.deepcopy(definition.workflow_configurations or {})
            override = before.get(WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY)
            if not override:
                raise HopSTTMigrationError(
                    f"definition {definition.id} lost its override"
                )
            after = {
                **before,
                WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: restore_v2_stt(
                    override, target["prior_stt"], force=force
                ),
            }
        plan.changes.append(
            TargetChange(
                target["kind"], int(target["id"]), before, after, target["prior_stt"]
            )
        )
    return plan


def backup_document(plan: MigrationPlan) -> dict:
    """What --apply saves. Contains the prior STT credentials: keep it private."""
    return {
        "kind": "hopstt_migration_backup",
        "taken_at": datetime.now(UTC).isoformat(),
        "organization_id": plan.organization_id,
        "workflow_id": plan.workflow_id,
        "targets": [
            {"kind": c.kind, "id": c.id, "prior_stt": c.prior_stt} for c in plan.changes
        ],
    }


async def apply_plan(plan: MigrationPlan) -> None:
    """Write every change in one transaction (org row via its client)."""
    definition_changes = [c for c in plan.changes if c.kind == "workflow_definition"]
    for change in plan.changes:
        if change.kind == "organization":
            await db_client.upsert_configuration(
                plan.organization_id,
                OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value,
                change.after,
                last_validated_at=datetime.now(UTC),
            )
    if definition_changes:
        async with db_client.async_session() as session:
            for change in definition_changes:
                await session.execute(
                    update(WorkflowDefinitionModel)
                    .where(WorkflowDefinitionModel.id == change.id)
                    .values(workflow_configurations=change.after)
                )
            await session.commit()
