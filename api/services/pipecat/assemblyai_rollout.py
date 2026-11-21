"""Call-start STT selection for the AssemblyAI platform default.

Selection hierarchy for one call (nothing here runs mid-call):

  1. The workflow's own STT (``model_overrides.stt`` or a v2 model
     configuration override) is used as is.
  2. The organization's configured STT is used as is, *unless* it was inherited
     from the platform (see :func:`classify_stt`) and the rollout
     (``ASSEMBLYAI_STT_ROLLOUT`` = canary/global) covers this call. Then the
     platform default, AssemblyAI ``assemblyai-stt-3-7-realtime-agent``, is used.
  3. A configuration with no STT stage (speech-to-speech realtime models) is
     left alone: there is nothing to replace.

When AssemblyAI is chosen but cannot be used at call start (no credential,
application budget or stream cap reached):

  * a call moved by the *rollout* simply keeps its original STT (no extra spend);
  * a call whose own config names AssemblyAI uses ``ASSEMBLYAI_FALLBACK_PROVIDER``
    if set and the org has credentials for it, else it is refused with a clear
    error. Nothing silently switches to a provider without a key.

Every decision is logged with its reason and counted in the usage store, and
``runtime_configuration`` records the provider actually used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from fastapi import HTTPException
from loguru import logger

from api.services.configuration.ai_model_configuration import (
    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
)
from api.services.configuration.assemblyai import (
    ASSEMBLYAI_DEFAULT_MODEL,
    ASSEMBLYAI_PROVIDER,
    MISSING_CREDENTIAL_MESSAGE,
    RolloutSettings,
    config_api_keys,
    in_rollout,
    resolve_assemblyai_api_key,
    rollout_settings,
)
from api.services.configuration.registry import (
    REGISTRY,
    AssemblyAISTTConfiguration,
    ServiceProviders,
    ServiceType,
)
from api.services.pipecat.assemblyai_usage import (
    AssemblyAIUsageSession,
    admit_platform_session,
    record_fallback,
    record_refusal,
)

# Classification of where an STT choice came from.
WORKFLOW_OVERRIDE = "workflow_override"  # explicit, never replaced
TENANT_BYOK = "tenant_byok"  # tenant's own key, never replaced
MANAGED = "dograh_managed"  # Dograh-managed bundle chosen by the org
PLATFORM_INHERITED = "platform_inherited"  # platform-provisioned credential
ALREADY_ASSEMBLYAI = "assemblyai"
NO_STT_STAGE = "no_stt_stage"  # speech-to-speech realtime
NOT_CONFIGURED = "not_configured"

ROLLOUT_ELIGIBLE = {PLATFORM_INHERITED, NOT_CONFIGURED}

# Env vars the platform uses to provision each provider's shared key. An org
# STT whose every key equals the platform's key was inherited, not chosen.
_PLATFORM_KEY_ENV = {
    "deepgram": "DEEPGRAM_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "speechmatics": "SPEECHMATICS_API_KEY",
    "gladia": "GLADIA_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "openai": "OPENAI_API_KEY",
    "hopstt": "HOPSTT_API_KEY",
}


def _excluded_org_ids() -> frozenset[int]:
    raw = os.getenv("ASSEMBLYAI_STT_EXCLUDED_ORG_IDS", "")
    return frozenset(int(p) for p in raw.split(",") if p.strip().isdigit())


def workflow_overrides_stt(run_configs: dict | None) -> bool:
    run_configs = run_configs or {}
    if run_configs.get(WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY):
        return True
    overrides = run_configs.get("model_overrides")
    return isinstance(overrides, dict) and bool(overrides.get("stt"))


def classify_stt(user_config, run_configs: dict | None = None) -> str:
    """Where this call's STT choice came from (see module docstring)."""
    if getattr(user_config, "is_realtime", False):
        return NO_STT_STAGE
    if workflow_overrides_stt(run_configs):
        return WORKFLOW_OVERRIDE
    stt = getattr(user_config, "stt", None)
    if stt is None:
        return NOT_CONFIGURED
    provider = getattr(stt, "provider", None)
    provider = getattr(provider, "value", provider)
    if provider == ASSEMBLYAI_PROVIDER:
        return ALREADY_ASSEMBLYAI
    if provider == ServiceProviders.DOGRAH.value:
        return MANAGED
    keys = config_api_keys(stt)
    env_name = _PLATFORM_KEY_ENV.get(provider or "")
    platform_key = (os.getenv(env_name, "").strip() if env_name else "") or None
    if provider == ServiceProviders.HOPSTT.value and not keys:
        return PLATFORM_INHERITED  # runs on the runtime's HOPSTT_API_KEY
    if platform_key and keys and all(k == platform_key for k in keys):
        return PLATFORM_INHERITED
    return TENANT_BYOK


def product_of(initial_context: dict | None) -> str | None:
    """Product label for usage metering, from what the caller app sends."""
    ctx = initial_context or {}
    for key in ("product", "hw_product"):
        value = ctx.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:64]
    if any(str(k).startswith("hv_session") for k in ctx):
        return "holiday_voices"
    return None


def platform_default_stt_config(previous_stt=None) -> AssemblyAISTTConfiguration:
    """The platform default STT block, keeping a compatible previous language."""
    language = getattr(previous_stt, "language", None)
    allowed = AssemblyAISTTConfiguration.model_fields["language"].json_schema_extra[
        "examples"
    ]
    if language not in allowed:
        language = "multi" if language in {"multi", "auto", None} else "en"
    return AssemblyAISTTConfiguration(
        provider=ServiceProviders.ASSEMBLYAI,
        model=ASSEMBLYAI_DEFAULT_MODEL,
        language=language,
    )


@dataclass
class STTCallPlan:
    user_config: object
    classification: str
    selection: str  # "configured" | "rollout" | "fallback"
    reason: str
    usage: AssemblyAIUsageSession | None = None
    notes: list[str] = field(default_factory=list)


async def _find_fallback_stt(provider: str, user_config, organization_id):
    if not provider or provider == ASSEMBLYAI_PROVIDER:
        return None
    if REGISTRY[ServiceType.STT].get(provider) is None:
        return None
    # Same credential lookup the HopSTT fallback uses: the org's own config.
    from api.services.pipecat.hopstt_fallback import _find_fallback_stt_config

    return await _find_fallback_stt_config(provider, user_config, organization_id)


async def _admit(
    stt_config, *, member: str, organization_id, workflow_id, product
) -> tuple[AssemblyAIUsageSession | None, str]:
    """Credential + budget check. Returns (usage session or None, reason)."""
    key, source = resolve_assemblyai_api_key(stt_config)
    if source == "missing":
        return None, "missing_credential"
    voice_focus = getattr(stt_config, "voice_focus", "off") not in (None, "off")
    if source == "platform":
        admission = await admit_platform_session(member, voice_focus=voice_focus)
        if not admission.admitted:
            await record_refusal(admission.reason)
            return None, admission.reason
    return (
        AssemblyAIUsageSession(
            member=member,
            organization_id=organization_id,
            workflow_id=workflow_id,
            product=product,
            credential_source=source,
            voice_focus=voice_focus,
            model=getattr(stt_config, "model", ASSEMBLYAI_DEFAULT_MODEL),
        ),
        "ok",
    )


async def plan_call_stt(
    user_config,
    *,
    organization_id: int | None,
    workflow_id: int | None,
    workflow_run_id: int,
    run_configs: dict | None,
    initial_context: dict | None = None,
    settings: RolloutSettings | None = None,
) -> STTCallPlan:
    """Decide this call's STT once, at call start."""
    settings = settings or rollout_settings()
    classification = classify_stt(user_config, run_configs)
    product = product_of(initial_context)
    member = f"run:{workflow_run_id}"
    common = dict(
        member=member,
        organization_id=organization_id,
        workflow_id=workflow_id,
        product=product,
    )

    if classification == NO_STT_STAGE:
        return STTCallPlan(
            user_config, classification, "configured", "speech-to-speech"
        )

    if classification == ALREADY_ASSEMBLYAI:
        usage, reason = await _admit(user_config.stt, **common)
        if usage is not None:
            return STTCallPlan(user_config, classification, "configured", "ok", usage)
        fallback = await _find_fallback_stt(
            settings.fallback_provider, user_config, organization_id
        )
        if fallback is not None:
            await record_fallback(reason, organization_id=organization_id)
            return STTCallPlan(
                user_config.model_copy(update={"stt": fallback}),
                classification,
                "fallback",
                f"assemblyai unavailable ({reason}); using {settings.fallback_provider}",
            )
        await record_fallback(f"refused:{reason}", organization_id=organization_id)
        if reason == "missing_credential":
            raise HTTPException(status_code=503, detail=MISSING_CREDENTIAL_MESSAGE)
        raise HTTPException(
            status_code=503,
            detail=(
                f"AssemblyAI session refused ({reason}): the application-side "
                "testing budget or stream limit is reached and no fallback STT "
                "is configured (ASSEMBLYAI_FALLBACK_PROVIDER). Raise the budget "
                "or configure a fallback."
            ),
        )

    if (
        classification in ROLLOUT_ELIGIBLE
        and organization_id not in _excluded_org_ids()
        and in_rollout(
            settings, organization_id=organization_id, workflow_id=workflow_id
        )
    ):
        candidate = platform_default_stt_config(getattr(user_config, "stt", None))
        usage, reason = await _admit(candidate, **common)
        if usage is not None:
            logger.info(
                f"[run {workflow_run_id}] STT platform default applied: "
                f"assemblyai/{candidate.model} (rollout={settings.mode}, "
                f"was {classification})"
            )
            return STTCallPlan(
                user_config.model_copy(update={"stt": candidate}),
                classification,
                "rollout",
                f"rollout={settings.mode}",
                usage,
            )
        if getattr(user_config, "stt", None) is None:
            raise HTTPException(status_code=503, detail=MISSING_CREDENTIAL_MESSAGE)
        await record_fallback(reason, organization_id=organization_id)
        return STTCallPlan(
            user_config,
            classification,
            "configured",
            f"rollout skipped ({reason}); kept configured STT",
        )

    return STTCallPlan(user_config, classification, "configured", "configured")
