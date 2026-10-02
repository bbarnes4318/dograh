"""OpenAI Realtime subclass: custom voice object, delivery instructions, output gate."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.services.openai.realtime import events

from api.services.pipecat.output_gate import ConversationOutputGate
from api.services.pipecat.realtime.openai_realtime import DograhOpenAIRealtimeLLMService


def _service(**kwargs) -> DograhOpenAIRealtimeLLMService:
    service = DograhOpenAIRealtimeLLMService(api_key="test-key", **kwargs)
    service.push_frame = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()
    return service


def _session_update(instructions="Workflow prompt."):
    return events.SessionUpdateEvent(
        session=events.SessionProperties(
            instructions=instructions,
            audio=events.AudioConfiguration(
                output=events.AudioOutput(voice="marin", format=events.PCMAudioFormat())
            ),
        )
    )


def test_builtin_voice_serialized_as_string():
    payload = _service().build_client_payload(_session_update())
    assert payload["session"]["audio"]["output"]["voice"] == "marin"


def test_custom_voice_serialized_as_id_object():
    service = _service(voice_type="custom", custom_voice_id="voice_123abc")
    payload = service.build_client_payload(_session_update())
    assert payload["session"]["audio"]["output"]["voice"] == {"id": "voice_123abc"}


def test_custom_voice_requires_id():
    with pytest.raises(ValueError):
        _service(voice_type="custom")


def test_voice_instructions_kept_separate_from_workflow_prompt():
    service = _service(voice_instructions="Speak natural American English.")
    instr = service.build_client_payload(_session_update("Ask for the zip code."))[
        "session"
    ]["instructions"]
    assert instr.startswith("Ask for the zip code.")
    assert "## Voice delivery\nSpeak natural American English." in instr


def test_no_voice_instructions_leaves_prompt_untouched():
    instr = _service().build_client_payload(_session_update("Only workflow."))[
        "session"
    ]["instructions"]
    assert instr == "Only workflow."


@pytest.mark.asyncio
async def test_audio_and_transcript_dropped_while_gate_suppressed():
    gate = ConversationOutputGate()
    service = _service(output_gate=gate)
    audio_evt = SimpleNamespace(
        item_id="i", content_index=0, response_id="r", output_index=0, delta="AAAA"
    )
    gate.suppress()
    await service._handle_evt_audio_delta(audio_evt)
    await service._handle_evt_audio_transcript_delta(SimpleNamespace(delta="Thanks!"))
    await service._handle_evt_text_delta(SimpleNamespace(delta="Goodbye"))
    service.push_frame.assert_not_awaited()
    assert gate.suppressed_count == 3

    gate.resume()
    await service._handle_evt_audio_delta(audio_evt)
    assert service.push_frame.await_count >= 1  # TTSStarted + audio


def test_responses_are_text_only_while_suppressed():
    gate = ConversationOutputGate()
    service = _service(output_gate=gate)
    assert service._get_enabled_modalities() == ["audio"]
    gate.suppress()
    assert service._get_enabled_modalities() == ["text"]
    gate.resume()
    assert service._get_enabled_modalities() == ["audio"]


@pytest.mark.asyncio
async def test_suppress_cancels_in_flight_response():
    gate = ConversationOutputGate()
    service = _service(output_gate=gate)
    service.send_client_event = AsyncMock()
    service._truncate_current_audio_response = AsyncMock()
    await service._cancel_active_output()
    sent = [c.args[0] for c in service.send_client_event.await_args_list]
    assert any(isinstance(e, events.ResponseCancelEvent) for e in sent)
    service._truncate_current_audio_response.assert_awaited_once()
