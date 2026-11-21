"""Controlled, reversible switch of ONE organization's or ONE workflow's STT to
AssemblyAI ``assemblyai-stt-3-7-realtime-agent``, for configurations the
platform rollout deliberately leaves alone (explicit choices).

It reuses the HopSTT migration core (dry run, validated configs, backup file,
exact rollback). The new STT block carries no API key, so calls use the
server's ``ASSEMBLYAI_API_KEY``. A tenant's own (BYOK) STT key is never
replaced unless the operator passes ``include_byok=True``.

Calls already in progress are unaffected: a run reads its STT once at call
start and pins its workflow definition.
"""

from __future__ import annotations

from api.schemas.ai_model_configuration import (
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.assemblyai import (
    ASSEMBLYAI_DEFAULT_MODEL,
    ASSEMBLYAI_PROVIDER,
    ASSEMBLYAI_STT_LANGUAGES,
)
from api.services.configuration.hopstt_migration import (
    HopSTTMigrationError,
    MigrationPlan,
    _org_config,
    backup_document,
    plan_provider_switch,
    plan_rollback,
)
from api.services.configuration.registry import AssemblyAISTTConfiguration

BACKUP_KIND = "assemblyai_migration_backup"


class AssemblyAIMigrationError(HopSTTMigrationError):
    pass


def build_assemblyai_stt_block(
    previous_stt: dict | None,
    *,
    model: str | None = None,
    language: str | None = None,
) -> dict:
    """An AssemblyAI STT block that keeps a supported previous language."""
    prev_language = (previous_stt or {}).get("language")
    if not language:
        if prev_language in ASSEMBLYAI_STT_LANGUAGES:
            language = prev_language
        elif prev_language in ("auto", "multi", None):
            language = "multi"
        else:
            language = "en"
    block = {
        "provider": ASSEMBLYAI_PROVIDER,
        "model": model or ASSEMBLYAI_DEFAULT_MODEL,
        "language": language,
    }
    return AssemblyAISTTConfiguration.model_validate(block).model_dump(
        mode="json", exclude_none=True
    )


async def _refuse_tenant_byok(organization_id: int) -> None:
    from api.services.pipecat.assemblyai_rollout import TENANT_BYOK, classify_stt

    config = OrganizationAIModelConfigurationV2.model_validate(
        await _org_config(organization_id)
    )
    if classify_stt(compile_ai_model_configuration_v2(config)) == TENANT_BYOK:
        raise AssemblyAIMigrationError(
            f"organization {organization_id} uses its own STT API key (BYOK). "
            "Switching it would replace the tenant's provider choice; pass "
            "--include-byok only with the tenant's agreement."
        )


async def plan_assemblyai_switch(
    *,
    organization_id: int,
    workflow_id: int | None = None,
    model: str | None = None,
    language: str | None = None,
    include_byok: bool = False,
) -> MigrationPlan:
    if workflow_id is None and not include_byok:
        await _refuse_tenant_byok(organization_id)
    return await plan_provider_switch(
        organization_id=organization_id,
        workflow_id=workflow_id,
        provider=ASSEMBLYAI_PROVIDER,
        build_stt_block=lambda prior: build_assemblyai_stt_block(
            prior, model=model, language=language
        ),
    )


async def plan_assemblyai_rollback(
    backup: dict, *, force: bool = False
) -> MigrationPlan:
    if backup.get("kind") != BACKUP_KIND:
        raise AssemblyAIMigrationError("not an AssemblyAI migration backup file")
    return await plan_rollback(backup, force=force, provider=ASSEMBLYAI_PROVIDER)


def assemblyai_backup_document(plan: MigrationPlan) -> dict:
    return backup_document(plan, kind=BACKUP_KIND)
