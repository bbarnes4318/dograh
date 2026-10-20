from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter

from api.services.configuration import check_validity
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    HopvoiceTTSConfiguration,
    ServiceProviders,
    TTSConfig,
)
from api.services.pipecat import service_factory
from api.services.pipecat.hopvoice_tts import HopvoiceTTSService


def _hopvoice_config(**overrides) -> HopvoiceTTSConfiguration:
    values = {
        "provider": "hopvoice",
        "api_key": None,
        "model": "native-fast",
        "voice": "alex_licensed_v1",
        "language": "en",
        "endpoint": "http://hopvoice:8787",
        "voice_instructions": "Warm, calm, conversational delivery.",
    }
    values.update(overrides)
    return HopvoiceTTSConfiguration.model_validate(values)


def test_hopvoice_is_registered_in_tts_discriminated_union():
    config = TypeAdapter(TTSConfig).validate_python(
        {
            "provider": "hopvoice",
            "api_key": None,
            "model": "native-fast",
            "voice": "alex_licensed_v1",
            "language": "en",
            "endpoint": "http://hopvoice:8787",
        }
    )

    assert isinstance(config, HopvoiceTTSConfiguration)
    assert config.provider == ServiceProviders.HOPVOICE
    assert config.model == "native-fast"
    assert config.endpoint == "http://hopvoice:8787"


def test_hopvoice_validation_rejects_disabled_runtime(monkeypatch):
    monkeypatch.delenv("HOPVOICE_ENABLED", raising=False)
    monkeypatch.setattr(
        check_validity,
        "validate_user_configured_service_url",
        lambda *_args, **_kwargs: None,
    )

    errors = UserConfigurationValidator()._validate_service(
        _hopvoice_config(), "tts"
    )

    assert errors == [
        {
            "model": "tts",
            "message": (
                "Hopvoice TTS is disabled on this runtime. Set "
                "HOPVOICE_ENABLED=true after deploying the Hopvoice GPU sidecar."
            ),
        }
    ]


def test_hopvoice_factory_rejects_disabled_runtime(monkeypatch):
    monkeypatch.delenv("HOPVOICE_ENABLED", raising=False)
    user_config = SimpleNamespace(tts=_hopvoice_config())
    audio_config = SimpleNamespace(transport_out_sample_rate=8000)

    with pytest.raises(HTTPException) as exc_info:
        service_factory.create_tts_service(
            user_config,
            audio_config,
            correlation_id="call-disabled",
        )

    assert exc_info.value.status_code == 503
    assert "HOPVOICE_ENABLED=true" in exc_info.value.detail


def test_hopvoice_factory_builds_persistent_native_adapter(monkeypatch):
    monkeypatch.setenv("HOPVOICE_ENABLED", "true")
    monkeypatch.setattr(
        service_factory,
        "_validate_runtime_service_url",
        lambda *_args, **_kwargs: None,
    )
    user_config = SimpleNamespace(tts=_hopvoice_config())
    audio_config = SimpleNamespace(transport_out_sample_rate=8000)

    service = service_factory.create_tts_service(
        user_config,
        audio_config,
        correlation_id="call-123",
    )

    assert isinstance(service, HopvoiceTTSService)
    assert service._endpoint == "ws://hopvoice:8787/v1/tts/ws"
    assert service._session_id == "call-123"
    assert service.sample_rate == 8000
    assert service._settings.model == "native-fast"
    assert service._settings.voice == "alex_licensed_v1"
    assert service._settings.language == "en"
    assert service._settings.instructions == "Warm, calm, conversational delivery."
