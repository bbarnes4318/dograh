"""Application-state guarantee: media playback / terminal chains stay silent.

play audio -> (send SMS) -> hang up must produce zero AI speech after playback,
enforced by the output gate rather than by prompts. Playback itself and
explicit speech actions are not affected.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.tests.utils import run_test

from api.services.pipecat.output_gate import (
    ConversationOutputGate,
    ConversationOutputGateProcessor,
)
from api.services.workflow.dto import (
    EdgeDataDTO,
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_callbacks import UserIdleHandler
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.services.workflow.workflow_graph import WorkflowGraph


def _workflow() -> WorkflowGraph:
    def node(id_, **kw):
        return RFNodeDTO(
            id=id_,
            type=kw.pop("type"),
            position=Position(x=0, y=0),
            data=kw.pop("data"),
        )

    start = StartCallNodeData(
        name="Start",
        prompt="p",
        is_start=True,
        add_global_prompt=False,
        extraction_enabled=False,
    )
    end = EndCallNodeData(
        name="End",
        prompt="p",
        is_end=True,
        add_global_prompt=False,
        extraction_enabled=False,
    )
    return WorkflowGraph(
        ReactFlowDTO(
            nodes=[
                node("start", type="startCall", data=start),
                node("end", type="endCall", data=end),
            ],
            edges=[
                RFEdgeDTO(
                    id="e",
                    source="start",
                    target="end",
                    data=EdgeDataDTO(label="x", condition="x"),
                )
            ],
        )
    )


def _engine(**kw) -> PipecatEngine:
    return PipecatEngine(
        workflow=_workflow(), call_context_vars={}, workflow_run_id=1, **kw
    )


# ── pipeline (chained) mode ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_processor_drops_model_text_only_while_suppressed():
    gate = ConversationOutputGate()
    gate.suppress()
    down, _ = await run_test(
        ConversationOutputGateProcessor(gate),
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("Are you still there?"),
            LLMFullResponseEndFrame(),
        ],
        # lifecycle frames still flow so aggregators stay balanced
        expected_down_frames=[LLMFullResponseStartFrame, LLMFullResponseEndFrame],
    )
    assert not [f for f in down if isinstance(f, LLMTextFrame)]
    assert gate.suppressed_count == 1


@pytest.mark.asyncio
async def test_gate_processor_passes_text_and_explicit_speech_actions():
    gate = ConversationOutputGate()
    await run_test(
        ConversationOutputGateProcessor(gate),
        frames_to_send=[LLMTextFrame("Hello")],
        expected_down_frames=[LLMTextFrame],
    )
    gate.suppress()
    # An explicit TTSSpeakFrame (a workflow speech action) is not model output.
    await run_test(
        ConversationOutputGateProcessor(gate),
        frames_to_send=[TTSSpeakFrame("Goodbye.")],
        expected_down_frames=[TTSSpeakFrame],
    )


# ── engine: play_audio terminal chain ───────────────────────────────


def _play_tool(terminal: bool):
    return SimpleNamespace(
        definition={
            "config": {
                "audio_url": "https://example.com/song.mp3",
                "terminal_chain": terminal,
            }
        },
        tool_uuid="t1",
        category="play_audio",
    )


async def _run_play_handler(engine: PipecatEngine, terminal: bool):
    """Run the real play_audio handler; returns (events, result_kwargs)."""
    engine._transport_output = SimpleNamespace(queue_frame=AsyncMock())
    engine._audio_config = SimpleNamespace(pipeline_sample_rate=8000)
    order: list[str] = []
    results: list[tuple] = []

    async def fake_play(audio, **kw):
        # gate must already be suppressed when playback starts
        order.append(f"play(gate_enabled={engine.conversation_output_enabled})")

    async def result_callback(result, properties=None):
        order.append("result")
        results.append((result, properties))

    handler = CustomToolManager(engine)._create_play_audio_handler(
        _play_tool(terminal), "play_song"
    )
    params = SimpleNamespace(arguments={}, result_callback=result_callback)

    async def song_ends():
        await asyncio.sleep(0.05)
        # Transport reports the bot started then stopped speaking the song.
        await engine.should_mute_user(BotStartedSpeakingFrame())
        order.append("song_audio_done")
        await engine.should_mute_user(BotStoppedSpeakingFrame())

    with (
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.convert_audio_file",
            new=AsyncMock(return_value=b"\x00\x00" * 8000),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.validate_user_configured_service_url"
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.play_audio",
            new=fake_play,
        ),
    ):
        await asyncio.gather(handler(params), song_ends())
    return order, results


@pytest.mark.asyncio
async def test_terminal_chain_silences_before_playback_and_returns_after_it_finishes():
    engine = _engine()
    order, results = await _run_play_handler(engine, terminal=True)
    assert order == ["play(gate_enabled=False)", "song_audio_done", "result"]
    assert engine.conversation_output_enabled is False
    result, props = results[0]
    assert result["action"] == "playback_complete"
    # The model may take silent follow-up turns (SMS, hangup): run_llm not forced off.
    assert props is None


@pytest.mark.asyncio
async def test_non_terminal_play_audio_is_unchanged():
    engine = _engine()
    order, results = await _run_play_handler(engine, terminal=False)
    assert engine.conversation_output_enabled is True
    assert results[0][0]["action"] == "playing_audio"
    assert results[0][1].run_llm is False


@pytest.mark.asyncio
async def test_node_change_resumes_output_for_conversational_node():
    engine = _engine()
    engine._setup_llm_context = AsyncMock()
    engine.context = MagicMock()
    engine._current_node = engine.workflow.nodes["start"]
    engine.suppress_conversation_output()
    assert engine.conversation_output_enabled is False
    # play_audio_complete -> conversational node
    await engine.set_node("end")
    assert engine.conversation_output_enabled is True


@pytest.mark.asyncio
async def test_staying_in_same_node_keeps_chain_silent():
    engine = _engine()
    engine._setup_llm_context = AsyncMock()
    engine._current_node = engine.workflow.nodes["start"]
    engine.suppress_conversation_output()
    await engine.set_node("start")
    assert engine.conversation_output_enabled is False


@pytest.mark.asyncio
async def test_playback_wait_times_out_without_deadlock():
    engine = _engine()
    engine.begin_playback_wait()
    assert await engine.wait_for_playback_complete(0.05) is False
    # released: a later wait does not block
    assert await engine.wait_for_playback_complete(0.05) is True


@pytest.mark.asyncio
async def test_earlier_bot_stop_does_not_complete_new_playback():
    engine = _engine()
    engine.begin_playback_wait()
    await engine.should_mute_user(BotStoppedSpeakingFrame())  # stale utterance ending
    assert not engine._playback_complete.is_set()


# ── user idle during a terminal chain ───────────────────────────────


def _idle_handler(engine):
    return UserIdleHandler(engine, nudges=None, enabled=True)


@pytest.mark.asyncio
async def test_idle_during_suppressed_output_never_speaks_a_nudge():
    engine = _engine()
    engine.task = SimpleNamespace(queue_frame=AsyncMock())
    engine.end_call_with_reason = AsyncMock()
    engine.suppress_conversation_output()
    await _idle_handler(engine).handle_idle(MagicMock())
    spoken = [c.args[0] for c in engine.task.queue_frame.await_args_list]
    assert not [f for f in spoken if isinstance(f, TTSSpeakFrame)]
    engine.end_call_with_reason.assert_awaited_once()  # silent hangup


@pytest.mark.asyncio
async def test_idle_while_song_still_playing_does_nothing():
    engine = _engine()
    engine.task = SimpleNamespace(queue_frame=AsyncMock())
    engine.end_call_with_reason = AsyncMock()
    engine.suppress_conversation_output()
    engine._bot_is_speaking = True
    await _idle_handler(engine).handle_idle(MagicMock())
    engine.end_call_with_reason.assert_not_awaited()
    engine.task.queue_frame.assert_not_awaited()
