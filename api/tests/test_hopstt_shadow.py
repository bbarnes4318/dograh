"""HopSTT shadow tap: pass-through is unchanged and failures never reach the call."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from api.services.pipecat import hopstt_shadow
from api.services.pipecat.hopstt_shadow import (
    HopSTTShadowPrimaryObserver,
    HopSTTShadowTap,
    shadow_enabled_for,
)
from api.services.pipecat.pipeline_builder import build_pipeline
from api.tests.test_hopstt_stt_service import (
    FRAME_BYTES,
    RATE,
    Capture,
    FakeHopSTT,
    wait_until,
)

FAST = {"capacity_routing": False, "backoff_base_s": 0.01, "backoff_max_s": 0.02}


class TapHarness:
    def __init__(self, tap: HopSTTShadowTap):
        self.tap = tap
        self.up = Capture(FrameDirection.UPSTREAM)
        self.down = Capture(FrameDirection.DOWNSTREAM)
        self.primary = tap.primary_observer()
        self.worker = PipelineWorker(
            Pipeline([self.up, tap, self.primary, self.down]),
            cancel_on_idle_timeout=False,
            params=PipelineParams(audio_in_sample_rate=RATE),
        )
        self.runner = WorkerRunner(handle_sigint=False)
        self.sent: list = []

    async def __aenter__(self):
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        await self.worker.queue_frame(EndFrame())
        await asyncio.wait_for(self.task, 10)

    async def audio(self, frames: int):
        for i in range(frames):
            frame = InputAudioRawFrame(
                audio=bytes([i % 256]) * FRAME_BYTES, sample_rate=RATE, num_channels=1
            )
            self.sent.append(frame)
            await self.worker.queue_frame(frame)
        await asyncio.sleep(0.05)

    def passed_audio(self):
        return [f for f in self.down.frames if isinstance(f, InputAudioRawFrame)]


def test_shadow_enabled_for(monkeypatch):
    monkeypatch.delenv("HOPSTT_SHADOW_ENABLED", raising=False)
    monkeypatch.setenv("HOPSTT_SHADOW_WORKFLOW_IDS", "12, 34")
    assert shadow_enabled_for(12) is False
    monkeypatch.setenv("HOPSTT_SHADOW_ENABLED", "true")
    assert shadow_enabled_for(12) is True
    assert shadow_enabled_for("34") is True
    assert shadow_enabled_for(5) is False
    monkeypatch.setenv("HOPSTT_SHADOW_WORKFLOW_IDS", "")
    assert shadow_enabled_for(12) is False


@pytest.mark.asyncio
async def test_passthrough_unchanged_and_shadow_metrics_and_capture(tmp_path):
    async with FakeHopSTT() as server:
        tap = HopSTTShadowTap(
            endpoints=[server.url],
            call_id="run:1",
            capture_dir=str(tmp_path),
            service_kwargs=FAST,
        )
        async with TapHarness(tap) as h:
            conn = await server.wait_conn(1)
            await h.audio(10)
            await wait_until(lambda: len(conn.audio) == 10 * FRAME_BYTES)
            await h.worker.queue_frame(
                VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.UPSTREAM
            )
            await wait_until(lambda: {"type": "finalize"} in conn.controls)
            await conn.transcript(
                "shadow words", final=True, end_ms=200, from_finalize=True
            )
            primary = TranscriptionFrame(
                text="primary words", user_id="", timestamp="t"
            )
            await h.worker.queue_frame(primary)
            await wait_until(lambda: tap.finals == 1 and tap.primary_finals == 1)
            # Exactly the frames sent, same objects, same order; nothing added.
            assert h.passed_audio() == h.sent
            assert [f for f in h.down.frames if isinstance(f, TranscriptionFrame)] == [
                primary
            ]
            assert h.up.of(VADUserStoppedSpeakingFrame)
        assert h.up.of(ErrorFrame) == []
        metrics = tap.metrics()
        assert metrics["finals"] == 1 and metrics["errors"] == 0
        assert metrics["finalize_to_final_ms_p50"] is not None
        lines = (tmp_path / f"{tap._session_id}.jsonl").read_text().splitlines()
        record = json.loads(lines[0])
        assert record["shadow"][0]["text"] == "shadow words"
        assert record["primary"][0]["text"] == "primary words"


@pytest.mark.asyncio
async def test_unreachable_shadow_stops_silently():
    tap = HopSTTShadowTap(
        endpoints=["http://127.0.0.1:9"],
        service_kwargs={**FAST, "recovery_deadline_s": 0.3, "connect_timeout_s": 0.1},
    )
    async with TapHarness(tap) as h:
        await h.audio(5)
        await wait_until(lambda: tap._stopped, timeout=5)
        await h.audio(5)
        assert h.passed_audio() == h.sent
    assert h.up.of(ErrorFrame) == []
    assert tap.errors >= 1


@pytest.mark.asyncio
async def test_auth_failure_stops_shadow_without_error_frame():
    async with FakeHopSTT(reject_all="auth_failed") as server:
        tap = HopSTTShadowTap(endpoints=[server.url], service_kwargs=FAST)
        async with TapHarness(tap) as h:
            await wait_until(lambda: tap._stopped)
            await h.audio(5)
            assert h.passed_audio() == h.sent
        assert h.up.of(ErrorFrame) == []


@pytest.mark.asyncio
async def test_full_queue_drops_and_counts_without_blocking(monkeypatch):
    async def stuck_start(self, frame):
        await asyncio.sleep(3600)

    monkeypatch.setattr(hopstt_shadow._ShadowHopSTTService, "start", stuck_start)
    tap = HopSTTShadowTap(
        endpoints=["http://127.0.0.1:9"], queue_frames=3, service_kwargs=FAST
    )
    async with TapHarness(tap) as h:
        await h.audio(20)
        assert h.passed_audio() == h.sent
        assert tap.dropped_frames == 17
    assert h.up.of(ErrorFrame) == []


def test_build_pipeline_inserts_tap_only_when_given():
    procs = {
        name: FrameProcessor(name=name)
        for name in (
            "in",
            "out",
            "stt",
            "audio_buffer",
            "llm",
            "tts",
            "user",
            "assistant",
            "callback",
            "metrics",
        )
    }
    transport = SimpleNamespace(input=lambda: procs["in"], output=lambda: procs["out"])

    def names(pipeline):
        return [p.name for p in pipeline.processors[1:-1]]

    args = (
        transport,
        procs["stt"],
        procs["audio_buffer"],
        procs["llm"],
        procs["tts"],
        procs["user"],
        procs["assistant"],
        procs["callback"],
        procs["metrics"],
    )
    baseline = names(build_pipeline(*args))
    assert baseline[:3] == ["in", "stt", "user"]

    for p in procs.values():  # processors can be linked once
        p._next = p._prev = None
    tap = HopSTTShadowTap(endpoints=["http://x:1"], name="tap")
    with_tap = build_pipeline(*args, shadow_tap=tap)
    order = [p for p in with_tap.processors[1:-1]]
    assert order[0].name == "in" and order[1] is tap and order[2].name == "stt"
    assert isinstance(order[3], HopSTTShadowPrimaryObserver)
    assert [p.name for p in order[4:]] == baseline[2:]
