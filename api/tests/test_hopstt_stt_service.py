"""Real Pipecat integration tests of HopSTTService against an in-process fake
HopSTT WebSocket server (services/hopstt/PROTOCOL.md §2, incl. §2.5)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transcriptions.language import Language
from websockets.asyncio.server import serve

from api.services.pipecat.hopstt_stt import (
    HopSTTService,
    HopSTTSettings,
    _AudioRing,
    hopstt_session_id,
    parse_endpoints,
    stream_url,
)

RATE = 8000
FRAME_BYTES = 320  # 20 ms of PCM16 @ 8 kHz
BYTES_PER_MS = 16


# --------------------------------------------------------------------------
# Fake HopSTT server
# --------------------------------------------------------------------------


class FakeConn:
    def __init__(self, ws):
        self.ws = ws
        self.config: dict | None = None
        self.audio = bytearray()
        self.controls: list[dict] = []
        self.closed = asyncio.Event()
        self.seq = 0

    @property
    def generation(self) -> int:
        return self.config["generation"]

    async def send(self, **msg):
        self.seq += 1
        msg.setdefault("seq", self.seq)
        msg.setdefault("generation", self.generation)
        await self.ws.send(json.dumps(msg))

    async def transcript(self, text, *, final, utt=1, start_ms=0, end_ms=0, **extra):
        await self.send(
            type="transcript",
            text=text,
            is_final=final,
            from_finalize=extra.pop("from_finalize", False),
            confidence=extra.pop("confidence", 0.9 if final else 0.0),
            language=extra.pop("language", "en"),
            utterance_id=utt,
            start_ms=start_ms,
            end_ms=end_ms,
            **extra,
        )


class FakeHopSTT:
    """Accepts streams; ``reject`` maps connection index -> error code."""

    def __init__(
        self, reject: dict[int, str] | None = None, reject_all: str | None = None
    ):
        self.reject = reject or {}
        self.reject_all = reject_all
        self.conns: list[FakeConn] = []
        self.attempts = 0
        self.new_conn = asyncio.Condition()
        self.url = ""
        self._server = None

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        index = self.attempts
        self.attempts += 1
        raw = await ws.recv()
        cfg = json.loads(raw)
        code = self.reject_all or self.reject.get(index)
        if code:
            fatal = code in {"auth_failed", "invalid_config"}
            await ws.send(
                json.dumps(
                    {
                        "type": "error",
                        "code": code,
                        "message": "x",
                        "retryable": not fatal,
                        "fatal": fatal,
                    }
                )
            )
            await ws.close(1008 if fatal else 1013)
            return
        conn = FakeConn(ws)
        conn.config = cfg
        async with self.new_conn:
            self.conns.append(conn)
            self.new_conn.notify_all()
        await ws.send(
            json.dumps(
                {
                    "type": "ready",
                    "session_id": cfg["session_id"],
                    "generation": cfg["generation"],
                }
            )
        )
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    conn.audio += msg
                else:
                    data = json.loads(msg)
                    conn.controls.append(data)
                    if data["type"] in ("end_of_stream", "cancel"):
                        await ws.send(
                            json.dumps({"type": "closed", "reason": data["type"]})
                        )
                        await ws.close()
        except Exception:
            pass
        finally:
            conn.closed.set()

    async def wait_conn(self, n: int, timeout: float = 5.0) -> FakeConn:
        async with self.new_conn:
            await asyncio.wait_for(
                self.new_conn.wait_for(lambda: len(self.conns) >= n), timeout
            )
        return self.conns[n - 1]


async def wait_until(predicate, timeout: float = 5.0, step: float = 0.01):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


# --------------------------------------------------------------------------
# Pipeline harness
# --------------------------------------------------------------------------


class Capture(FrameProcessor):
    def __init__(self, direction: FrameDirection):
        super().__init__()
        self.direction = direction
        self.frames: list[Frame] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == self.direction:
            self.frames.append(frame)
        await self.push_frame(frame, direction)

    def of(self, cls):
        return [f for f in self.frames if isinstance(f, cls)]


class Harness:
    def __init__(self, stt: HopSTTService, extra: list[FrameProcessor] | None = None):
        self.stt = stt
        self.up = Capture(FrameDirection.UPSTREAM)
        self.down = Capture(FrameDirection.DOWNSTREAM)
        self.pipeline = Pipeline([self.up, stt, *(extra or []), self.down])
        self.worker = PipelineWorker(
            self.pipeline,
            cancel_on_idle_timeout=False,
            params=PipelineParams(audio_in_sample_rate=RATE),
        )
        self.runner = WorkerRunner(handle_sigint=False)
        self.task: asyncio.Task | None = None
        self.sent_audio = bytearray()

    async def __aenter__(self):
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        if not self.task.done():
            await self.worker.queue_frame(EndFrame())
            try:
                await asyncio.wait_for(self.task, 5)
            except asyncio.TimeoutError:
                await self.worker.cancel()
                await asyncio.wait_for(self.task, 5)

    async def audio(self, ms: int, fill: int | None = None):
        """Send ``ms`` of audio in 20 ms frames; bytes encode their position."""
        for _ in range(ms // 20):
            if fill is None:
                # Distinct, position-dependent content so replay is checkable.
                base = len(self.sent_audio) // 2
                pcm = b"".join(
                    ((base + i) % 65536).to_bytes(2, "little")
                    for i in range(FRAME_BYTES // 2)
                )
            else:
                pcm = bytes([fill]) * FRAME_BYTES
            self.sent_audio += pcm
            await self.worker.queue_frame(
                InputAudioRawFrame(audio=pcm, sample_rate=RATE, num_channels=1)
            )
        await asyncio.sleep(0.05)

    async def frame(self, frame: Frame):
        await self.worker.queue_frame(frame)
        await asyncio.sleep(0.05)

    def errors(self):
        return self.up.of(ErrorFrame)


def make_stt(endpoints, **kwargs) -> HopSTTService:
    kwargs.setdefault("capacity_routing", False)
    kwargs.setdefault("backoff_base_s", 0.01)
    kwargs.setdefault("backoff_max_s", 0.05)
    return HopSTTService(
        endpoints=endpoints,
        api_key="test-key",
        session_id=hopstt_session_id("call-1"),
        sample_rate=RATE,
        settings=HopSTTSettings(model="nemotron-3.5-streaming", language="en"),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Unit helpers
# --------------------------------------------------------------------------


def test_parse_endpoints_and_stream_url():
    assert parse_endpoints(" http://a:8790/, http://b:8790 ,,http://a:8790") == [
        "http://a:8790",
        "http://b:8790",
    ]
    assert stream_url("http://a:8790") == "ws://a:8790/v1/stt/stream"
    assert stream_url("https://a/v1") == "wss://a/v1/stt/stream"
    assert stream_url("ws://a:1/api/v1/stt/stream") == "ws://a:1/v1/stt/stream"
    with pytest.raises(ValueError):
        stream_url("a:8790")


def test_session_id_is_opaque_and_stable():
    sid = hopstt_session_id("+15551234567")
    assert sid == hopstt_session_id("+15551234567")
    assert "555" not in sid and len(sid) == 32


def test_audio_ring_bounds_and_commit():
    ring = _AudioRing(max_bytes=100)
    ring.append(b"a" * 60)
    ring.append(b"b" * 60)
    assert (ring.start, ring.end) == (20, 120)
    assert ring.dropped_bytes == 20
    ring.discard_before(70)
    assert ring.read(0, 1000) == b"b" * 50
    assert ring.read(100, 10) == b"b" * 10


# --------------------------------------------------------------------------
# Live stream behaviour
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_interim_and_final_frames():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url, chunk_ms=160)
        async with Harness(stt) as h:
            conn = await server.wait_conn(1)
            cfg = conn.config
            assert cfg["type"] == "config"
            assert cfg["model"] == "nemotron-3.5-streaming"
            assert cfg["language"] == "en"
            assert cfg["sample_rate"] == 8000
            assert cfg["encoding"] == "pcm_s16le"
            assert cfg["chunk_ms"] == 160
            assert cfg["interim_results"] is True
            assert cfg["endpointing"]["mode"] == "client"
            assert cfg["generation"] == 0 and cfg["stream_offset_ms"] == 0
            assert cfg["session_id"] == hopstt_session_id("call-1")

            await h.audio(200)
            await wait_until(lambda: len(conn.audio) == len(h.sent_audio))
            assert bytes(conn.audio) == bytes(h.sent_audio)

            await conn.transcript("my date", final=False, end_ms=100)
            await conn.transcript("My date of birth.", final=True, end_ms=200)
            await wait_until(lambda: h.down.of(TranscriptionFrame))
            interim = h.down.of(InterimTranscriptionFrame)[0]
            final = h.down.of(TranscriptionFrame)[0]
            assert interim.text == "my date" and interim.language == Language.EN
            assert final.text == "My date of birth."
            assert final.language == Language.EN
            assert final.user_id == ""
            assert final.result == {"confidence": 0.9}
            assert stt.committed_ms == 200
        assert h.errors() == []


@pytest.mark.asyncio
async def test_finalize_on_vad_stop_and_marks_final_finalized():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            conn = await server.wait_conn(1)
            await h.frame(VADUserStartedSpeakingFrame())
            await h.audio(100)
            await h.frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
            await wait_until(lambda: {"type": "finalize"} in conn.controls)
            await conn.transcript("Yes.", final=True, end_ms=100, from_finalize=True)
            await wait_until(lambda: h.down.of(TranscriptionFrame))
            assert h.down.of(TranscriptionFrame)[0].finalized is True
        assert {"type": "end_of_stream"} in conn.controls
        assert h.errors() == []


@pytest.mark.asyncio
async def test_drop_mid_utterance_reconnects_and_replays_only_uncommitted_audio():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            c0 = await server.wait_conn(1)
            await h.audio(400)
            await wait_until(lambda: len(c0.audio) == len(h.sent_audio))
            # First utterance committed at 300 ms; 100 ms after it uncommitted.
            await c0.transcript("Hello.", final=True, utt=1, start_ms=0, end_ms=300)
            await wait_until(lambda: stt.committed_ms == 300)
            await h.audio(200)  # mid-utterance
            await wait_until(lambda: len(c0.audio) == len(h.sent_audio))
            await c0.transcript("I am", final=False, utt=2, start_ms=300, end_ms=500)

            await c0.ws.close(1011)  # server drops
            c1 = await server.wait_conn(2)
            assert c1.config["generation"] == 1
            assert c1.config["session_id"] == c0.config["session_id"]
            assert c1.config["stream_offset_ms"] == 300

            await h.audio(100)  # live audio after recovery
            expected = bytes(h.sent_audio[300 * BYTES_PER_MS :])
            await wait_until(lambda: len(c1.audio) == len(expected))
            assert bytes(c1.audio) == expected

            await c1.transcript(
                "I am here.", final=True, utt=1, start_ms=300, end_ms=700
            )
            await wait_until(lambda: len(h.down.of(TranscriptionFrame)) == 2)
            assert [f.text for f in h.down.of(TranscriptionFrame)] == [
                "Hello.",
                "I am here.",
            ]
            assert stt.reconnect_count == 1
        assert h.errors() == []


@pytest.mark.asyncio
async def test_stale_generation_and_duplicate_finals_are_dropped():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            c0 = await server.wait_conn(1)
            await h.audio(300)
            await c0.transcript("One.", final=True, utt=1, end_ms=200)
            await c0.transcript(
                "One.", final=True, utt=1, end_ms=200
            )  # same utterance id
            await wait_until(lambda: stt.dropped_duplicate_finals == 1)
            await c0.ws.close(1012)
            c1 = await server.wait_conn(2)
            # A late message tagged with the old generation on the new socket.
            await c1.send(
                type="transcript",
                text="ghost",
                is_final=True,
                utterance_id=9,
                generation=0,
                start_ms=200,
                end_ms=280,
            )
            # A replay overlap: a final lying entirely before the commit point.
            await c1.transcript("One.", final=True, utt=1, start_ms=0, end_ms=200)
            await c1.transcript("Two.", final=True, utt=2, start_ms=200, end_ms=300)
            await wait_until(lambda: len(h.down.of(TranscriptionFrame)) == 2)
            await asyncio.sleep(0.1)
            assert [f.text for f in h.down.of(TranscriptionFrame)] == ["One.", "Two."]
            assert stt.dropped_stale_messages >= 1
            assert stt.dropped_duplicate_finals == 2
        assert h.errors() == []


@pytest.mark.asyncio
async def test_finalize_requested_during_outage_is_sent_after_replay():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url, backoff_base_s=0.2, backoff_max_s=0.2)
        async with Harness(stt) as h:
            c0 = await server.wait_conn(1)
            server.reject = {1: "capacity"}  # first reconnect attempt is refused
            await c0.ws.close(1011)
            await asyncio.sleep(0.02)
            await h.audio(60)
            await h.frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
            c1 = await server.wait_conn(2)
            await wait_until(lambda: {"type": "finalize"} in c1.controls)
            assert c1.config["generation"] == 2  # gen 1 was the refused attempt
            assert len(c1.audio) == len(h.sent_audio)
        assert h.errors() == []


@pytest.mark.asyncio
async def test_recovery_deadline_exceeded_pushes_exactly_one_fatal_error():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url, recovery_deadline_s=0.5, max_reconnect_attempts=50)
        h = Harness(stt)
        async with h:
            c0 = await server.wait_conn(1)
            server.reject_all = "overloaded"
            await c0.ws.close(1011)
            await wait_until(lambda: h.errors(), timeout=5)
            await asyncio.sleep(0.3)
        errors = h.errors()
        assert len(errors) == 1
        assert errors[0].fatal is True
        assert "recovery deadline exceeded" in errors[0].error


@pytest.mark.asyncio
async def test_auth_failed_is_fatal_immediately():
    async with FakeHopSTT(reject_all="auth_failed") as server:
        stt = make_stt(server.url)
        h = Harness(stt)
        async with h:
            await wait_until(lambda: h.errors())
            await asyncio.sleep(0.2)
        assert server.attempts == 1
        assert len(h.errors()) == 1
        assert h.errors()[0].fatal is True
        assert "auth_failed" in h.errors()[0].error


@pytest.mark.asyncio
async def test_fatal_error_message_mid_call_is_fatal():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        h = Harness(stt)
        async with h:
            c0 = await server.wait_conn(1)
            await c0.send(
                type="error", code="invalid_config", retryable=False, fatal=True
            )
            await wait_until(lambda: h.errors())
            await asyncio.sleep(0.2)
        assert len(h.errors()) == 1 and h.errors()[0].fatal is True
        assert len(server.conns) == 1


@pytest.mark.asyncio
async def test_capacity_moves_to_next_endpoint():
    async with FakeHopSTT(reject_all="capacity") as full, FakeHopSTT() as free:
        stt = make_stt(f"{full.url},{free.url}")
        async with Harness(stt) as h:
            conn = await free.wait_conn(1)
            await h.audio(100)
            await wait_until(lambda: len(conn.audio) == len(h.sent_audio))
            assert full.attempts == 1
            assert conn.config["generation"] == 1
        assert h.errors() == []


@pytest.mark.asyncio
async def test_draining_message_mid_call_reconnects_elsewhere():
    async with FakeHopSTT() as a, FakeHopSTT() as b:
        stt = make_stt(f"{a.url},{b.url}")
        async with Harness(stt) as h:
            c0 = await a.wait_conn(1)
            await c0.send(type="error", code="draining", retryable=True, fatal=False)
            c1 = await b.wait_conn(1)
            assert c1.config["generation"] == 1
        assert h.errors() == []


@pytest.mark.asyncio
async def test_cancel_sends_cancel():
    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        h = Harness(stt)
        await h.__aenter__()
        conn = await server.wait_conn(1)
        await h.worker.cancel()
        await asyncio.wait_for(h.task, 5)
        await wait_until(lambda: conn.closed.is_set())
        assert {"type": "cancel"} in conn.controls


def test_stt_metadata_reports_configured_ttfs():
    stt = make_stt("http://x:1", ttfs_p99_latency=0.42)
    assert stt.service_metadata_frame().ttfs_p99_latency == 0.42
    assert make_stt("http://x:1").service_metadata_frame().ttfs_p99_latency == 0.5


# --------------------------------------------------------------------------
# Pipeline level: interruption and MachineAnswerGuard semantics unchanged
# --------------------------------------------------------------------------


def _hygiene_engine():
    from api.services.pipecat.call_hygiene import CallHygieneState

    engine = MagicMock()
    engine.task = MagicMock()
    engine.task.queue_frame = AsyncMock()
    engine._gathered_context = {}
    engine.call_hygiene = CallHygieneState()
    disposed = {"value": False}

    async def _end(**kwargs):
        disposed["value"] = True

    engine.end_call_for_hygiene = AsyncMock(side_effect=_end)
    engine.is_call_disposed = MagicMock(side_effect=lambda: disposed["value"])
    return engine


@pytest.mark.asyncio
async def test_repeated_machine_interims_end_the_call_once():
    from api.schemas.workflow_configurations import CallHygieneConfiguration
    from api.services.pipecat.call_hygiene import MachineAnswerGuard

    engine = _hygiene_engine()
    guard = MachineAnswerGuard(engine, CallHygieneConfiguration())
    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        async with Harness(stt, [guard]) as h:
            conn = await server.wait_conn(1)
            await h.audio(100)
            phrase = "your call has been forwarded to an automated voice"
            await conn.transcript(phrase, final=False, end_ms=60)
            await conn.transcript(phrase + " messaging", final=False, end_ms=80)
            await conn.transcript(phrase + " messaging system", final=False, end_ms=100)
            await wait_until(lambda: engine.end_call_for_hygiene.await_count >= 1)
            await asyncio.sleep(0.2)
    engine.end_call_for_hygiene.assert_awaited_once_with(
        disposition="voicemail_detected", tag="vm_keyword_guard", abort_immediately=True
    )
    assert engine.call_hygiene.machine_detected is True


@pytest.mark.asyncio
async def test_interim_transcript_starts_user_turn_and_interrupts():
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
        LLMUserAggregatorParams,
    )
    from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    aggregator = LLMUserAggregator(
        LLMContext(),
        params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[TranscriptionUserTurnStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy()],
            )
        ),
    )
    started = []

    @aggregator.event_handler("on_user_turn_started")
    async def _on_started(_agg, strategy):
        started.append(strategy)

    async with FakeHopSTT() as server:
        stt = make_stt(server.url)
        async with Harness(stt, [aggregator]) as h:
            conn = await server.wait_conn(1)
            await h.audio(100)
            await conn.transcript("wait a second", final=False, end_ms=100)
            await wait_until(lambda: started)
            await wait_until(lambda: h.down.of(InterruptionFrame))
    assert isinstance(started[0], TranscriptionUserTurnStartStrategy)
    assert h.errors() == []
