"""Agent 12 ("body to body"): Play Song -> Feedback starts the Feedback turn.

The fixture is agent 12's released workflow (definition 194) with the one
configuration change this fix needs: ``transition_on_playback_complete`` on
edge ``3-10`` (PLAY SONG -> FEEDBACK). When the song finishes the workflow
enters FEEDBACK and the assistant speaks first, with no caller input.

Workflows without the flag keep the existing Play Audio behavior: the tool
returns as soon as the audio is queued, with ``run_llm=False``, and the next
generation waits for the caller.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    LLMContextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
)
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams

from api.services.pipecat.worker_runner import run_pipeline_worker
from api.services.workflow.dto import EdgeDataDTO, ReactFlowDTO
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.services.workflow.workflow_graph import WorkflowGraph
from pipecat.tests import MockTTSService
from pipecat.tests.mock_llm_service import ContextCapturingMockLLM

FIXTURE = Path(__file__).parent / "fixtures" / "agent12_body_to_body_workflow.json"

PLAY_SONG_NODE = "3"
FEEDBACK_NODE = "10"
PLAY_SONG_TO_FEEDBACK_EDGE = "3-10"
PLAY_SONG_TOOL_UUID = "90ca5f26-632c-4daa-8d7b-ae629454ec41"
FEEDBACK_LINE = "So, yes or no?"


@dataclass
class _Tool:
    tool_uuid: str
    name: str
    description: str
    category: str
    definition: Dict[str, Any]


# Agent 12's saved Play Song tool: play_audio, terminal_chain not set.
PLAY_SONG_TOOL = _Tool(
    tool_uuid=PLAY_SONG_TOOL_UUID,
    name="Play Song",
    description="",
    category="play_audio",
    definition={
        "schema_version": 1,
        "type": "play_audio",
        "config": {
            "audio_url": "https://aivoice.hopwhistle.com/voice-audio/music/Body_to_Body_Master_09.21.wav"
        },
    },
)


def _agent12_json(*, flag: bool = True) -> dict:
    data = json.loads(FIXTURE.read_text())
    data.pop("_comment", None)
    if not flag:
        for edge in data["edges"]:
            edge["data"].pop("transition_on_playback_complete", None)
    return data


def _graph(*, flag: bool = True) -> WorkflowGraph:
    return WorkflowGraph(ReactFlowDTO.model_validate(_agent12_json(flag=flag)))


def _fake_playback(engine: PipecatEngine, log: list[str], on_song_end=None):
    """Stand-in for play_audio: the transport reports the song start and end."""

    async def fake_play(audio, **kw):
        log.append(f"song_start(output_enabled={engine.conversation_output_enabled})")

        async def song_ends():
            await engine.should_mute_user(BotStartedSpeakingFrame())
            await asyncio.sleep(0.05)
            log.append("song_end")
            if on_song_end:
                on_song_end()
            await engine.should_mute_user(BotStoppedSpeakingFrame())

        asyncio.get_running_loop().create_task(song_ends())

    return fake_play


# ── configuration scope ─────────────────────────────────────────────


def test_flag_defaults_off():
    assert (
        EdgeDataDTO(label="x", condition="x").transition_on_playback_complete is False
    )


def test_only_agent12_play_song_to_feedback_edge_is_flagged():
    graph = _graph()
    flagged = [e.id for e in graph.edges if e.transition_on_playback_complete]
    assert flagged == [PLAY_SONG_TO_FEEDBACK_EDGE]
    edge = next(e for e in graph.edges if e.id == PLAY_SONG_TO_FEEDBACK_EDGE)
    assert graph.nodes[edge.source].name == "PLAY SONG"
    assert graph.nodes[edge.target].name == "FEEDBACK"


def test_definition_without_flag_has_no_flagged_edges():
    assert not [
        e for e in _graph(flag=False).edges if e.transition_on_playback_complete
    ]


# ── end to end through a real pipeline ──────────────────────────────


async def _run_agent12_call(*, flag: bool):
    """OPENING -> (caller already said yes) -> PLAY SONG -> song -> ?

    LLM steps: 0 = OPENING calls ``yes``; 1 = PLAY SONG calls ``play_song``;
    2 = whatever runs next. No user frames are ever sent.
    """
    steps = [
        ContextCapturingMockLLM.create_function_call_chunks(
            function_name="yes", arguments={}, tool_call_id="call_yes"
        ),
        ContextCapturingMockLLM.create_function_call_chunks(
            function_name="play_song", arguments={}, tool_call_id="call_song"
        ),
        ContextCapturingMockLLM.create_text_chunks(FEEDBACK_LINE),
    ]
    llm = ContextCapturingMockLLM(mock_steps=steps, chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=40, frame_delay=0)
    transport = MockTransport(
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        )
    )
    context = LLMContext()
    assistant = LLMContextAggregatorPair(
        context, assistant_params=LLMAssistantAggregatorParams()
    ).assistant()
    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=_graph(flag=flag),
        call_context_vars={"first_name": "Sam"},
        workflow_run_id=1,
    )
    pipeline = Pipeline([llm, tts, transport.output(), assistant])
    task = PipelineWorker(pipeline, params=PipelineParams(), enable_rtvi=False)
    engine.set_task(task)
    engine.set_transport_output(transport.output())
    engine._audio_config = SimpleNamespace(pipeline_sample_rate=8000)

    log: list[str] = []
    generations_when_song_ended: list[int] = []

    async def drive():
        await asyncio.sleep(0.01)
        await engine.initialize()
        await engine.set_node(engine.workflow.start_node_id)
        await engine.llm.queue_frame(LLMContextFrame(engine.context))
        # Wait for the song to end, then give the pipeline time to react.
        for _ in range(200):
            if "song_end" in log:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        await task.queue_frame(EndFrame())

    with (
        patch(
            "api.db:db_client.get_organization_id_by_workflow_run_id",
            new_callable=AsyncMock,
            return_value=1,
        ),
        patch(
            "api.db:db_client.get_tools_by_uuids",
            new_callable=AsyncMock,
            return_value=[PLAY_SONG_TOOL],
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.convert_audio_file",
            new=AsyncMock(return_value=b"\x00\x00" * 800),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.validate_user_configured_service_url"
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.play_audio",
            new=_fake_playback(
                engine,
                log,
                on_song_end=lambda: generations_when_song_ended.append(
                    len(llm.captured_contexts)
                ),
            ),
        ),
    ):
        await asyncio.wait_for(
            asyncio.gather(run_pipeline_worker(task), drive()), timeout=20
        )
    return engine, llm, context, log, generations_when_song_ended[0]


def _assistant_texts_after_song(context: LLMContext) -> list[str]:
    messages = context.get_messages()
    song_result = max(
        i
        for i, m in enumerate(messages)
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_song"
    )
    return [
        m["content"]
        for m in messages[song_result + 1 :]
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    ]


@pytest.mark.asyncio
async def test_agent12_song_end_enters_feedback_and_assistant_speaks_first():
    engine, llm, context, log, gens_at_song_end = await _run_agent12_call(flag=True)

    # The song played with AI output suppressed and finished before anything
    # else happened: only OPENING and PLAY SONG had generated by then.
    assert log[0] == "song_start(output_enabled=False)"
    assert "song_end" in log
    assert gens_at_song_end == 2

    # Feedback was entered and its assistant turn ran without any user turn.
    assert engine._current_node.id == FEEDBACK_NODE
    assert engine.conversation_output_enabled is True
    assert len(llm.captured_contexts) == 3
    feedback_gen = llm.get_context_at_step(2)
    assert "So, yes or no?" in feedback_gen["system_prompt"]
    assert "The song has just finished playing" in feedback_gen["system_prompt"]
    assert not [m for m in feedback_gen["messages"] if m.get("role") == "user"]

    # The first (and only) assistant content after the song is the Feedback line.
    after_song = _assistant_texts_after_song(context)
    assert after_song and after_song[0].startswith(FEEDBACK_LINE)
    assert not [m for m in context.get_messages() if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_without_flag_play_song_keeps_waiting_for_the_caller():
    """Same graph, no flag: today's behavior — nothing runs until the caller talks."""
    engine, llm, context, log, _ = await _run_agent12_call(flag=False)

    assert log[0] == "song_start(output_enabled=True)"
    assert engine._current_node.id == PLAY_SONG_NODE
    assert len(llm.captured_contexts) == 2
    assert _assistant_texts_after_song(context) == []


# ── handler level ───────────────────────────────────────────────────


def _engine_at(node_id: str, *, flag: bool) -> PipecatEngine:
    engine = PipecatEngine(
        workflow=_graph(flag=flag), call_context_vars={}, workflow_run_id=1
    )
    engine._current_node = engine.workflow.nodes[node_id]
    engine._setup_llm_context = AsyncMock()
    engine._transport_output = SimpleNamespace(queue_frame=AsyncMock())
    engine._audio_config = SimpleNamespace(pipeline_sample_rate=8000)
    return engine


async def _call_play_song(engine: PipecatEngine, *, before_song_end=None):
    results: list[tuple] = []
    log: list[str] = []

    async def result_callback(result, properties=None):
        results.append((result, properties))

    fake_play = _fake_playback(engine, log)

    async def play(audio, **kw):
        await fake_play(audio, **kw)
        if before_song_end:
            await before_song_end()

    handler, timeout = CustomToolManager(engine)._create_handler(
        PLAY_SONG_TOOL, "play_song"
    )
    with (
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.convert_audio_file",
            new=AsyncMock(return_value=b"\x00\x00" * 800),
        ),
        patch(
            "api.services.workflow.pipecat_engine_custom_tools.validate_user_configured_service_url"
        ),
        patch("api.services.workflow.pipecat_engine_custom_tools.play_audio", new=play),
    ):
        await handler(SimpleNamespace(arguments={}, result_callback=result_callback))
        await asyncio.sleep(0.1)  # let the simulated song finish
    return results, timeout, log


@pytest.mark.asyncio
async def test_flagged_play_song_waits_for_song_then_requests_feedback_turn():
    engine = _engine_at(PLAY_SONG_NODE, flag=True)
    results, timeout, log = await _call_play_song(engine)

    assert log == ["song_start(output_enabled=False)", "song_end"]
    assert engine._current_node.id == FEEDBACK_NODE
    ((result, props),) = results
    assert result["action"] == "playback_complete"
    assert props.run_llm is True  # the explicit assistant-turn trigger
    assert timeout == 900.0  # long enough for a whole song


@pytest.mark.asyncio
async def test_unflagged_play_song_is_unchanged():
    engine = _engine_at(PLAY_SONG_NODE, flag=False)
    results, timeout, log = await _call_play_song(engine)

    assert log[0] == "song_start(output_enabled=True)"
    assert engine._current_node.id == PLAY_SONG_NODE
    ((result, props),) = results
    assert result["action"] == "playing_audio"
    assert props.run_llm is False
    assert timeout == 60.0
    assert engine.playback_transition_edge() is None


@pytest.mark.asyncio
async def test_flag_does_nothing_outside_its_source_node():
    # The flag lives on 3-10; Play Audio from any other node keeps the default.
    engine = _engine_at("7", flag=True)
    assert engine.playback_transition_edge() is None


@pytest.mark.asyncio
async def test_no_double_transition_if_workflow_moved_during_song():
    engine = _engine_at(PLAY_SONG_NODE, flag=True)

    async def moved():
        engine._current_node = engine.workflow.nodes[FEEDBACK_NODE]

    results, _, _ = await _call_play_song(engine, before_song_end=moved)
    ((result, props),) = results
    assert props.run_llm is False
    assert engine.conversation_output_enabled is True
    engine._setup_llm_context.assert_not_awaited()
