"""GPT-Live service: session lifecycle, audio path, delegation, output gate, failures.

A real local WebSocket server stands in for OpenAI's Live endpoint so the
service's connection handling, payloads and task cleanup run for real.
"""

import asyncio
import base64
import json
from contextlib import asynccontextmanager

import pytest
from pipecat.audio.utils import create_stream_resampler, ulaw_to_pcm
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    FunctionCallResultProperties,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMContextFrame,
    SpeechOutputAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.settings import LLMSettings
from websockets.asyncio.server import serve

from api.services.configuration.options.openai import CUSTOM_VOICE_UNAVAILABLE_MESSAGE
from api.services.pipecat.output_gate import ConversationOutputGate
from api.services.pipecat.realtime.openai_live import (
    DograhOpenAILiveLLMService,
    build_frontend_instructions,
)
from api.services.pipecat.realtime.openai_live.service import OPENING_PROMPT
from api.services.pipecat.worker_runner import run_pipeline_worker


class FakeLiveServer:
    """Minimal scripted OpenAI Live endpoint."""

    def __init__(self):
        self.received: list[dict] = []
        self.headers = None
        self.connections = 0
        self.closed = asyncio.Event()
        self._ws = None
        self.started = asyncio.Event()
        self.auto_start = True

    async def handler(self, ws):
        self.connections += 1
        self._ws = ws
        self.headers = ws.request.headers
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                if msg["type"] == "session.start" and self.auto_start:
                    await ws.send(
                        json.dumps(
                            {"type": "session.started", "session": {"id": "sess_1"}}
                        )
                    )
                    self.started.set()
                elif msg["type"] == "session.close":
                    await ws.send(
                        json.dumps({"type": "session.closed", "reason": "client"})
                    )
        finally:
            self.closed.set()

    async def send(self, event: dict):
        await self._ws.send(json.dumps(event))

    async def drop(self):
        await self._ws.close()

    def of_type(self, t):
        return [m for m in self.received if m["type"] == t]


class Capture(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames: list[Frame] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)


@asynccontextmanager
async def live_session(
    *,
    sample_rate=8000,
    system_instruction="Workflow node prompt.",
    server: FakeLiveServer | None = None,
    **service_kwargs,
):
    server = server or FakeLiveServer()
    async with serve(server.handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        service = DograhOpenAILiveLLMService(
            api_key="sk-test-secret",
            base_url=f"ws://127.0.0.1:{port}",
            **service_kwargs,
        )
        service._context = LLMContext()
        if system_instruction:
            service._settings.system_instruction = system_instruction
        upstream, downstream = Capture(), Capture()
        pipeline = Pipeline([upstream, service, downstream])
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=sample_rate, audio_out_sample_rate=sample_rate
            ),
            enable_rtvi=False,
        )

        class H:
            pass

        h = H()
        h.server, h.service, h.worker = server, service, worker
        h.down, h.up = downstream, upstream
        run = asyncio.create_task(run_pipeline_worker(worker))
        h.run = run
        try:
            await asyncio.wait_for(server.started.wait(), timeout=5)
            yield h
        finally:
            if not run.done():
                await worker.queue_frame(EndFrame())
                try:
                    await asyncio.wait_for(run, timeout=10)
                except Exception:  # noqa: BLE001
                    run.cancel()


async def settle(cond, timeout=3.0):
    async def _w():
        while not cond():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_w(), timeout=timeout)


def audio_evt(pcm_or_ulaw: bytes) -> dict:
    return {
        "type": "session.output_audio.delta",
        "delta": base64.b64encode(pcm_or_ulaw).decode(),
    }


def speech_frames(h):
    return [f for f in h.down.frames if isinstance(f, SpeechOutputAudioRawFrame)]


# ── session creation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_start_payload_for_8k_telephony():
    async with live_session() as h:
        start = h.server.of_type("session.start")[0]["session"]
        assert start["model"] == "gpt-live-1"
        assert start["audio"]["output"]["voice"] == "meridian"
        # 8 kHz telephony: μ-law both ways, no resampling.
        assert start["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
        # Workflow prompt rides the backend delegation, not the frontend persona.
        assert start["delegation"]["type"] == "responses"
        assert (
            start["delegation"]["responses"]["instructions"] == "Workflow node prompt."
        )
        assert "Workflow node prompt." not in start["instructions"]
        # Key is only sent as a header.
        assert h.server.headers["Authorization"] == "Bearer sk-test-secret"
        assert "sk-test-secret" not in json.dumps(h.server.received)


@pytest.mark.asyncio
async def test_pcm24k_for_non_8k_transport():
    async with live_session(sample_rate=16000) as h:
        fmt = h.server.of_type("session.start")[0]["session"]["audio"]["format"]
        assert fmt == {"type": "audio/pcm", "rate": 24000}


@pytest.mark.asyncio
async def test_audio_format_escape_hatch_forces_pcm():
    async with live_session(audio_format="pcm") as h:
        fmt = h.server.of_type("session.start")[0]["session"]["audio"]["format"]
        assert fmt["type"] == "audio/pcm"


@pytest.mark.asyncio
async def test_selectable_voice_is_sent_to_openai():
    async with live_session(voice="gleam") as h:
        assert (
            h.server.of_type("session.start")[0]["session"]["audio"]["output"]["voice"]
            == "gleam"
        )


@pytest.mark.asyncio
async def test_custom_voice_sent_as_object():
    async with live_session(voice_type="custom", custom_voice_id="voice_123abc") as h:
        voice = h.server.of_type("session.start")[0]["session"]["audio"]["output"][
            "voice"
        ]
        assert voice == {"id": "voice_123abc"}


@pytest.mark.asyncio
async def test_voice_delivery_instructions_in_frontend_block_only():
    async with live_session(voice_instructions="Sound casual, short phrases.") as h:
        s = h.server.of_type("session.start")[0]["session"]
        assert "## Voice delivery\nSound casual, short phrases." in s["instructions"]
        assert "Sound casual" not in json.dumps(s["delegation"])


def test_frontend_instructions_contain_no_workflow_logic():
    text = build_frontend_instructions(None)
    assert "delegate" in text and "Voice delivery" not in text


# ── audio path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_input_audio_is_mulaw_encoded_without_resampling():
    async with live_session() as h:
        pcm = b"\x10\x00" * 160  # 20 ms @ 8 kHz
        await h.worker.queue_frame(
            InputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1)
        )
        await settle(lambda: h.server.of_type("session.input_audio.append"))
        evt = h.server.of_type("session.input_audio.append")[0]
        assert len(base64.b64decode(evt["audio"])) == 160  # 1 byte/sample, 20 ms


@pytest.mark.asyncio
async def test_output_audio_decoded_in_order_at_transport_rate():
    async with live_session() as h:
        chunks = [bytes([0xFF - i] * 160) for i in range(5)]  # distinct μ-law chunks
        for c in chunks:
            await h.server.send(audio_evt(c))
        await settle(lambda: len(speech_frames(h)) == 5)
        frames = speech_frames(h)
        assert all(f.sample_rate == 8000 and f.num_channels == 1 for f in frames)
        expected = [
            await ulaw_to_pcm(c, 8000, 8000, create_stream_resampler()) for c in chunks
        ]
        assert [f.audio for f in frames] == expected  # ordering preserved


# ── barge-in ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_caller_speech_starts_user_turn_without_interrupting_model():
    async with live_session() as h:
        await h.server.send(audio_evt(b"\x80" * 160))
        await h.worker.queue_frame(BotStartedSpeakingFrame())
        await h.server.send(
            {
                "type": "session.input_transcript.delta",
                "delta": " hold on",
                "start_ms": 0,
            }
        )
        await settle(
            lambda: any(isinstance(f, UserStartedSpeakingFrame) for f in h.down.frames)
        )
        # GPT-Live handles barge-in natively; Dograh must not add its own interruption.
        assert not any(isinstance(f, InterruptionFrame) for f in h.down.frames)


# ── output gate / terminal chains ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_model_audio_or_transcript_while_gate_suppressed():
    gate = ConversationOutputGate()
    async with live_session(output_gate=gate) as h:
        await h.server.send(audio_evt(b"\x80" * 160))
        await settle(lambda: len(speech_frames(h)) == 1)
        gate.suppress()
        before = len(h.down.frames)
        for _ in range(5):
            await h.server.send(audio_evt(b"\x81" * 160))
        await h.server.send(
            {
                "type": "session.output_transcript.delta",
                "delta": " I sent the text.",
                "start_ms": 0,
            }
        )
        await asyncio.sleep(0.3)
        new = h.down.frames[before:]
        assert not [f for f in new if isinstance(f, SpeechOutputAudioRawFrame)]
        assert gate.suppressed_count >= 6
        gate.resume()
        await h.server.send(audio_evt(b"\x82" * 160))
        await settle(lambda: len(speech_frames(h)) == 2)


async def _run_terminal_chain(tool_names: list[str], gate: ConversationOutputGate):
    """play_audio(complete) -> <tools> through the real handler path.

    Returns (assistant_audio_frames_after_playback, executed_tool_names, harness data).
    """
    executed: list[str] = []
    async with live_session(output_gate=gate) as h:
        svc = h.service

        def make(name):
            async def handler(params):
                executed.append(name)
                await params.result_callback({"status": "success", "tool": name})

            return handler

        for n in tool_names:
            svc.register_function(n, make(n))

        # --- playback owned by the application; model output suppressed ---
        gate.suppress()
        await settle(lambda: gate.enabled is False)
        marker = len(h.down.frames)

        # The model keeps "talking" and delegating after playback completes.
        await h.server.send(audio_evt(b"\x80" * 160))
        await h.server.send(
            {
                "type": "session.output_transcript.delta",
                "delta": " Alright, thanks!",
                "start_ms": 0,
            }
        )
        for i, name in enumerate(tool_names):
            call_id = f"call_{i}"
            await h.server.send(
                {
                    "type": "response.event",
                    "delegation_id": f"d{i}",
                    "event": {"type": "response.created"},
                }
            )
            await h.server.send(
                {
                    "type": "response.event",
                    "delegation_id": f"d{i}",
                    "event": {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "status": "completed",
                            "call_id": call_id,
                            "name": name,
                            "arguments": "{}",
                        },
                    },
                }
            )
            await h.server.send(
                {
                    "type": "response.event",
                    "delegation_id": f"d{i}",
                    "event": {"type": "response.completed", "response": {}},
                }
            )
            await settle(
                lambda c=call_id: any(
                    m["item"].get("call_id") == c
                    for m in h.server.of_type("response.item.create")
                )
            )
            await h.server.send(audio_evt(b"\x81" * 160))  # "Are you still there?"
        await asyncio.sleep(0.3)
        after = [
            f
            for f in h.down.frames[marker:]
            if isinstance(f, SpeechOutputAudioRawFrame)
        ]
        outputs = {
            m["item"]["call_id"]: json.loads(m["item"]["output"])
            for m in h.server.of_type("response.item.create")
        }
        return len(after), executed, outputs


@pytest.mark.asyncio
async def test_play_audio_complete_send_sms_hangup_is_silent():
    gate = ConversationOutputGate()
    frames_after, executed, outputs = await _run_terminal_chain(
        ["send_sms", "end_call"], gate
    )
    assert frames_after == 0  # assistant_audio_frames_after_playback == 0
    assert executed == ["send_sms", "end_call"]
    # Tool results come from the real handlers, not from the model.
    assert outputs["call_0"] == {"status": "success", "tool": "send_sms"}
    assert outputs["call_1"] == {"status": "success", "tool": "end_call"}


@pytest.mark.asyncio
async def test_play_audio_complete_hangup_is_silent():
    gate = ConversationOutputGate()
    frames_after, executed, _ = await _run_terminal_chain(["end_call"], gate)
    assert frames_after == 0
    assert executed == ["end_call"]


@pytest.mark.asyncio
async def test_play_audio_complete_then_conversational_node_resumes_speech():
    gate = ConversationOutputGate()
    async with live_session(output_gate=gate) as h:
        gate.suppress()
        await h.server.send(audio_evt(b"\x80" * 160))
        await asyncio.sleep(0.1)
        assert not speech_frames(h)
        gate.resume()  # what PipecatEngine.set_node does on a node change
        await h.server.send(audio_evt(b"\x80" * 160))
        await settle(lambda: len(speech_frames(h)) == 1)


@pytest.mark.asyncio
async def test_run_llm_false_result_keeps_model_silent_until_caller_speaks():
    async with live_session() as h:

        async def play(params):
            await params.result_callback(
                {"status": "success"},
                properties=FunctionCallResultProperties(run_llm=False),
            )

        h.service.register_function("play_song", play)
        await h.server.send(
            {
                "type": "response.event",
                "delegation_id": "d",
                "event": {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "c1",
                        "name": "play_song",
                        "arguments": "{}",
                    },
                },
            }
        )
        await settle(lambda: h.server.of_type("response.item.create"))
        await h.server.send(audio_evt(b"\x80" * 160))
        await asyncio.sleep(0.2)
        assert not speech_frames(h)
        await h.server.send(
            {"type": "session.input_transcript.delta", "delta": " hello", "start_ms": 0}
        )
        await settle(
            lambda: any(isinstance(f, UserStartedSpeakingFrame) for f in h.down.frames)
        )
        await h.server.send(audio_evt(b"\x80" * 160))
        await settle(lambda: len(speech_frames(h)) == 1)


# ── delegation round trip ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_function_call_runs_registered_handler_and_continues_backend():
    async with live_session() as h:
        seen = {}

        async def lookup(params):
            seen["args"] = dict(params.arguments)
            await params.result_callback({"zip": "10001"})

        h.service.register_function("lookup", lookup)
        for ev in (
            {"type": "response.created"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "c9",
                    "name": "lookup",
                    "arguments": '{"q": "x"}',
                },
            },
            {"type": "response.completed", "response": {}},
        ):
            await h.server.send(
                {"type": "response.event", "delegation_id": "d", "event": ev}
            )
        await settle(lambda: h.server.of_type("response.create"))
        assert seen["args"] == {"q": "x"}
        item = h.server.of_type("response.item.create")[0]["item"]
        assert item["call_id"] == "c9" and json.loads(item["output"]) == {
            "zip": "10001"
        }


@pytest.mark.asyncio
async def test_node_change_updates_backend_instructions():
    async with live_session() as h:
        await h.service._update_settings(
            LLMSettings(system_instruction="Node two prompt.")
        )
        await settle(lambda: h.server.of_type("session.update"))
        upd = h.server.of_type("session.update")[0]["session"]["delegation"][
            "responses"
        ]
        assert upd["instructions"] == "Node two prompt."


# ── call opening ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generated_opening_makes_model_speak_first():
    # Start node without a fixed greeting: the engine queues an LLMContextFrame
    # and expects the bot to open. The caller is muted until it does.
    async with live_session() as h:
        await h.worker.queue_frame(LLMContextFrame(h.service._context))
        await settle(lambda: h.server.of_type("session.commentary.append"))
        opening = h.server.of_type("session.commentary.append")
        assert opening[0]["content"] == OPENING_PROMPT
        assert opening[0]["delegation_id"] is None

        # Later context frames (node changes) do not re-open the call.
        await h.worker.queue_frame(LLMContextFrame(h.service._context))
        await asyncio.sleep(0.2)
        assert len(h.server.of_type("session.commentary.append")) == 1


@pytest.mark.asyncio
async def test_static_greeting_is_not_followed_by_generated_opening():
    async with live_session() as h:
        await h.worker.queue_frame(TTSSpeakFrame("Hi, this is Sam."))
        await h.worker.queue_frame(LLMContextFrame(h.service._context))
        await settle(lambda: h.server.of_type("session.commentary.append"))
        await asyncio.sleep(0.2)
        appended = h.server.of_type("session.commentary.append")
        assert len(appended) == 1
        assert "Hi, this is Sam." in appended[0]["content"]


@pytest.mark.asyncio
async def test_no_generated_opening_once_caller_has_spoken():
    async with live_session() as h:
        await h.server.send(
            {"type": "session.input_transcript.delta", "delta": "hello?", "start_ms": 0}
        )
        await settle(
            lambda: any(isinstance(f, UserStartedSpeakingFrame) for f in h.down.frames)
        )
        await h.worker.queue_frame(LLMContextFrame(h.service._context))
        await asyncio.sleep(0.2)
        assert not h.server.of_type("session.commentary.append")


# ── lifecycle & failures ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_end_closes_upstream_session_and_releases_tasks():
    async with live_session() as h:
        svc = h.service
        await h.worker.queue_frame(EndFrame())
        await asyncio.wait_for(h.run, timeout=10)
        assert h.server.of_type("session.close")  # graceful close requested
        await asyncio.wait_for(h.server.closed.wait(), timeout=3)
        assert svc._websocket is None and svc._receive_task is None
        assert not svc._open_function_calls and not svc._pending_responses


@pytest.mark.asyncio
async def test_upstream_disconnect_mid_call_fails_call_and_stops_output():
    async with live_session() as h:
        await h.server.send(audio_evt(b"\x80" * 160))
        await settle(lambda: len(speech_frames(h)) == 1)
        await h.server.drop()
        await settle(lambda: any(isinstance(f, ErrorFrame) for f in h.up.frames))
        err = next(f for f in h.up.frames if isinstance(f, ErrorFrame))
        assert "disconnected" in err.error.lower()
        assert getattr(err, "fatal", False)
        assert h.service._session_started is False


@pytest.mark.asyncio
async def test_connection_failure_is_fatal_and_leaves_no_task():
    service = DograhOpenAILiveLLMService(api_key="k", base_url="ws://127.0.0.1:1")
    service._context = LLMContext()
    service._settings.system_instruction = "p"
    up = Capture()
    worker = PipelineWorker(
        Pipeline([up, service, Capture()]),
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
        enable_rtvi=False,
    )
    run = asyncio.create_task(run_pipeline_worker(worker))
    await settle(lambda: any(isinstance(f, ErrorFrame) for f in up.frames), timeout=8)
    err = next(f for f in up.frames if isinstance(f, ErrorFrame))
    assert "connection failed" in err.error.lower() and getattr(err, "fatal", False)
    assert service._websocket is None and service._receive_task is None
    if not run.done():
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(run, timeout=10)


@pytest.mark.asyncio
async def test_custom_voice_rejection_reports_clear_error_without_substitution():
    async with live_session(voice_type="custom", custom_voice_id="voice_nope123") as h:
        await h.server.send(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "voice_not_found",
                    "message": "voice not found",
                    "param": "session.audio.output.voice",
                },
            }
        )
        await settle(lambda: any(isinstance(f, ErrorFrame) for f in h.up.frames))
        err = next(f for f in h.up.frames if isinstance(f, ErrorFrame))
        assert CUSTOM_VOICE_UNAVAILABLE_MESSAGE in err.error
        # no second session.start with a different voice
        assert len(h.server.of_type("session.start")) == 1
