"""AssemblyAI platform default: call-start selection, budgets, usage, migration."""

from __future__ import annotations

import copy
import os
import uuid

import pytest
from fastapi import HTTPException

from api.db.models import OrganizationModel
from api.enums import OrganizationConfigurationKey
from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.assemblyai import BudgetSettings
from api.services.configuration.registry import (
    AssemblyAISTTConfiguration,
    CartesiaSTTConfiguration,
    DeepgramSTTConfiguration,
    DograhSTTService,
    HopSTTSTTConfiguration,
    ServiceProviders,
)
from api.services.pipecat import assemblyai_rollout as rollout
from api.services.pipecat import assemblyai_usage as usage_mod
from api.services.pipecat.assemblyai_usage import Admission

PLATFORM_DG = "platform-deepgram-key"
MODEL = "assemblyai-stt-3-7-realtime-agent"


def _cfg(stt=None, *, realtime=False):
    return EffectiveAIModelConfiguration(stt=stt, is_realtime=realtime)


def _deepgram(key=PLATFORM_DG, language="en"):
    return DeepgramSTTConfiguration(provider="deepgram", api_key=key, language=language)


@pytest.fixture
def env(monkeypatch):
    for name in (
        "ASSEMBLYAI_STT_ROLLOUT",
        "ASSEMBLYAI_STT_CANARY_ORG_IDS",
        "ASSEMBLYAI_STT_CANARY_WORKFLOW_IDS",
        "ASSEMBLYAI_STT_EXCLUDED_ORG_IDS",
        "ASSEMBLYAI_FALLBACK_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", PLATFORM_DG)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai-platform-key-0000")
    return monkeypatch


@pytest.fixture
def admit_ok(monkeypatch):
    calls = []

    async def fake_admit(member, *, voice_focus, settings=None, redis_client=None):
        calls.append(member)
        return Admission(True, "ok")

    monkeypatch.setattr(rollout, "admit_platform_session", fake_admit)
    return calls


@pytest.fixture
def no_metering(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(rollout, "record_fallback", noop)
    monkeypatch.setattr(rollout, "record_refusal", noop)


# ---------------------------------------------------------- classification


def test_classification_hierarchy(env):
    assert rollout.classify_stt(_cfg(realtime=True)) == rollout.NO_STT_STAGE
    assert rollout.classify_stt(_cfg()) == rollout.NOT_CONFIGURED
    assert (
        rollout.classify_stt(
            _cfg(_deepgram()), {"model_overrides": {"stt": {"provider": "x"}}}
        )
        == rollout.WORKFLOW_OVERRIDE
    )
    assert (
        rollout.classify_stt(
            _cfg(_deepgram()), {"model_configuration_v2_override": {"mode": "byok"}}
        )
        == rollout.WORKFLOW_OVERRIDE
    )
    assert rollout.classify_stt(_cfg(_deepgram())) == rollout.PLATFORM_INHERITED
    assert (
        rollout.classify_stt(_cfg(_deepgram("tenant-own-key"))) == rollout.TENANT_BYOK
    )
    assert (
        rollout.classify_stt(_cfg(_deepgram([PLATFORM_DG, "tenant-own-key"])))
        == rollout.TENANT_BYOK
    )
    assert (
        rollout.classify_stt(
            _cfg(DograhSTTService(provider="dograh", api_key="m", model="default"))
        )
        == rollout.MANAGED
    )
    assert (
        rollout.classify_stt(_cfg(HopSTTSTTConfiguration(provider="hopstt")))
        == rollout.PLATFORM_INHERITED
    )
    assert (
        rollout.classify_stt(_cfg(AssemblyAISTTConfiguration(provider="assemblyai")))
        == rollout.ALREADY_ASSEMBLYAI
    )


def test_product_attribution():
    assert (
        rollout.product_of({"hw_product": "CALLBACK_RECOVERY"}) == "callback_recovery"
    )
    assert rollout.product_of({"product": "Real Estate"}) == "real estate"
    assert rollout.product_of({"hv_session_id": "x"}) == "holiday_voices"
    assert rollout.product_of({}) is None


def test_platform_default_keeps_supported_language():
    assert (
        rollout.platform_default_stt_config(_deepgram(language="es")).language == "es"
    )
    assert (
        rollout.platform_default_stt_config(_deepgram(language="multi")).language
        == "multi"
    )
    assert (
        rollout.platform_default_stt_config(_deepgram(language="hi")).language == "en"
    )
    assert rollout.platform_default_stt_config(None).model == MODEL


# ---------------------------------------------------------------- planning


async def _plan(config, *, org=1, workflow=10, run_configs=None):
    return await rollout.plan_call_stt(
        config,
        organization_id=org,
        workflow_id=workflow,
        workflow_run_id=uuid.uuid4().int % 10**9,
        run_configs=run_configs,
        initial_context={"hw_product": "LEAD_QUALIFIER"},
    )


async def test_rollout_off_changes_nothing(env, admit_ok):
    config = _cfg(_deepgram())
    plan = await _plan(config)
    assert plan.user_config is config
    assert plan.selection == "configured"
    assert admit_ok == []


async def test_canary_moves_only_listed_inherited_calls(env, admit_ok):
    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "canary")
    env.setenv("ASSEMBLYAI_STT_CANARY_ORG_IDS", "1")
    plan = await _plan(_cfg(_deepgram(language="es")), org=1)
    assert plan.selection == "rollout"
    assert plan.user_config.stt.provider == ServiceProviders.ASSEMBLYAI
    assert plan.user_config.stt.model == MODEL
    assert plan.user_config.stt.language == "es"
    assert plan.user_config.stt.api_key is None  # platform key, server-side
    assert plan.usage.product == "lead_qualifier"
    assert plan.usage.credential_source == "platform"

    other = await _plan(_cfg(_deepgram()), org=2)
    assert other.selection == "configured"
    assert other.user_config.stt.provider == ServiceProviders.DEEPGRAM


async def test_canary_by_workflow_id(env, admit_ok):
    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "canary")
    env.setenv("ASSEMBLYAI_STT_CANARY_WORKFLOW_IDS", "77")
    plan = await _plan(_cfg(_deepgram()), org=5, workflow=77)
    assert plan.selection == "rollout"


async def test_global_rollout_respects_explicit_choices(env, admit_ok):
    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "global")
    env.setenv("ASSEMBLYAI_STT_EXCLUDED_ORG_IDS", "9")
    tenant = _cfg(_deepgram("tenant-own-key"))
    assert (await _plan(tenant)).user_config is tenant
    overridden = _cfg(_deepgram())
    plan = await _plan(
        overridden, run_configs={"model_overrides": {"stt": {"provider": "deepgram"}}}
    )
    assert plan.user_config is overridden
    realtime = _cfg(realtime=True)
    assert (await _plan(realtime)).user_config is realtime
    excluded = _cfg(_deepgram())
    assert (await _plan(excluded, org=9)).user_config is excluded
    assert (await _plan(_cfg(_deepgram()), org=3)).selection == "rollout"


async def test_rollout_over_budget_keeps_original_stt(env, monkeypatch, no_metering):
    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "global")

    async def refuse(member, **kwargs):
        return Admission(False, "daily_budget")

    monkeypatch.setattr(rollout, "admit_platform_session", refuse)
    config = _cfg(_deepgram())
    plan = await _plan(config)
    assert plan.user_config is config
    assert "daily_budget" in plan.reason
    assert plan.usage is None


async def test_rollout_without_platform_key_keeps_original(env, admit_ok, no_metering):
    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "global")
    env.delenv("ASSEMBLYAI_API_KEY")
    config = _cfg(_deepgram())
    plan = await _plan(config)
    assert plan.user_config is config
    assert "missing_credential" in plan.reason


async def test_explicit_assemblyai_byok_is_never_budget_limited(env, monkeypatch):
    async def must_not_admit(*args, **kwargs):
        raise AssertionError("BYOK sessions are not admitted against platform budgets")

    monkeypatch.setattr(rollout, "admit_platform_session", must_not_admit)
    config = _cfg(AssemblyAISTTConfiguration(provider="assemblyai", api_key="tenant"))
    plan = await _plan(config)
    assert plan.user_config is config
    assert plan.usage.credential_source == "byok"


async def test_explicit_assemblyai_without_key_is_refused_clearly(env, no_metering):
    env.delenv("ASSEMBLYAI_API_KEY")
    with pytest.raises(HTTPException) as exc:
        await _plan(_cfg(AssemblyAISTTConfiguration(provider="assemblyai")))
    assert exc.value.status_code == 503
    assert "ASSEMBLYAI_API_KEY" in exc.value.detail


async def test_explicit_assemblyai_falls_back_only_when_authorized(
    env, monkeypatch, no_metering
):
    env.delenv("ASSEMBLYAI_API_KEY")
    config = _cfg(AssemblyAISTTConfiguration(provider="assemblyai"))
    fallback = CartesiaSTTConfiguration(provider="cartesia", api_key="org-cartesia")

    async def find(provider, user_config, organization_id):
        return fallback if provider == "cartesia" else None

    monkeypatch.setattr(rollout, "_find_fallback_stt", find)
    env.setenv("ASSEMBLYAI_FALLBACK_PROVIDER", "deepgram")  # org has no deepgram creds
    with pytest.raises(HTTPException):
        await _plan(config)
    env.setenv("ASSEMBLYAI_FALLBACK_PROVIDER", "cartesia")
    plan = await _plan(config)
    assert plan.selection == "fallback"
    assert plan.user_config.stt is fallback


# --------------------------------------------------------- budgets (Redis)

requires_redis = pytest.mark.skipif(
    "REDIS_URL" not in os.environ, reason="Requires Redis (set REDIS_URL via .env.test)"
)


@pytest.fixture
async def isolated_redis(monkeypatch):
    import redis.asyncio as aioredis

    client = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    prefix = f"test-aai-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(usage_mod, "PREFIX", prefix)
    monkeypatch.setattr(usage_mod, "ACTIVE_KEY", f"{prefix}:active")
    monkeypatch.setattr(usage_mod, "RESERVED_KEY", f"{prefix}:reserved")
    monkeypatch.setattr(usage_mod, "TOTAL_USD_KEY", f"{prefix}:usd:total")
    monkeypatch.setattr(usage_mod, "TOTAL_SECONDS_KEY", f"{prefix}:seconds:total")
    yield client
    keys = [k async for k in client.scan_iter(f"{prefix}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


def _budget(**kw):
    base = dict(
        daily_budget_usd=5.0,
        total_budget_usd=25.0,
        max_concurrent_streams=2,
        reserve_minutes_per_call=5.0,
    )
    base.update(kw)
    return BudgetSettings(**base)


@requires_redis
async def test_stream_cap_and_release(isolated_redis):
    settings = _budget()
    admit = usage_mod.admit_platform_session
    assert (
        await admit(
            "run:1", voice_focus=False, settings=settings, redis_client=isolated_redis
        )
    ).admitted
    assert (
        await admit(
            "run:2", voice_focus=False, settings=settings, redis_client=isolated_redis
        )
    ).admitted
    refused = await admit(
        "run:3", voice_focus=False, settings=settings, redis_client=isolated_redis
    )
    assert refused == Admission(False, "concurrency")
    # Re-admitting a member that already holds a slot is idempotent.
    assert (
        await admit(
            "run:1", voice_focus=False, settings=settings, redis_client=isolated_redis
        )
    ).admitted
    await usage_mod.release_platform_session("run:1", redis_client=isolated_redis)
    assert (
        await admit(
            "run:3", voice_focus=False, settings=settings, redis_client=isolated_redis
        )
    ).admitted


@requires_redis
async def test_daily_and_total_budgets_count_reservations(isolated_redis):
    # 5 min reserved per call at $0.45/h = $0.0375; a $0.05 day fits one call.
    settings = _budget(daily_budget_usd=0.05, max_concurrent_streams=10)
    admit = usage_mod.admit_platform_session
    assert (
        await admit(
            "run:a", voice_focus=False, settings=settings, redis_client=isolated_redis
        )
    ).admitted
    second = await admit(
        "run:b", voice_focus=False, settings=settings, redis_client=isolated_redis
    )
    assert second.reason == "daily_budget"

    settings = _budget(total_budget_usd=0.0, max_concurrent_streams=10)
    third = await admit(
        "run:c", voice_focus=False, settings=settings, redis_client=isolated_redis
    )
    assert third.reason == "total_budget"


@requires_redis
async def test_usage_is_metered_by_org_workflow_and_product(isolated_redis):
    session = usage_mod.AssemblyAIUsageSession(
        member="run:42",
        organization_id=7,
        workflow_id=70,
        product="callback_recovery",
        credential_source="platform",
        voice_focus=False,
        model=MODEL,
        redis_client=isolated_redis,
    )
    await usage_mod.admit_platform_session(
        "run:42", voice_focus=False, settings=_budget(), redis_client=isolated_redis
    )
    session.connection_opened()
    session.add_billed_seconds(120.0)
    session.record_error(3005)
    await session.finish()
    await session.finish()  # idempotent

    snap = await usage_mod.usage_snapshot(days=1, redis_client=isolated_redis)
    today = snap["days"][0]
    assert today["minutes"] == 2.0
    assert today["estimated_usd"] == pytest.approx(0.015)
    assert today["sessions"] == 1
    assert today["error_rate"] == 1.0
    assert today["minutes_by_org"] == {"7": 2.0}
    assert today["minutes_by_workflow"] == {"70": 2.0}
    assert today["minutes_by_product"] == {"callback_recovery": 2.0}
    assert snap["active_platform_streams"] == 0  # slot released
    assert "not AssemblyAI's account balance" in snap["note"]


@requires_redis
async def test_no_limits_by_default_admits_everything(isolated_redis):
    unlimited = BudgetSettings(
        daily_budget_usd=None,
        total_budget_usd=None,
        max_concurrent_streams=None,
        reserve_minutes_per_call=5.0,
    )
    for i in range(50):
        result = await usage_mod.admit_platform_session(
            f"run:u{i}",
            voice_focus=True,
            settings=unlimited,
            redis_client=isolated_redis,
        )
        assert result.admitted
    # Streams are still counted for the status view.
    assert await isolated_redis.zcard(usage_mod.ACTIVE_KEY) == 50


async def test_no_limits_never_refuses_even_without_redis():
    class Broken:
        async def eval(self, *args):
            raise ConnectionError("down")

    unlimited = BudgetSettings(
        daily_budget_usd=None,
        total_budget_usd=None,
        max_concurrent_streams=None,
        reserve_minutes_per_call=5.0,
    )
    result = await usage_mod.admit_platform_session(
        "run:y", voice_focus=False, settings=unlimited, redis_client=Broken()
    )
    assert result.admitted


async def test_admission_fails_closed_without_redis():
    class Broken:
        async def eval(self, *args):
            raise ConnectionError("down")

    result = await usage_mod.admit_platform_session(
        "run:x", voice_focus=False, settings=_budget(), redis_client=Broken()
    )
    assert result == Admission(False, "budget_store_unavailable")


# ---------------------------------------------------------- migration (DB)

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
            "stt": {
                "provider": "deepgram",
                "api_key": PLATFORM_DG,
                "model": "nova-3",
                "language": "es",
            },
        },
    },
}


@pytest.fixture
async def org(async_session):
    org = OrganizationModel(provider_id=f"test-org-aai-{uuid.uuid4().hex[:6]}")
    async_session.add(org)
    await async_session.flush()
    return org


def test_migration_block_has_no_key_and_keeps_language():
    from api.services.configuration.assemblyai_migration import (
        build_assemblyai_stt_block,
    )

    block = build_assemblyai_stt_block(
        {"provider": "deepgram", "language": "es", "api_key": "k"}
    )
    assert block["provider"] == "assemblyai" and block["model"] == MODEL
    assert block["language"] == "es" and "api_key" not in block


async def test_org_migration_dry_run_apply_rollback(db_session, org, env):
    from api.services.configuration.assemblyai_migration import (
        assemblyai_backup_document,
        plan_assemblyai_rollback,
        plan_assemblyai_switch,
    )
    from api.services.configuration.hopstt_migration import apply_plan

    key = OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    await db_session.upsert_configuration(org.id, key, copy.deepcopy(BYOK))
    plan = await plan_assemblyai_switch(organization_id=org.id)
    assert (await db_session.get_configuration(org.id, key)).value == BYOK  # dry run

    backup = assemblyai_backup_document(plan)
    await apply_plan(plan)
    stored = (await db_session.get_configuration(org.id, key)).value
    assert stored["byok"]["pipeline"]["stt"]["provider"] == "assemblyai"
    assert stored["byok"]["pipeline"]["tts"] == BYOK["byok"]["pipeline"]["tts"]

    await apply_plan(await plan_assemblyai_rollback(backup))
    assert (await db_session.get_configuration(org.id, key)).value == BYOK


async def test_org_migration_refuses_tenant_byok_unless_included(db_session, org, env):
    from api.services.configuration.assemblyai_migration import (
        AssemblyAIMigrationError,
        plan_assemblyai_switch,
    )

    tenant = copy.deepcopy(BYOK)
    tenant["byok"]["pipeline"]["stt"]["api_key"] = "tenant-own-deepgram-key"
    key = OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    await db_session.upsert_configuration(org.id, key, tenant)
    with pytest.raises(AssemblyAIMigrationError, match="BYOK"):
        await plan_assemblyai_switch(organization_id=org.id)
    plan = await plan_assemblyai_switch(organization_id=org.id, include_byok=True)
    assert plan.changes[0].after["byok"]["pipeline"]["stt"]["provider"] == "assemblyai"


async def test_rollout_report_classifies_orgs_without_writing(db_session, org, env):
    from api.services.configuration.assemblyai_admin import rollout_report

    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "canary")
    env.setenv("ASSEMBLYAI_STT_CANARY_ORG_IDS", str(org.id))
    key = OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    await db_session.upsert_configuration(org.id, key, copy.deepcopy(BYOK))
    report = await rollout_report()
    entry = next(o for o in report["organizations"] if o["organization_id"] == org.id)
    assert entry["classification"] == rollout.PLATFORM_INHERITED
    assert entry["current_stt"]["provider"] == "deepgram"
    assert entry["moves_under_current_rollout"] is True
    assert "api_key" not in str(entry)
    assert (await db_session.get_configuration(org.id, key)).value == BYOK


async def test_global_rollout_moves_managed_stt_only(env, admit_ok):
    """A Dograh-managed config keeps its managed LLM/TTS; only STT moves."""
    env.setenv("ASSEMBLYAI_STT_ROLLOUT", "global")
    from api.services.configuration.registry import DograhLLMService, DograhTTSService

    managed = EffectiveAIModelConfiguration(
        llm=DograhLLMService(provider="dograh", api_key="m", model="default"),
        tts=DograhTTSService(provider="dograh", api_key="m", model="default"),
        stt=DograhSTTService(
            provider="dograh", api_key="m", model="default", language="multi"
        ),
        managed_service_version=2,
    )
    plan = await _plan(managed)
    assert plan.selection == "rollout"
    assert plan.classification == rollout.MANAGED
    assert plan.user_config.stt.provider == ServiceProviders.ASSEMBLYAI
    assert plan.user_config.stt.model == MODEL
    assert plan.user_config.stt.language == "multi"
    assert plan.user_config.llm == managed.llm
    assert plan.user_config.tts == managed.tts
    assert plan.user_config.managed_service_version == 2


async def test_managed_stays_when_rollout_off(env, admit_ok):
    managed = _cfg(DograhSTTService(provider="dograh", api_key="m", model="default"))
    assert (await _plan(managed)).user_config is managed
