from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pipecat.services.settings import NOT_GIVEN

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    FISH_FREE_TTS_MODELS,
    FISH_TTS_MODELS,
    FishTTSConfiguration,
    ServiceProviders,
    paid_fish_models_allowed,
)
from api.services.pipecat.service_factory import create_tts_service


def _audio_config():
    return SimpleNamespace(
        transport_out_sample_rate=8000,
        transport_in_sample_rate=8000,
    )


def _user_config(**tts_overrides):
    tts = {
        "provider": ServiceProviders.FISH.value,
        "api_key": "test-key",
        "model": "s2.1-pro-free",
        "voice": "voice-ref-1",
        "latency": "balanced",
        "speed": 1.0,
        "volume": 0,
        "normalize": True,
    }
    tts.update(tts_overrides)
    return SimpleNamespace(tts=SimpleNamespace(**tts))


def test_fish_tts_configuration_defaults():
    config = FishTTSConfiguration(api_key="test-key")

    assert config.provider == ServiceProviders.FISH
    # The free tier of S2.1 Pro is the default and the only enabled model.
    assert config.model == "s2.1-pro-free"
    assert config.voice == ""
    assert config.latency == "balanced"
    assert config.speed == 1.0
    assert config.volume == 0
    assert config.normalize is True
    assert FISH_FREE_TTS_MODELS == ["s2.1-pro-free"]
    # The advertised model list contains no paid models.
    assert FISH_TTS_MODELS == ["s2.1-pro-free"]


def test_fish_tts_configuration_rejects_out_of_range_prosody():
    with pytest.raises(ValueError):
        FishTTSConfiguration(api_key="test-key", speed=2.5)
    with pytest.raises(ValueError):
        FishTTSConfiguration(api_key="test-key", volume=-30)


def test_create_fish_tts_service_passes_settings():
    user_config = _user_config(speed=1.2, volume=-3, normalize=False)

    with patch(
        "api.services.pipecat.service_factory.FishAudioTTSService"
    ) as mock_service:
        create_tts_service(user_config, _audio_config())

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["api_key"] == "test-key"
    assert kwargs["output_format"] == "pcm"
    # Sample rate is resolved from the pipeline StartFrame, like other providers.
    assert "sample_rate" not in kwargs
    settings = kwargs["settings"]
    assert settings.model == "s2.1-pro-free"
    assert settings.voice == "voice-ref-1"
    assert settings.latency == "balanced"
    assert settings.prosody_speed == 1.2
    assert settings.prosody_volume == -3
    assert settings.normalize is False


def test_create_fish_tts_service_empty_voice_stays_unset():
    # An empty voice means "use the model's default voice"; the settings field
    # must stay NOT_GIVEN so no empty reference_id reaches the wire.
    user_config = _user_config(voice="")

    with patch(
        "api.services.pipecat.service_factory.FishAudioTTSService"
    ) as mock_service:
        create_tts_service(user_config, _audio_config())

    settings = mock_service.call_args.kwargs["settings"]
    assert settings.voice is NOT_GIVEN


def test_create_fish_tts_service_defaults_model_to_free_tier():
    user_config = _user_config(model=None)

    with patch(
        "api.services.pipecat.service_factory.FishAudioTTSService"
    ) as mock_service:
        create_tts_service(user_config, _audio_config())

    settings = mock_service.call_args.kwargs["settings"]
    assert settings.model == "s2.1-pro-free"


def test_paid_fish_models_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_PAID_FISH_MODELS", raising=False)
    assert paid_fish_models_allowed() is False


@pytest.mark.parametrize("paid_model", ["s2.1-pro", "s2-pro", "s1", "s1-mini"])
def test_fish_configuration_rejects_paid_models_by_default(monkeypatch, paid_model):
    # The frontend saves configurations through this pydantic model, so a paid
    # model cannot be selected from the UI while the flag is unset.
    monkeypatch.delenv("ALLOW_PAID_FISH_MODELS", raising=False)
    with pytest.raises(ValueError, match="ALLOW_PAID_FISH_MODELS"):
        FishTTSConfiguration(api_key="test-key", model=paid_model)


@pytest.mark.parametrize("paid_model", ["s2.1-pro", "s2-pro", "s1"])
def test_create_fish_tts_service_rejects_paid_models_by_default(
    monkeypatch, paid_model
):
    # Runtime gate: even a config stored before the gate existed (bypassing
    # pydantic validation) must fail at service creation — and must NOT fall
    # back to any other model or provider.
    monkeypatch.delenv("ALLOW_PAID_FISH_MODELS", raising=False)
    user_config = _user_config(model=paid_model)

    with patch(
        "api.services.pipecat.service_factory.FishAudioTTSService"
    ) as mock_service:
        with pytest.raises(HTTPException) as exc_info:
            create_tts_service(user_config, _audio_config())

    assert exc_info.value.status_code == 400
    assert "ALLOW_PAID_FISH_MODELS" in exc_info.value.detail
    # No Fish service was constructed at all: rejection, not fallback.
    assert mock_service.call_count == 0


def test_fish_configuration_blank_model_defaults_to_free_tier(monkeypatch):
    monkeypatch.delenv("ALLOW_PAID_FISH_MODELS", raising=False)
    assert FishTTSConfiguration(api_key="test-key", model="").model == "s2.1-pro-free"
    assert (
        FishTTSConfiguration(api_key="test-key", model="   ").model == "s2.1-pro-free"
    )
    assert FishTTSConfiguration(api_key="test-key").model == "s2.1-pro-free"


def test_fish_schema_advertises_only_free_model():
    # The workflow UI builds its model dropdown from this JSON schema, so paid
    # models must not appear as selectable examples.
    schema = FishTTSConfiguration.model_json_schema()
    assert schema["properties"]["model"]["examples"] == ["s2.1-pro-free"]
    assert schema["properties"]["model"]["default"] == "s2.1-pro-free"


def test_fish_paid_model_allowed_only_with_explicit_env_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_PAID_FISH_MODELS", "true")
    assert paid_fish_models_allowed() is True
    config = FishTTSConfiguration(api_key="test-key", model="s2.1-pro")
    assert config.model == "s2.1-pro"

    user_config = _user_config(model="s2.1-pro")
    with patch(
        "api.services.pipecat.service_factory.FishAudioTTSService"
    ) as mock_service:
        create_tts_service(user_config, _audio_config())
    assert mock_service.call_args.kwargs["settings"].model == "s2.1-pro"

    # And the gate closes again the moment the flag is unset.
    monkeypatch.delenv("ALLOW_PAID_FISH_MODELS")
    assert paid_fish_models_allowed() is False


async def test_fish_rate_limit_reconnects_never_switch_to_paid_model(monkeypatch):
    # Build the REAL FishAudioTTSService through the factory (no mock), then
    # simulate Fish rejecting connections (as it would when the free model is
    # rate limited or unavailable) and assert every connection attempt still
    # requests s2.1-pro-free: there is no code path that swaps in a paid model.
    monkeypatch.delenv("ALLOW_PAID_FISH_MODELS", raising=False)
    service = create_tts_service(_user_config(model=None), _audio_config())

    connect_mock = AsyncMock(side_effect=Exception("429 Too Many Requests"))
    with patch("pipecat.services.fish.tts.websocket_connect", connect_mock):
        with (
            patch.object(service, "push_error", new=AsyncMock()),
            patch.object(service, "_call_event_handler", new=AsyncMock()),
        ):
            await service._connect_websocket()
            await service._connect_websocket()  # retry after the failure

    assert connect_mock.call_count == 2
    for call in connect_mock.call_args_list:
        assert call.kwargs["additional_headers"]["model"] == "s2.1-pro-free"
    # The configured model is unchanged after the failures — no fallback.
    assert service._settings.model == "s2.1-pro-free"
    assert service._websocket is None


def test_fish_is_registered_for_key_validation():
    validator = UserConfigurationValidator()
    assert ServiceProviders.FISH.value in validator._validator_map


def test_fish_key_validation_accepts_valid_key():
    validator = UserConfigurationValidator()
    with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
        mock_get.return_value.status_code = 200
        assert validator._check_fish_api_key("s2.1-pro-free", "fish-valid-key") is True
    called_url = mock_get.call_args.args[0]
    assert called_url == "https://api.fish.audio/wallet/self/api-credit"
    assert (
        mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer fish-valid-key"
    )


def test_fish_key_validation_rejects_bad_key():
    validator = UserConfigurationValidator()
    with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
        mock_get.return_value.status_code = 401
        with pytest.raises(ValueError):
            validator._check_fish_api_key("s2.1-pro-free", "bad-key")


def test_fish_key_validation_tolerates_provider_errors():
    # A transient 5xx from Fish Audio must not block saving a configuration.
    validator = UserConfigurationValidator()
    with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
        mock_get.return_value.status_code = 503
        assert validator._check_fish_api_key("s2.1-pro-free", "fish-valid-key") is True
