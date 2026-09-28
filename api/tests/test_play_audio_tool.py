"""Tests for the play_audio tool: plays a URL's audio to the caller silently."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pipecat.frames.frames import TTSAudioRawFrame

from api.enums import ToolCategory
from api.schemas.tool import PlayAudioConfig
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

FAKE_PCM = b"\x00\x01" * 800
URL = "https://example.com/song.mp3"


def _engine():
    engine = Mock()
    engine._audio_config = SimpleNamespace(pipeline_sample_rate=8000)
    engine._queued_speech_mute_state = "idle"
    engine._frames = []

    async def queue_frame(frame):
        engine._frames.append(frame)

    engine._transport_output.queue_frame = queue_frame
    return engine


def _tool():
    return SimpleNamespace(
        category=ToolCategory.PLAY_AUDIO.value,
        definition={"type": "play_audio", "config": {"audio_url": URL}},
    )


@pytest.mark.asyncio
async def test_plays_converted_audio_without_running_llm():
    engine = _engine()
    handler, timeout = CustomToolManager(engine)._create_handler(_tool(), "play_song")
    params = SimpleNamespace(arguments={}, result_callback=AsyncMock())

    with patch(
        "api.services.workflow.pipecat_engine_custom_tools.convert_audio_file",
        AsyncMock(return_value=FAKE_PCM),
    ) as convert:
        await handler(params)

    convert.assert_awaited_once_with(URL, 8000, "pcm")
    audio = [f for f in engine._frames if isinstance(f, TTSAudioRawFrame)]
    assert audio[0].audio == FAKE_PCM and audio[0].sample_rate == 8000
    assert engine._queued_speech_mute_state == "waiting"
    assert timeout == 60.0
    result, kwargs = params.result_callback.await_args
    assert result[0]["status"] == "success"
    assert kwargs["properties"].run_llm is False


@pytest.mark.asyncio
async def test_failed_download_reports_error_to_llm():
    engine = _engine()
    handler, _ = CustomToolManager(engine)._create_handler(_tool(), "play_song")
    params = SimpleNamespace(arguments={}, result_callback=AsyncMock())

    with patch(
        "api.services.workflow.pipecat_engine_custom_tools.convert_audio_file",
        AsyncMock(return_value=None),
    ):
        await handler(params)

    assert engine._frames == []
    assert params.result_callback.await_args.args[0]["status"] == "error"


def test_config_rejects_non_http_url():
    with pytest.raises(ValueError):
        PlayAudioConfig(audio_url="file:///etc/passwd")
    assert PlayAudioConfig(audio_url=f" {URL} ").audio_url == URL
