import json
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
from api.services.pipecat import hopvoice_tts, service_factory
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

    errors = UserConfigurationValidator()._validate_service(_hopvoice_config(), "tts")

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
    # The configured rate; start() makes it the live sample_rate.
    assert service._init_sample_rate == 8000
    assert service._settings.model == "native-fast"
    assert service._settings.voice == "alex_licensed_v1"
    assert service._settings.language == "en"
    assert service._settings.instructions == "Warm, calm, conversational delivery."


class _FakeHopvoiceSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    @property
    def state(self):
        from websockets.protocol import State

        return State.CLOSED if self.closed else State.OPEN

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return json.dumps({"type": "ready"})

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_hopvoice_adapter_handshakes_through_the_reconnect_hooks(monkeypatch):
    """The base class reconnects through these two methods, so they own the
    session handshake and the goodbye."""
    sockets = []

    async def fake_connect(url, additional_headers=None):
        assert url == "ws://hopvoice:8787/v1/tts/ws"
        sockets.append(_FakeHopvoiceSocket())
        return sockets[-1]

    monkeypatch.setattr(hopvoice_tts, "websocket_connect", fake_connect)
    service = HopvoiceTTSService(
        endpoint="http://hopvoice:8787",
        api_key=None,
        session_id="call-123",
        sample_rate=8000,
        settings=HopvoiceTTSService.Settings(model="native-fast", voice="alex"),
    )
    service._sample_rate = 8000  # what start() sets before connecting

    await service._connect_websocket()
    await service._disconnect_websocket()
    await service._connect_websocket()

    start = sockets[0].sent[0]
    assert start["type"] == "start"
    assert start["session_id"] == "call-123"
    assert start["voice_id"] == "alex"
    assert start["output"]["sample_rate"] == 8000
    assert sockets[0].sent[-1] == {"type": "end"} and sockets[0].closed
    # A reconnect opens a fresh socket and repeats the handshake.
    assert len(sockets) == 2 and sockets[1].sent[0]["type"] == "start"
