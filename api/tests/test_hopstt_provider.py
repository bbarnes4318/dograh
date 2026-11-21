"""HopSTT provider wiring: registry/schema, gate, validator, factory, masking,
backward compatibility of existing configs, and the paid fallback."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from api.routes.user import get_default_configurations
from api.schemas.ai_model_configuration import (
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration import check_validity
from api.services.configuration.ai_model_configuration import (
    mask_ai_model_configuration_v2,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    REGISTRY,
    CartesiaSTTConfiguration,
    HopSTTSTTConfiguration,
    ServiceProviders,
    ServiceType,
    STTConfig,
)
from api.services.pipecat import hopstt_fallback, service_factory
from api.services.pipecat.hopstt_stt import HopSTTService, hopstt_session_id

CARTESIA_ORG_CONFIG = {
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
                "provider": "cartesia",
                "api_key": "cartesia-key-abcd",
                "model": "ink-whisper",
                "language": "en",
            },
        },
    },
}

DOGRAH_ORG_CONFIG = {
    "version": 2,
    "mode": "dograh",
    "dograh": {
        "api_key": "dgr-key-1234",
        "voice": "default",
        "speed": 1.0,
        "language": "multi",
    },
}


def _hopstt(**overrides) -> HopSTTSTTConfiguration:
    values = {"provider": "hopstt", "model": "nemotron-3.5-streaming", "language": "en"}
    values.update(overrides)
    return HopSTTSTTConfiguration.model_validate(values)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "HOPSTT_ENABLED",
        "HOPSTT_DEFAULT_ENDPOINT",
        "HOPSTT_API_KEY",
        "HOPSTT_PAID_FALLBACK_ENABLED",
        "HOPSTT_PAID_FALLBACK_PROVIDER",
        "HOPSTT_PAID_FALLBACK_MAX_MINUTES_PER_DAY",
        "HOPSTT_PAID_FALLBACK_RESERVE_MINUTES_PER_CALL",
    ):
        monkeypatch.delenv(name, raising=False)
    hopstt_fallback._local_minutes.clear()
    hopstt_fallback._local_counts.clear()


# ----------------------------------------------------------------- registry


def test_registered_in_stt_union_with_defaults():
    config = TypeAdapter(STTConfig).validate_python({"provider": "hopstt"})
    assert isinstance(config, HopSTTSTTConfiguration)
    assert ServiceProviders.HOPSTT.value == "hopstt"
    assert REGISTRY[ServiceType.STT]["hopstt"] is HopSTTSTTConfiguration
    assert config.model == "nemotron-3.5-streaming"
    assert config.language == "en"
    assert config.endpoint == "http://hopstt-live:8790"
    assert config.api_key is None
    assert config.chunk_ms == 320
    assert config.connect_timeout_s == 2.0
    assert config.recovery_deadline_s == 5.0
    assert config.max_reconnect_attempts == 6
    assert config.ttfs_p99_latency == 0.5


def test_schema_marks_advanced_fields_and_offers_models():
    schema = HopSTTSTTConfiguration.model_json_schema()
    props = schema["properties"]
    advanced = {name for name, p in props.items() if p.get("advanced")}
    assert advanced == {
        "chunk_ms",
        "connect_timeout_s",
        "recovery_deadline_s",
        "max_reconnect_attempts",
        "ttfs_p99_latency",
    }
    assert props["model"]["examples"] == [
        "nemotron-3.5-streaming",
        "nemotron-en-streaming",
        "voxtral-realtime",
    ]
    assert props["model"]["allow_custom_input"] is True
    assert props["language"]["model_options"]["nemotron-en-streaming"] == ["en"]
    assert "auto" in props["language"]["examples"]
    assert props["chunk_ms"]["examples"] == [80, 160, 320, 560, 1120]
    assert "api_key" not in schema.get("required", [])


@pytest.mark.asyncio
async def test_hopstt_listed_in_ui_defaults():
    defaults = await get_default_configurations()
    assert "hopstt" in defaults.stt
    assert defaults.stt["hopstt"]["title"] == "HopSTT"


@pytest.mark.parametrize("chunk", [0, 100, 321])
def test_invalid_chunk_ms_rejected(chunk):
    with pytest.raises(ValidationError):
        _hopstt(chunk_ms=chunk)


def test_blank_endpoint_rejected():
    with pytest.raises(ValidationError):
        _hopstt(endpoint=" , ")


def test_api_key_masked_in_v2_config():
    org = copy.deepcopy(CARTESIA_ORG_CONFIG)
    org["byok"]["pipeline"]["stt"] = {
        "provider": "hopstt",
        "api_key": "hopstt-secret-9876",
    }
    parsed = OrganizationAIModelConfigurationV2.model_validate(org)
    masked = mask_ai_model_configuration_v2(parsed)
    key = masked["byok"]["pipeline"]["stt"]["api_key"]
    assert "hopstt-secret" not in str(key)
    assert str(key).endswith("9876")


# --------------------------------------------------------- backward compat


def test_existing_cartesia_org_config_still_validates_unchanged():
    parsed = OrganizationAIModelConfigurationV2.model_validate(CARTESIA_ORG_CONFIG)
    stt = parsed.byok.pipeline.stt
    assert isinstance(stt, CartesiaSTTConfiguration)
    dumped = parsed.model_dump(mode="json", exclude_none=True)
    # LLM/TTS gain their own schema defaults on dump (pre-existing behaviour);
    # the STT block round-trips byte for byte.
    assert (
        dumped["byok"]["pipeline"]["stt"]
        == CARTESIA_ORG_CONFIG["byok"]["pipeline"]["stt"]
    )
    assert (
        compile_ai_model_configuration_v2(parsed).stt.provider
        == ServiceProviders.CARTESIA
    )


def test_existing_dograh_mode_config_still_validates_unchanged():
    parsed = OrganizationAIModelConfigurationV2.model_validate(DOGRAH_ORG_CONFIG)
    assert parsed.model_dump(mode="json", exclude_none=True) == DOGRAH_ORG_CONFIG
    assert (
        compile_ai_model_configuration_v2(parsed).stt.provider
        == ServiceProviders.DOGRAH
    )


def test_external_turns_stay_off_for_hopstt():
    assert (
        service_factory.stt_uses_external_turns(SimpleNamespace(stt=_hopstt())) is False
    )


# --------------------------------------------------------------- gate/validator


def _no_url_check(monkeypatch):
    monkeypatch.setattr(
        check_validity, "validate_user_configured_service_url", lambda *a, **k: None
    )


def test_validation_rejects_disabled_runtime(monkeypatch):
    _no_url_check(monkeypatch)
    errors = UserConfigurationValidator()._validate_service(_hopstt(), "stt")
    assert len(errors) == 1
    assert "HOPSTT_ENABLED=true" in errors[0]["message"]


def test_validation_passes_without_api_key_when_enabled(monkeypatch):
    monkeypatch.setenv("HOPSTT_ENABLED", "true")
    _no_url_check(monkeypatch)
    assert UserConfigurationValidator()._validate_service(_hopstt(), "stt") == []


def test_validation_checks_every_endpoint(monkeypatch):
    monkeypatch.setenv("HOPSTT_ENABLED", "true")
    seen = []

    def check(url, *, field_name):
        seen.append(url)
        if "bad" in url:
            raise ValueError(
                "endpoint must resolve to a public IP address in SaaS mode"
            )

    monkeypatch.setattr(check_validity, "validate_user_configured_service_url", check)
    errors = UserConfigurationValidator()._validate_service(
        _hopstt(endpoint="http://good:8790, http://bad:8790"), "stt"
    )
    assert seen == ["http://good:8790", "http://bad:8790"]
    assert errors == [
        {
            "model": "stt",
            "message": "endpoint must resolve to a public IP address in SaaS mode",
        }
    ]


# ------------------------------------------------------------------ factory


def test_factory_rejects_disabled_runtime():
    with pytest.raises(HTTPException) as exc:
        service_factory.create_stt_service(
            SimpleNamespace(stt=_hopstt()),
            SimpleNamespace(transport_in_sample_rate=8000),
        )
    assert exc.value.status_code == 503
    assert "HOPSTT_ENABLED=true" in exc.value.detail


def test_factory_builds_service_with_config(monkeypatch):
    monkeypatch.setenv("HOPSTT_ENABLED", "true")
    checked = []
    monkeypatch.setattr(
        service_factory,
        "_validate_runtime_service_url",
        lambda url, field: checked.append(url),
    )
    cfg = _hopstt(
        endpoint="http://10.0.0.5:8790,http://10.0.0.6:8790",
        api_key="k-1",
        language="es",
        chunk_ms=560,
        connect_timeout_s=1.5,
        recovery_deadline_s=7,
        max_reconnect_attempts=4,
        ttfs_p99_latency=0.6,
    )
    stt = service_factory.create_stt_service(
        SimpleNamespace(stt=cfg),
        SimpleNamespace(transport_in_sample_rate=8000),
        keyterms=["Medicare"],
        correlation_id="corr-1",
    )
    assert isinstance(stt, HopSTTService)
    assert checked == ["http://10.0.0.5:8790", "http://10.0.0.6:8790"]
    assert stt._endpoints == ["http://10.0.0.5:8790", "http://10.0.0.6:8790"]
    assert stt._api_key == "k-1"
    assert stt._init_sample_rate == 8000
    assert stt._chunk_ms == 560
    assert stt._connect_timeout_s == 1.5
    assert stt._recovery_deadline_s == 7
    assert stt._max_reconnect_attempts == 4
    assert stt._ttfs_p99_latency == 0.6
    assert stt._keyterms == ["Medicare"]
    assert stt._session_id == hopstt_session_id("corr-1")
    assert stt._settings.model == "nemotron-3.5-streaming"
    msg = stt._config_message(0, 0)
    assert msg["language"] == "es" and msg["chunk_ms"] == 560


def test_factory_uses_runtime_defaults_for_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("HOPSTT_ENABLED", "true")
    monkeypatch.setenv(
        "HOPSTT_DEFAULT_ENDPOINT", "http://gpu-1:8790, http://gpu-2:8790"
    )
    monkeypatch.setenv("HOPSTT_API_KEY", "env-key")
    monkeypatch.setattr(
        service_factory, "_validate_runtime_service_url", lambda *a: None
    )
    stt = service_factory.create_stt_service(
        SimpleNamespace(stt=_hopstt()), SimpleNamespace(transport_in_sample_rate=16000)
    )
    assert stt._endpoints == ["http://gpu-1:8790", "http://gpu-2:8790"]
    assert stt._api_key == "env-key"
    assert stt._init_sample_rate == 16000
    # An explicitly configured endpoint wins over the runtime default.
    stt = service_factory.create_stt_service(
        SimpleNamespace(stt=_hopstt(endpoint="http://own:8790")),
        SimpleNamespace(transport_in_sample_rate=8000),
    )
    assert stt._endpoints == ["http://own:8790"]


# ------------------------------------------------------------ paid fallback


def _user_config(stt=None, tts=None):
    return compile_ai_model_configuration_v2(
        OrganizationAIModelConfigurationV2.model_validate(CARTESIA_ORG_CONFIG)
    ).model_copy(update={"stt": stt or _hopstt(endpoint="http://127.0.0.1:9")})


class FakeRedis:
    def __init__(self):
        self.values: dict[str, float] = {}

    async def eval(self, _script, _n, key, minutes, max_minutes, _ttl):
        cur = self.values.get(key, 0.0)
        if cur + float(minutes) > float(max_minutes):
            return "-1"
        self.values[key] = cur + float(minutes)
        return str(self.values[key])

    async def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1

    async def expire(self, key, ttl):
        pass


def _enable_fallback(monkeypatch, max_minutes="10"):
    monkeypatch.setenv("HOPSTT_PAID_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("HOPSTT_PAID_FALLBACK_PROVIDER", "cartesia")
    monkeypatch.setenv("HOPSTT_PAID_FALLBACK_MAX_MINUTES_PER_DAY", max_minutes)


@pytest.mark.asyncio
async def test_fallback_disabled_by_default_does_no_health_check(monkeypatch):
    called = []

    async def never(*a, **k):
        called.append(1)
        return False, "x"

    monkeypatch.setattr(hopstt_fallback, "any_endpoint_ready", never)
    cfg = _user_config()
    assert (
        await hopstt_fallback.apply_hopstt_paid_fallback(cfg, organization_id=None)
        is cfg
    )
    _enable_fallback(monkeypatch, max_minutes="0")  # enabled but no budget
    assert (
        await hopstt_fallback.apply_hopstt_paid_fallback(cfg, organization_id=None)
        is cfg
    )
    assert called == []


@pytest.mark.asyncio
async def test_fallback_not_used_when_any_endpoint_ready(monkeypatch):
    _enable_fallback(monkeypatch)

    async def ready(*a, **k):
        return True, "ready"

    monkeypatch.setattr(hopstt_fallback, "any_endpoint_ready", ready)
    cfg = _user_config()
    assert (
        await hopstt_fallback.apply_hopstt_paid_fallback(cfg, organization_id=None)
        is cfg
    )


@pytest.mark.asyncio
async def test_fallback_uses_org_credentials_within_budget(monkeypatch):
    _enable_fallback(monkeypatch, max_minutes="10")
    monkeypatch.setenv("HOPSTT_PAID_FALLBACK_RESERVE_MINUTES_PER_CALL", "4")
    redis = FakeRedis()
    cfg = _user_config()  # endpoint 127.0.0.1:9 -> real health check fails fast

    first = await hopstt_fallback.apply_hopstt_paid_fallback(
        cfg, organization_id=None, redis_client=redis
    )
    assert isinstance(first.stt, CartesiaSTTConfiguration)
    assert first.stt.get_all_api_keys() == [
        "cartesia-key-abcd"
    ]  # from the org's TTS block
    second = await hopstt_fallback.apply_hopstt_paid_fallback(
        cfg, organization_id=None, redis_client=redis
    )
    assert second.stt.provider == ServiceProviders.CARTESIA
    third = await hopstt_fallback.apply_hopstt_paid_fallback(
        cfg, organization_id=None, redis_client=redis
    )
    assert third is cfg  # 4 + 4 + 4 > 10: budget reached, stays on HopSTT
    count_key = next(
        k for k in redis.values if k.startswith("hopstt:paid_fallback:count:")
    )
    assert redis.values[count_key] == 2


@pytest.mark.asyncio
async def test_fallback_without_provider_credentials_stays_on_hopstt(monkeypatch):
    _enable_fallback(monkeypatch)
    monkeypatch.setenv("HOPSTT_PAID_FALLBACK_PROVIDER", "deepgram")

    async def down(*a, **k):
        return False, "down"

    monkeypatch.setattr(hopstt_fallback, "any_endpoint_ready", down)
    cfg = _user_config()
    assert (
        await hopstt_fallback.apply_hopstt_paid_fallback(cfg, organization_id=None)
        is cfg
    )


@pytest.mark.asyncio
async def test_budget_falls_back_to_process_counter_without_redis():
    class Broken:
        async def eval(self, *a):
            raise ConnectionError("no redis")

    assert await hopstt_fallback.reserve_budget(6, 10, redis_client=Broken()) is True
    assert await hopstt_fallback.reserve_budget(6, 10, redis_client=Broken()) is False
    assert await hopstt_fallback.reserve_budget(1, 0, redis_client=Broken()) is False


@pytest.mark.asyncio
async def test_health_check_is_bounded():
    import time

    start = time.monotonic()
    ok, reason = await hopstt_fallback.any_endpoint_ready(
        ["http://10.255.255.1:8790", "http://127.0.0.1:9"], timeout_s=0.3
    )
    assert ok is False
    assert time.monotonic() - start < 1.0
    assert "not ready" in reason
