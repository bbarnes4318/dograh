"""Inference scheduling.

``LiveScheduler`` owns one inference thread per worker (one GPU). Each tick it
takes at most one queued item from every live session, groups audio chunks
by (engine, chunk size) and advances each group with a single batched
``engine.step()``. Finalize/reset items run after the steps of the same tick,
so each session's items keep their order. Engine state is created and freed
only on this thread.

``BatchPool`` runs recorded-audio jobs with bounded concurrency and a bounded
two-level priority queue (``normal`` before ``low``). In ``both`` role its
engines call a gate between internal chunks of work that waits while the
live scheduler has pending work (live always first), and new jobs do not
start while live utilisation is at or above
``HOPSTT_BATCH_PAUSE_AT_LIVE_UTIL``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .audio import DecodeError, DurationExceeded, decode_file
from .engines.base import EngineError, JobCancelled, metric_language
from .session import EngineEvent, LiveSession, WorkItem

logger = logging.getLogger("hopstt.scheduler")


class LiveScheduler:
    def __init__(self, engines: dict[str, Any], settings, metrics):
        self.engines = engines
        self.settings = settings
        self.metrics = metrics
        self._sessions: dict[str, LiveSession] = {}
        self._to_free: list[LiveSession] = []
        self._cond = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None
        # Set while no live work is queued or running (batch priority gate).
        self.idle = threading.Event()
        self.idle.set()
        self.ticks = 0

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hopstt-live", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._cond:
            for session in self._sessions.values():
                session.closing = True
                self._to_free.append(session)
            self._sessions.clear()
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ---- session registry (loop thread) ---------------------------------------
    def register(self, session: LiveSession) -> None:
        with self._cond:
            self._sessions[session.key] = session

    def unregister(self, session: LiveSession) -> None:
        """Stop scheduling the session; its engine state is freed on the
        inference thread (never while a step is using it)."""
        with self._cond:
            session.closing = True
            if self._sessions.pop(session.key, None) is not None or session.state is not None:
                self._to_free.append(session)
            self._cond.notify_all()

    def notify(self) -> None:
        with self._cond:
            self.idle.clear()
            self._cond.notify_all()

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def queued_audio_ms(self) -> float:
        return sum(s.backlog_ms for s in list(self._sessions.values()))

    def wait_freed(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                if not self._to_free:
                    return True
            time.sleep(0.005)
        return False

    # ---- inference thread -----------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stop and not self._to_free and not any(
                    s.has_work() for s in self._sessions.values()
                ):
                    self.idle.set()
                    self._cond.wait(timeout=0.5)
                if self._stop and not self._to_free:
                    break
                self.idle.clear()
                frees, self._to_free = self._to_free, []
                work: list[tuple[LiveSession, WorkItem]] = []
                for session in self._sessions.values():
                    item = session.pop()
                    if item is not None:
                        work.append((session, item))
            for session in frees:
                self._free(session)
            if work:
                self._tick(work)
            if self._stop:
                with self._cond:
                    if not self._to_free:
                        break
        self.idle.set()

    def _free(self, session: LiveSession) -> None:
        state, session.state = session.state, None
        if state is not None:
            try:
                session.engine.free(state)
            except Exception:  # pragma: no cover - defensive
                logger.exception("engine free failed session=%s", session.log_id)

    def _ensure_state(self, session: LiveSession) -> bool:
        if session.state is not None:
            return True
        try:
            session.state = session.engine.new_state(session.cfg.language, session.cfg.chunk_ms)
            return True
        except Exception as exc:
            logger.error("new_state failed session=%s err=%s", session.log_id, type(exc).__name__)
            self._fail(session, "model_error", "could not allocate model state")
            return False

    def _fail(self, session: LiveSession, code: str, message: str) -> None:
        session.failed = True
        session.post(EngineEvent(kind="error", code=code, message=message))

    def _tick(self, work: list[tuple[LiveSession, WorkItem]]) -> None:
        self.ticks += 1
        steps: dict[tuple, list[tuple[LiveSession, WorkItem]]] = {}
        controls: list[tuple[LiveSession, WorkItem]] = []
        for session, item in work:
            if session.closing or session.failed:
                continue
            if not self._ensure_state(session):
                continue
            if item.kind == "audio":
                key = (id(session.engine), session.engine.batch_key(session.state))
                steps.setdefault(key, []).append((session, item))
            else:
                controls.append((session, item))

        max_batch = self.settings.max_batch_size
        for group in steps.values():
            for i in range(0, len(group), max_batch):
                self._step(group[i : i + max_batch])
        for session, item in controls:
            self._control(session, item)

    def _step(self, group: list[tuple[LiveSession, WorkItem]]) -> None:
        engine = group[0][0].engine
        model = engine.spec.id
        states = [s.state for s, _ in group]
        chunks = [it.audio for _, it in group]
        started = time.perf_counter()
        try:
            hyps = engine.step(states, chunks)
        except Exception as exc:
            # The whole batch shares one forward pass, so every session in it
            # gets a retryable model_error (clients reconnect and resend).
            logger.error("step failed model=%s batch=%d err=%s", model, len(group), type(exc).__name__)
            for session, item in group:
                session.done_item(item)
                self._fail(session, "model_error", "inference failed")
            return
        elapsed = time.perf_counter() - started
        audio_s = sum(it.audio_ms for _, it in group) / 1000.0
        self.metrics.step_seconds.labels(model).observe(elapsed)
        self.metrics.step_batch_size.labels(model).observe(len(group))
        if audio_s > 0:
            self.metrics.step_rtf.labels(model).observe(elapsed / (audio_s / len(group)))
            self.metrics.audio_seconds.labels("live", model).inc(audio_s)
        for (session, item), hyp in zip(group, hyps):
            session.done_item(item)
            session.post(
                EngineEvent(
                    kind="hyp",
                    text=hyp.text,
                    language=hyp.language,
                    confidence=hyp.confidence,
                    end_ms=item.end_ms,
                    enqueued_at=item.enqueued_at,
                )
            )

    def _control(self, session: LiveSession, item: WorkItem) -> None:
        engine = session.engine
        if item.kind == "reset":
            try:
                engine.reset_utterance(session.state)
            except Exception:  # pragma: no cover - defensive
                logger.exception("reset failed session=%s", session.log_id)
            session.done_item(item)
            return
        tail = item.audio if item.audio is not None else None
        try:
            import numpy as np

            hyp = engine.finalize(session.state, tail if tail is not None else np.zeros(0, np.float32))
        except Exception as exc:
            logger.error("finalize failed model=%s err=%s", engine.spec.id, type(exc).__name__)
            session.done_item(item)
            self._fail(session, "model_error", "inference failed")
            return
        if item.audio_ms:
            self.metrics.audio_seconds.labels("live", engine.spec.id).inc(item.audio_ms / 1000.0)
        session.done_item(item)
        session.post(
            EngineEvent(
                kind="final",
                text=hyp.text,
                language=hyp.language,
                confidence=hyp.confidence,
                end_ms=item.end_ms,
                enqueued_at=item.enqueued_at,
                from_finalize=item.from_finalize,
            )
        )


# ==========================================================================
# Batch
# ==========================================================================
class QueueFull(Exception):
    pass


class LivePriority(Exception):
    """role=both and live utilisation is at the pause threshold."""


@dataclass
class BatchParams:
    model: str
    language: str = "en"
    channels: str = "split"
    word_timestamps: bool = True
    priority: str = "normal"


@dataclass
class Job:
    id: str
    params: BatchParams
    path: str | None  # upload temp file; deleted after decode or on cancel
    kind: str = "job"  # "job" | "sync"
    status: str = "queued"
    attempts: int = 0
    error: dict | None = None
    result: dict | None = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    cancelled: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    max_seconds: float | None = None

    def public(self) -> dict:
        out: dict = {"job_id": self.id, "status": self.status, "attempts": self.attempts}
        if self.error is not None:
            out["error"] = self.error
        if self.result is not None:
            out["result"] = self.result
        return out


def _remove_file(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


class BatchPool:
    def __init__(self, engines: dict[str, Any], settings, metrics, live: LiveScheduler | None, live_util):
        self.engines = engines
        self.settings = settings
        self.metrics = metrics
        self.live = live
        self.live_util = live_util  # callable -> float
        self.jobs: dict[str, Job] = {}
        self._queues: dict[str, deque[Job]] = {"normal": deque(), "low": deque()}
        self._wakeup = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._janitor: asyncio.Task | None = None
        self.running = 0
        self._closing = False

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        for i in range(self.settings.batch_max_concurrency):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"hopstt-batch-{i}"))
        self._janitor = asyncio.create_task(self._purge_loop(), name="hopstt-batch-janitor")

    async def stop(self) -> None:
        self._closing = True
        for job in list(self.jobs.values()):
            if job.status == "queued":
                self._finish(job, "failed", error={"code": "draining", "retryable": True})
            elif job.status == "running":
                job.cancelled = True
        tasks = self._workers + ([self._janitor] if self._janitor else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()

    def fail_all_for_drain(self) -> None:
        for job in list(self.jobs.values()):
            if job.status in ("queued", "running"):
                job.cancelled = True
                if job.status == "queued":
                    self._finish(job, "failed", error={"code": "draining", "retryable": True})

    # ---- queue ----------------------------------------------------------------
    @property
    def queued(self) -> int:
        return len(self._queues["normal"]) + len(self._queues["low"])

    def _update_gauges(self) -> None:
        for prio, q in self._queues.items():
            self.metrics.batch_queue_depth.labels(prio).set(len(q))
        self.metrics.batch_running.set(self.running)

    def live_blocked(self) -> bool:
        if self.settings.role != "both":
            return False
        return self.live_util() >= self.settings.batch_pause_at_live_util

    def submit(self, params: BatchParams, path: str | None, kind: str = "job", max_seconds: float | None = None) -> Job:
        if self.live_blocked():
            raise LivePriority()
        if self.queued >= self.settings.batch_queue_limit:
            raise QueueFull()
        job = Job(id=secrets.token_hex(12), params=params, path=path, kind=kind, max_seconds=max_seconds)
        self.jobs[job.id] = job
        self._queues["low" if params.priority == "low" else "normal"].append(job)
        self._update_gauges()
        self._wakeup.set()
        return job

    def get(self, job_id: str) -> Job | None:
        self._purge()
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.status == "queued":
            for q in self._queues.values():
                try:
                    q.remove(job)
                except ValueError:
                    pass
            self._finish(job, "cancelled")
        elif job.status == "running":
            job.cancelled = True
        return job

    def _finish(self, job: Job, status: str, *, result: dict | None = None, error: dict | None = None) -> None:
        job.status = status
        job.result = result
        job.error = error
        job.finished_at = time.monotonic()
        _remove_file(job.path)
        job.path = None
        self.metrics.batch_jobs.labels(job.kind, status).inc()
        job.done.set()
        self._update_gauges()

    async def _next_job(self) -> Job:
        while True:
            if not self.live_blocked():
                for prio in ("normal", "low"):
                    if self._queues[prio]:
                        job = self._queues[prio].popleft()
                        self._update_gauges()
                        return job
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    # ---- execution ------------------------------------------------------------
    def _gate_for(self, job: Job):
        live = self.live if self.settings.role == "both" else None

        def gate() -> None:
            while True:
                if job.cancelled:
                    raise JobCancelled()
                if live is None or live.idle.wait(0.05):
                    if job.cancelled:
                        raise JobCancelled()
                    return

        return gate

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._next_job()
            if job.status != "queued":
                continue
            job.status = "running"
            job.started_at = time.monotonic()
            self.running += 1
            self._update_gauges()
            self.metrics.batch_queue_seconds.observe(job.started_at - job.created_at)
            try:
                await self._run_job(job)
            except asyncio.CancelledError:
                if job.status == "running":
                    self._finish(job, "failed", error={"code": "draining", "retryable": True})
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("batch worker error")
                if job.status == "running":
                    self._finish(job, "failed", error={"code": "internal", "retryable": True})
            finally:
                self.running -= 1
                self._update_gauges()

    async def _run_job(self, job: Job) -> None:
        params = job.params
        engine = self.engines[params.model]
        try:
            decoded = await asyncio.to_thread(
                decode_file,
                job.path,
                target_rate=engine.sample_rate,
                mix=params.channels == "mix",
                timeout_s=self.settings.ffmpeg_timeout_s,
                max_seconds=job.max_seconds,
            )
        except DurationExceeded as exc:
            self._finish(job, "failed", error={"code": "too_long", "retryable": False, "seconds": round(exc.seconds, 1)})
            return
        except DecodeError as exc:
            self._finish(job, "failed", error={"code": "invalid_audio", "retryable": False, "message": str(exc)})
            return
        finally:
            _remove_file(job.path)
            job.path = None
        gate = self._gate_for(job)
        queue_ms = int(((job.started_at or time.monotonic()) - job.created_at) * 1000)
        attempt_error: dict | None = None
        while job.attempts < self.settings.job_max_attempts:
            job.attempts += 1
            if job.cancelled:
                self._finish(job, "cancelled")
                return
            started = time.perf_counter()
            try:
                transcripts = await asyncio.to_thread(
                    engine.transcribe,
                    decoded.channels,
                    decoded.sample_rate,
                    params.language,
                    params.word_timestamps and engine.spec.word_timestamps,
                    gate,
                )
            except JobCancelled:
                self._finish(job, "cancelled")
                return
            except EngineError as exc:
                attempt_error = {"code": "model_error", "retryable": True}
                if not exc.transient:
                    break
                if job.attempts < self.settings.job_max_attempts:
                    self.metrics.batch_retries.inc()
                    await asyncio.sleep(self.settings.job_retry_backoff_s * job.attempts)
                continue
            except Exception as exc:
                logger.error("batch inference failed model=%s err=%s", params.model, type(exc).__name__)
                attempt_error = {"code": "model_error", "retryable": True}
                if job.attempts < self.settings.job_max_attempts:
                    self.metrics.batch_retries.inc()
                    await asyncio.sleep(self.settings.job_retry_backoff_s * job.attempts)
                continue
            inference_s = time.perf_counter() - started
            self.metrics.batch_inference_seconds.labels(params.model).observe(inference_s)
            self.metrics.audio_seconds.labels("batch", params.model).inc(
                decoded.duration_sec * len(decoded.channels)
            )
            result = build_batch_result(
                engine, decoded, transcripts, params, queue_ms=queue_ms, inference_ms=int(inference_s * 1000)
            )
            self._finish(job, "succeeded", result=result)
            return
        self._finish(job, "failed", error=attempt_error or {"code": "model_error", "retryable": True})

    # ---- TTL ----------------------------------------------------------------
    def _purge(self) -> None:
        now = time.monotonic()
        ttl = self.settings.job_ttl_s
        for job_id, job in list(self.jobs.items()):
            if job.finished_at is not None and now - job.finished_at > ttl:
                del self.jobs[job_id]

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, max(0.05, self.settings.job_ttl_s / 4)))
            self._purge()


def build_batch_result(engine, decoded, transcripts, params: BatchParams, *, queue_ms: int, inference_ms: int) -> dict:
    include_words = params.word_timestamps and engine.spec.word_timestamps
    channels = []
    for index, tr in enumerate(transcripts):
        language = tr.language or params.language
        channels.append(
            {
                "channel": index,
                "language": language,
                "text": tr.text,
                "segments": [seg.to_dict(include_words) for seg in tr.segments],
            }
        )
    return {
        "model": engine.spec.id,
        "engine": engine.name,
        "duration_sec": round(decoded.duration_sec, 3),
        "sample_rate": decoded.source_sample_rate,
        "channels": channels,
        "timings": {"decode_ms": decoded.decode_ms, "queue_ms": queue_ms, "inference_ms": inference_ms},
    }


__all__ = [
    "BatchParams",
    "BatchPool",
    "Job",
    "LivePriority",
    "LiveScheduler",
    "QueueFull",
    "build_batch_result",
    "metric_language",
]
