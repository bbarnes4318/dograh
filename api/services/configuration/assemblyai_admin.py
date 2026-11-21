"""Operator views for the AssemblyAI platform STT default (superuser only).

* :func:`assemblyai_status` - whether the platform credential is provisioned
  (never the key), rollout settings, budgets and metered usage.
* :func:`rollout_report` - a dry run over every organization and workflow:
  which calls the rollout would move onto AssemblyAI and which keep an explicit
  choice. It uses the same classifier the runtime uses at call start, and it
  writes nothing.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.db import db_client
from api.db.models import OrganizationConfigurationModel, WorkflowModel
from api.enums import OrganizationConfigurationKey
from api.schemas.ai_model_configuration import (
    EffectiveAIModelConfiguration,
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.assemblyai import (
    ASSEMBLYAI_DEFAULT_MODEL,
    ASSEMBLYAI_STREAMING_URL,
    in_rollout,
    platform_assemblyai_api_key,
    rollout_settings,
)
from api.services.configuration.resolve import resolve_effective_config
from api.services.pipecat.assemblyai_rollout import (
    ROLLOUT_ELIGIBLE,
    _excluded_org_ids,
    classify_stt,
    workflow_overrides_stt,
)
from api.services.pipecat.assemblyai_usage import usage_snapshot


def credential_status() -> dict:
    return {
        "platform_key_configured": platform_assemblyai_api_key() is not None,
        "env_var": "ASSEMBLYAI_API_KEY",
        "note": "The key itself is never returned. Tenant BYOK keys take precedence.",
    }


async def assemblyai_status(*, days: int = 7) -> dict:
    settings = rollout_settings()
    return {
        "model": ASSEMBLYAI_DEFAULT_MODEL,
        "endpoint": ASSEMBLYAI_STREAMING_URL,
        "credential": credential_status(),
        "rollout": settings.as_dict(),
        "excluded_org_ids": sorted(_excluded_org_ids()),
        "usage": await usage_snapshot(days=days),
    }


def _stt_summary(stt) -> dict | None:
    if stt is None:
        return None
    provider = getattr(stt, "provider", None)
    return {
        "provider": getattr(provider, "value", provider),
        "model": getattr(stt, "model", None),
        "language": getattr(stt, "language", None),
    }


async def rollout_report() -> dict:
    """Classify every org and workflow; nothing is written."""
    settings = rollout_settings()
    excluded = _excluded_org_ids()
    async with db_client.async_session() as session:
        rows = (
            (
                await session.execute(
                    select(OrganizationConfigurationModel).where(
                        OrganizationConfigurationModel.key
                        == OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
                    )
                )
            )
            .scalars()
            .all()
        )
        workflows = (
            (
                await session.execute(
                    select(WorkflowModel).options(
                        selectinload(WorkflowModel.released_definition)
                    )
                )
            )
            .scalars()
            .unique()
            .all()
        )

    organizations = []
    effective_by_org: dict[int, EffectiveAIModelConfiguration] = {}
    for row in rows:
        entry = {"organization_id": row.organization_id}
        try:
            config = OrganizationAIModelConfigurationV2.model_validate(row.value)
            effective = compile_ai_model_configuration_v2(config)
        except Exception as exc:
            entry.update(classification="invalid_config", error=type(exc).__name__)
            organizations.append(entry)
            continue
        effective_by_org[row.organization_id] = effective
        classification = classify_stt(effective)
        eligible = (
            classification in ROLLOUT_ELIGIBLE and row.organization_id not in excluded
        )
        entry.update(
            classification=classification,
            current_stt=_stt_summary(effective.stt),
            eligible_for_platform_default=eligible,
            moves_under_current_rollout=eligible
            and in_rollout(
                settings, organization_id=row.organization_id, workflow_id=None
            ),
        )
        organizations.append(entry)

    workflow_entries = []
    for wf in workflows:
        definition = wf.released_definition
        configs = (definition.workflow_configurations if definition else None) or {}
        org_effective = effective_by_org.get(wf.organization_id)
        if workflow_overrides_stt(configs):
            classification = "workflow_override"
            try:
                from api.services.configuration.ai_model_configuration import (
                    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
                )

                v2 = configs.get(WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY)
                effective = (
                    compile_ai_model_configuration_v2(
                        OrganizationAIModelConfigurationV2.model_validate(v2)
                    )
                    if v2
                    else resolve_effective_config(
                        org_effective or EffectiveAIModelConfiguration(),
                        configs.get("model_overrides"),
                    )
                )
                stt = _stt_summary(effective.stt)
            except Exception:
                stt = None
        else:
            classification = (
                classify_stt(org_effective)
                if org_effective is not None
                else "not_configured"
            )
            stt = _stt_summary(org_effective.stt) if org_effective else None
        eligible = (
            classification in ROLLOUT_ELIGIBLE and wf.organization_id not in excluded
        )
        workflow_entries.append(
            {
                "workflow_id": wf.id,
                "name": wf.name,
                "organization_id": wf.organization_id,
                "status": getattr(wf.status, "value", wf.status),
                "classification": classification,
                "current_stt": stt,
                "moves_under_current_rollout": eligible
                and in_rollout(
                    settings, organization_id=wf.organization_id, workflow_id=wf.id
                ),
            }
        )

    def count(items, key):
        out: dict[str, int] = {}
        for item in items:
            out[item.get(key, "?")] = out.get(item.get(key, "?"), 0) + 1
        return out

    return {
        "rollout": settings.as_dict(),
        "note": (
            "Dry run. Only platform_inherited / not_configured STT choices follow "
            "the platform default; workflow overrides, tenant BYOK keys, "
            "Dograh-managed bundles and speech-to-speech configs are kept. Use "
            "scripts/assemblyai_migrate.py to switch an explicit org or workflow."
        ),
        "organizations_by_classification": count(organizations, "classification"),
        "workflows_by_classification": count(workflow_entries, "classification"),
        "organizations": organizations,
        "workflows": workflow_entries,
    }
