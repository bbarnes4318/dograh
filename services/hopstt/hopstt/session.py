"""Per-connection live session state shared by the WebSocket handler (event
loop thread) and the scheduler (inference thread).

The handler decodes and resamples audio, cuts it into engine chunks and
enqueues ``WorkItem``s; the scheduler pops at most one item per session per
tick, runs the engine, and posts ``EngineEvent``s back to the loop with
``call_soon_threadsafe``. Everything that touches engine state happens on
the scheduler thread; everything that touches the socket happens on the
loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .audio import dbfs


@dataclass
class StreamConfig:
    model: str
    language: str
    sample_rate: int
    encoding: str
    chunk_ms: int
    interim_results: bool = True
    vad_events: bool = False
    endpointing_mode: str = "client"
    silence_ms: int = 500
    session_id: str = ""
    generation: int = 0
    stream_offset_ms: int = 0
    correlation_id: str = ""
    keyterms: tuple[str, ...] = ()


@dataclass
class WorkItem:
    kind: str  # "audio" | "finalize" | "reset"
    audio: np.ndarray | None = None
    end_ms: int = 0  # call-timeline position after this item
    audio_ms: float = 0.0
    enqueued_at: float = field(default_factory=time.perf_counter)
    from_finalize: bool = True


@dataclass
class EngineEvent:
    kind: str  # "hyp" | "final" | "error"
    text: str = ""
    language: str | None = None
    confidence: float | None = None
    end_ms: int = 0
    enqueued_at: float = 0.0
    from_finalize: bool = True
    code: str = ""
    message: str = ""


def log_id(value: str) -> str:
    """Short, non-reversible id for logs (never log raw session ids)."""
    if not value:
        return "-"
    return hashlib.sha256(value.encode()).hexdigest()[:10]


class LiveSession:
    def __init__(self, cfg: StreamConfig, engine: Any, loop: asyncio.AbstractEventLoop, max_queue_ms: int):
        self.cfg = cfg
        self.engine = engine
        self.loop = loop
        self.max_queue_ms = max_queue_ms
        self.key = secrets.token_hex(8)
        self.log_id = log_id(cfg.session_id or self.key)
        self.lock = threading.Lock()
        self.items: deque[WorkItem] = deque()
        self.queued_ms = 0.0
        self.peak_queued_ms = 0.0
        self.inflight_ms = 0.0
        self.processed_ms = 0.0
        self.state: Any = None
        self.closing = False
        self.failed = False
        self.events: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self.space = asyncio.Event()
        self.space.set()

    # ---- loop thread ------------------------------------------------------
    def enqueue(self, item: WorkItem) -> None:
        with self.lock:
            if self.closing:
                return
            self.items.append(item)
            self.queued_ms += item.audio_ms
            self.peak_queued_ms = max(self.peak_queued_ms, self.queued_ms)
            if self.queued_ms >= self.max_queue_ms:
                self.space.clear()

    def drop_pending(self) -> None:
        with self.lock:
            self.items.clear()
            self.queued_ms = 0.0
        self.space.set()

    @property
    def backlog_ms(self) -> float:
        return self.queued_ms + self.inflight_ms

    # ---- scheduler thread ---------------------------------------------------
    def pop(self) -> WorkItem | None:
        with self.lock:
            if not self.items or self.closing:
                return None
            item = self.items.popleft()
            self.queued_ms -= item.audio_ms
            self.inflight_ms = item.audio_ms
            resume = self.queued_ms <= self.max_queue_ms / 2
        if resume:
            self._call(self.space.set)
        return item

    def has_work(self) -> bool:
        return bool(self.items) and not self.closing

    def done_item(self, item: WorkItem) -> None:
        with self.lock:
            self.inflight_ms = 0.0
            self.processed_ms += item.audio_ms

    def post(self, event: EngineEvent) -> None:
        self._call(self.events.put_nowait, event)

    def _call(self, fn, *args) -> None:
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # loop closed during shutdown


class EnergyVad:
    """Hysteresis energy VAD on 20 ms frames of the decoded input audio.

    Speech starts after ``min_speech_ms`` of frames above ``threshold_dbfs``
    and ends after ``silence_ms`` below it. Used for ``vad_events`` and for
    ``endpointing.mode = "server"``.
    """

    def __init__(self, sample_rate: int, threshold_dbfs: float, min_speech_ms: int, silence_ms: int):
        self.frame = sample_rate // 50
        self.sample_rate = sample_rate
        self.threshold = threshold_dbfs
        self.min_speech_frames = max(1, min_speech_ms // 20)
        self.silence_frames = max(1, silence_ms // 20)
        self._buf = np.zeros(0, np.float32)
        self._pos = 0  # samples consumed into frames
        self.in_speech = False
        self._run_speech = 0
        self._run_silence = 0
        self._speech_start_sample = 0

    def push(self, audio: np.ndarray) -> list[tuple[str, int]]:
        """Return [("start"|"end", sample_index)] events."""
        events: list[tuple[str, int]] = []
        self._buf = np.concatenate([self._buf, audio]) if len(self._buf) else audio
        n = len(self._buf) // self.frame
        for i in range(n):
            frame = self._buf[i * self.frame : (i + 1) * self.frame]
            start = self._pos + i * self.frame
            voiced = dbfs(frame) > self.threshold
            if not self.in_speech:
                if voiced:
                    if self._run_speech == 0:
                        self._speech_start_sample = start
                    self._run_speech += 1
                    if self._run_speech >= self.min_speech_frames:
                        self.in_speech = True
                        self._run_silence = 0
                        events.append(("start", self._speech_start_sample))
                else:
                    self._run_speech = 0
            else:
                if voiced:
                    self._run_silence = 0
                else:
                    if self._run_silence == 0:
                        self._silence_start_sample = start
                    self._run_silence += 1
                    if self._run_silence >= self.silence_frames:
                        self.in_speech = False
                        self._run_speech = 0
                        events.append(("end", self._silence_start_sample))
        self._pos += n * self.frame
        self._buf = self._buf[n * self.frame :]
        return events
