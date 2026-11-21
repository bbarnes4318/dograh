"""Shadow-test tap for the HopSTT migration (Stage 5).

When ``HOPSTT_SHADOW_ENABLED=true`` and the workflow id is listed in
``HOPSTT_SHADOW_WORKFLOW_IDS`` (comma list; default empty = never), the
pipeline gets a :class:`HopSTTShadowTap` right after ``transport.input``. It
passes every frame through unchanged and immediately, and feeds a copy of the
caller audio to a secondary HopSTT session in the background, sending
``finalize`` on ``VADUserStoppedSpeakingFrame`` (which reaches the tap
upstream, broadcast by the user aggregator). The primary STT keeps serving the
call; the shadow never affects it:

* it never pushes frames of its own (no transcripts, no ``ErrorFrame``);
* the hand-off is a bounded queue: when it is full the frame is dropped and
  counted, the pipeline never waits;
* any failure (including HopSTT's own fatal path) just stops shadowing for
  that call.

A companion :class:`HopSTTShadowPrimaryObserver`, placed right after the
primary STT, records the primary provider's final ``TranscriptionFrame`` text
for the optional capture file. It also passes everything through untouched.

Logs: aggregate per-call metrics only (finals, finalize->final p50/p95,
reconnects, errors, drops). No transcript text.

``HOPSTT_SHADOW_CAPTURE_DIR``: when set, writes ``<session>.jsonl`` with one
record per utterance (shadow hypothesis + primary transcript text, with
timestamps) for offline WER. **This file contains caller speech (PII). Enable
it only for calls you are authorized to record and evaluate, keep the
directory access-controlled, and delete it after the evaluation.**

Other environment: ``HOPSTT_SHADOW_ENDPOINT`` (default
``HOPSTT_DEFAULT_ENDPOINT``, then the built-in default), ``HOPSTT_API_KEY``,
``HOPSTT_SHADOW_MODEL`` (default nemotron-3.5-streaming),
``HOPSTT_SHADOW_LANGUAGE`` (default en), ``HOPSTT_SHADOW_QUEUE_FRAMES``
(default 500, ~10 s of 20 ms frames).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from pathlib import Path

from loguru import logger

from api.services.configuration.hopstt import env_flag, split_endpoints
from api.services.configuration.registry import HOPSTT_DEFAULT_ENDPOINT
from api.services.pipecat.hopstt_stt import (
    HopSTTService,
    HopSTTSettings,
    hopstt_session_id,
)
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)


def shadow_enabled_for(workflow_id) -> bool:
    if not env_flag("HOPSTT_SHADOW_ENABLED", False) or workflow_id is None:
        return False
    ids = {p.strip() for p in os.getenv("HOPSTT_SHADOW_WORKFLOW_IDS", "").split(",")}
    return str(workflow_id) in ids - {""}


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


class _ShadowHopSTTService(HopSTTService):
    """HopSTTService whose output goes to the tap, never into the pipeline."""

    def __init__(self, *, on_final, on_fatal, **kwargs):
        super().__init__(**kwargs)
        self._on_final = on_final
        self._on_fatal = on_fatal

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        if isinstance(frame, TranscriptionFrame):
            self._on_final(frame)
        # Interims, metrics and everything else are dropped.

    async def _fatal(self, reason: str) -> None:
        if self._fatal_sent:
            return
        self._fatal_sent = True
        self._live = False
        self._on_fatal(reason)


class HopSTTShadowTap(FrameProcessor):
    def __init__(
        self,
        *,
        endpoints: list[str] | None = None,
        api_key: str | None = None,
        model: str | None = None,
        language: str | None = None,
        call_id: str | None = None,
        capture_dir: str | None = None,
        queue_frames: int | None = None,
        service_kwargs: dict | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._endpoints = endpoints or split_endpoints(
            os.getenv("HOPSTT_SHADOW_ENDPOINT")
            or os.getenv("HOPSTT_DEFAULT_ENDPOINT")
            or HOPSTT_DEFAULT_ENDPOINT
        )
        self._api_key = api_key or os.getenv("HOPSTT_API_KEY") or None
        self._model = (
            model or os.getenv("HOPSTT_SHADOW_MODEL") or "nemotron-3.5-streaming"
        )
        self._language = language or os.getenv("HOPSTT_SHADOW_LANGUAGE") or "en"
        self._session_id = hopstt_session_id(f"shadow:{call_id}" if call_id else None)
        capture_dir = (
            capture_dir
            if capture_dir is not None
            else os.getenv("HOPSTT_SHADOW_CAPTURE_DIR")
        )
        self._capture_path = (
            Path(capture_dir) / f"{self._session_id}.jsonl" if capture_dir else None
        )
        size = queue_frames or int(
            os.getenv("HOPSTT_SHADOW_QUEUE_FRAMES", "500") or 500
        )
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, size))
        self._service_kwargs = service_kwargs or {}

        self._setup: FrameProcessorSetup | None = None
        self._shadow: _ShadowHopSTTService | None = None
        self._worker: asyncio.Task | None = None
        self._active = False
        self._stopped = False
        self._summary_logged = False

        # Aggregate metrics
        self.finals = 0
        self.primary_finals = 0
        self.finalizes = 0
        self.errors = 0
        self.dropped_frames = 0
        self.latencies_ms: list[float] = []
        self._last_finalize_at: float | None = None
        # Capture: the open utterance
        self._utterance = 0
        self._current: dict | None = None

    # ------------------------------------------------------------ pipecat

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._setup = setup

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Pass-through first, always, unchanged.
        await self.push_frame(frame, direction)
        try:
            if isinstance(frame, StartFrame):
                self._begin(frame)
            elif isinstance(frame, (EndFrame, CancelFrame)):
                self._stop("call ended")
            elif self._active and (
                (
                    isinstance(frame, AudioRawFrame)
                    and direction == FrameDirection.DOWNSTREAM
                )
                or isinstance(frame, VADUserStoppedSpeakingFrame)
            ):
                try:
                    self._queue.put_nowait(frame)
                except asyncio.QueueFull:
                    self.dropped_frames += 1
        except Exception as exc:  # never let the shadow touch the call
            self._fail(f"tap:{type(exc).__name__}")

    async def cleanup(self):
        await self._shutdown()
        await super().cleanup()

    def primary_observer(self) -> "HopSTTShadowPrimaryObserver":
        return HopSTTShadowPrimaryObserver(self)

    # ------------------------------------------------------------ shadow

    def _begin(self, start: StartFrame) -> None:
        if self._active or self._stopped or self._setup is None:
            return
        self._active = True
        self._worker = asyncio.get_running_loop().create_task(self._run(start))

    async def _run(self, start: StartFrame) -> None:
        try:
            kwargs = {
                "endpoints": self._endpoints,
                "api_key": self._api_key,
                "session_id": self._session_id,
                "sample_rate": start.audio_in_sample_rate,
                "settings": HopSTTSettings(model=self._model, language=self._language),
                **self._service_kwargs,
            }
            self._shadow = _ShadowHopSTTService(
                on_final=self._on_shadow_final, on_fatal=self._on_shadow_fatal, **kwargs
            )
            await self._shadow.setup(self._setup)
            await self._shadow.start(start)
            while self._active:
                frame = await self._queue.get()
                if not self._active:
                    break
                if isinstance(frame, VADUserStoppedSpeakingFrame):
                    self._open_utterance()
                    self.finalizes += 1
                    self._last_finalize_at = time.monotonic()
                    await self._shadow.finalize()
                elif frame.audio:
                    async for _ in self._shadow.run_stt(frame.audio):
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(f"worker:{type(exc).__name__}")

    def _on_shadow_final(self, frame: TranscriptionFrame) -> None:
        self.finals += 1
        if self._last_finalize_at is not None:
            self.latencies_ms.append((time.monotonic() - self._last_finalize_at) * 1000)
            self._last_finalize_at = None
        if self._capture_path is not None:
            utt = self._current or self._open_utterance()
            utt["shadow"].append({"text": frame.text, "at": frame.timestamp})

    def _on_shadow_fatal(self, reason: str) -> None:
        self._fail(f"hopstt:{reason[:80]}")

    def record_primary_final(self, frame: TranscriptionFrame) -> None:
        self.primary_finals += 1
        if self._capture_path is not None and not self._stopped:
            utt = self._current or self._open_utterance()
            utt["primary"].append({"text": frame.text, "at": frame.timestamp})

    def _open_utterance(self) -> dict:
        self._flush_utterance()
        self._utterance += 1
        self._current = {
            "utterance": self._utterance,
            "session_id": self._session_id,
            "opened_at": time.time(),
            "shadow": [],
            "primary": [],
        }
        return self._current

    def _flush_utterance(self) -> None:
        current, self._current = self._current, None
        if self._capture_path is None or not current:
            return
        if not current["shadow"] and not current["primary"]:
            return
        try:
            self._capture_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._capture_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(current) + "\n")
            os.chmod(self._capture_path, 0o600)
        except Exception as exc:
            self.errors += 1
            logger.warning(f"HopSTT shadow capture write failed: {type(exc).__name__}")

    def _fail(self, reason: str) -> None:
        self.errors += 1
        if not self._stopped:
            logger.warning(f"HopSTT shadow stopped for this call: {reason}")
        self._stop(reason)

    def _stop(self, reason: str) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._active = False
        try:
            self._queue.put_nowait(EndFrame())  # wake the worker
        except asyncio.QueueFull:
            pass

    async def _shutdown(self) -> None:
        self._stop("cleanup")
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._worker), timeout=1.0)
            except BaseException:
                pass
        if self._shadow is not None:
            try:
                await asyncio.wait_for(
                    self._shadow._shutdown(graceful=False), timeout=1.5
                )
                await asyncio.wait_for(self._shadow.cleanup(), timeout=1.0)
            except BaseException:
                pass
        self._flush_utterance()
        self._log_summary()

    def metrics(self) -> dict:
        p50 = _percentile(self.latencies_ms, 50)
        p95 = _percentile(self.latencies_ms, 95)
        return {
            "finals": self.finals,
            "primary_finals": self.primary_finals,
            "finalizes": self.finalizes,
            "finalize_to_final_ms_p50": round(p50) if p50 is not None else None,
            "finalize_to_final_ms_p95": round(p95) if p95 is not None else None,
            "reconnects": self._shadow.reconnect_count if self._shadow else 0,
            "errors": self.errors,
            "dropped_frames": self.dropped_frames,
        }

    def _log_summary(self) -> None:
        if self._summary_logged or (self._shadow is None and not self.errors):
            return
        self._summary_logged = True
        logger.info(f"HopSTT shadow summary: {json.dumps(self.metrics())}")


class HopSTTShadowPrimaryObserver(FrameProcessor):
    """Passes everything through; tells the tap about primary final text."""

    def __init__(self, tap: HopSTTShadowTap, **kwargs):
        super().__init__(**kwargs)
        self._tap = tap

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            try:
                self._tap.record_primary_final(frame)
            except Exception:
                pass
