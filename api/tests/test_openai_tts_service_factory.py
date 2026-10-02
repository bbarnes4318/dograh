from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import APIStatusError
from pipecat.frames.frames import ErrorFrame
from pipecat.services.openai._constants import OPENAI_SAMPLE_RATE

from api.services.configuration.options.openai import CUSTOM_VOICE_UNAVAILABLE_MESSAGE
from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.openai_tts import DograhOpenAITTSService
from api.services.pipecat.service_factory import create_tts_service

AUDIO_CONFIG = SimpleNamespace(
    transport_out_sample_rate=16000, transport_in_sample_rate=16000
)


def _tts_config(**overrides):
    base = dict(
        provider=ServiceProviders.OPENAI.value,
        api_key="test-key",
        model="gpt-4o-mini-tts",
        voice="alloy",
        base_url=None,
    )
    base.update(overrides)
    return SimpleNamespace(tts=SimpleNamespace(**base))


def test_create_openai_tts_service_uses_openai_pcm_sample_rate():
    with patch(
        "api.services.pipecat.service_factory.DograhOpenAITTSService"
    ) as mock_service:
        create_tts_service(_tts_config(), AUDIO_CONFIG)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["sample_rate"] == OPENAI_SAMPLE_RATE
    assert kwargs["settings"].model == "gpt-4o-mini-tts"


def test_factory_passes_voice_and_delivery_instructions():
    # Regression: the voice used to be dropped, leaving pipecat's default.
    cfg = _tts_config(voice="marin", voice_instructions="Speak casually.")
    service = create_tts_service(cfg, AUDIO_CONFIG)
    assert isinstance(service, DograhOpenAITTSService)
    params = service.build_speech_params("hello")
    assert params["voice"] == "marin"
    assert params["instructions"] == "Speak casually."
    assert params["response_format"] == "pcm"
    assert params["model"] == "gpt-4o-mini-tts"


def test_custom_voice_serialized_as_object_not_string():
    cfg = _tts_config(voice_type="custom", custom_voice_id="voice_123abc")
    service = create_tts_service(cfg, AUDIO_CONFIG)
    assert service.build_speech_params("hi")["voice"] == {"id": "voice_123abc"}


def test_custom_voice_requires_id():
    with pytest.raises(ValueError):
        DograhOpenAITTSService(api_key="k", voice_type="custom")


def _status_error(code):
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    return APIStatusError(
        "boom", response=httpx.Response(code, request=request), body=None
    )


async def _collect(service, text="hi"):
    return [f async for f in service.run_tts(text, "ctx")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404])
async def test_custom_voice_failure_surfaces_clear_error_without_fallback(status):
    service = DograhOpenAITTSService(
        api_key="k", voice_type="custom", custom_voice_id="voice_missing1"
    )
    stream = MagicMock()
    stream.__aenter__ = AsyncMock(side_effect=_status_error(status))
    stream.__aexit__ = AsyncMock(return_value=False)
    service._client = MagicMock()
    service._client.audio.speech.with_streaming_response.create.return_value = stream

    frames = await _collect(service)

    assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
    assert frames[0].error == CUSTOM_VOICE_UNAVAILABLE_MESSAGE
    # exactly one request, with the custom voice — no retry on a built-in voice
    create = service._client.audio.speech.with_streaming_response.create
    assert create.call_count == 1
    assert create.call_args.kwargs["voice"] == {"id": "voice_missing1"}
