"""HopSTT ASGI server (FastAPI + uvicorn). Implements PROTOCOL.md v1."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from starlette.websockets import WebSocketDisconnect, WebSocketState

from . import __version__
from .audio import ENCODINGS, SAMPLE_RATES, Chunker, FrameDecoder, StreamingResampler, Timeline
from .config import ConfigError, Settings
from .engines import build_engine
from .engines.base import metric_language
from .metrics import Metrics
from .scheduler import BatchParams, BatchPool, LivePriority, LiveScheduler, QueueFull
from .session import EnergyVad, EngineEvent, LiveSession, StreamConfig, WorkItem, log_id

logging.basicConfig(level=os.getenv("HOPSTT_LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("hopstt")

# code -> (retryable, fatal, close code)
ERRORS: dict[str, tuple[bool, bool, int]] = {
    "auth_failed": (False, True, 1008),
    "invalid_config": (False, True, 1008),
    "config_timeout": (True, False, 1008),
    "capacity": (True, False, 1013),
    "draining": (True, False, 1012),
    "overloaded": (True, False, 1013),
    "idle_timeout": (True, False, 1000),
    "model_error": (True, False, 1011),
    "internal": (True, False, 1011),
}


class StreamClosed(Exception):
    """Ends a stream with an error code (or a clean close)."""

    def __init__(self, code: str | None = None, message: str = "", reason: str | None = None):
        super().__init__(message or code or reason or "")
        self.code = code
        self.message = message
        self.reason = reason or code or "closed"


class PayloadTooLarge(Exception):
    pass


# ==========================================================================
# Runtime: engines, schedulers, admission, readiness, drain
# ==========================================================================
class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metrics = Metrics(settings.role)
        self.engines: dict[str, Any] = {}
        self.loaded = False
        self.load_error: str | None = None
        self.draining = False
        self.force_drain = asyncio.Event()
        self.active_streams = 0
        self.active_by_model: dict[str, int] = {}
        self.peak_queued_ms = 0.0  # max per-session queue seen (diagnostics/tests)
        self.live = LiveScheduler({}, settings, self.metrics)
        self.batch: BatchPool | None = None
        self._load_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self.drained = asyncio.Event()

    # ---- startup ------------------------------------------------------------
    async def start(self) -> None:
        self.live.engines = self.engines
        self.batch = BatchPool(self.engines, self.settings, self.metrics, self.live, self.live_util)
        self.live.start()
        self.batch.start()
        self._update_capacity()
        self._load_task = asyncio.create_task(self._load(), name="hopstt-load")

    async def _load(self) -> None:
        started = time.perf_counter()
        try:
            for spec in self.settings.models_to_load():
                engine = build_engine(spec, self.settings)
                await asyncio.to_thread(engine.load)
                if self.settings.warmup:
                    await asyncio.to_thread(engine.warmup)
                else:
                    engine.warm = True
                self.engines[spec.id] = engine
                logger.info("engine ready model=%s engine=%s", spec.id, engine.name)
            self.loaded = True
            logger.info("all engines ready in %.1fs", time.perf_counter() - started)
        except Exception as exc:
            self.load_error = type(exc).__name__
            logger.exception("engine load failed")
        self._update_capacity()

    async def wait_loaded(self, timeout: float = 30.0) -> bool:
        if self._load_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._load_task), timeout)
            except asyncio.TimeoutError:
                return False
        return self.loaded

    async def stop(self) -> None:
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
        self.force_drain.set()
        if self.batch is not None:
            await self.batch.stop()
        await asyncio.to_thread(self.live.stop)
        for engine in self.engines.values():
            try:
                engine.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("engine close failed")

    # ---- capacity -----------------------------------------------------------
    def live_util(self) -> float:
        return self.active_streams / max(1, self.settings.max_streams)

    def live_available(self) -> int:
        if not self.settings.serves_live:
            return 0
        return max(0, self.settings.max_streams - self.active_streams)

    def _update_capacity(self) -> None:
        self.metrics.capacity_available.set(self.live_available())
        self.metrics.draining.set(1 if self.draining else 0)
        self.metrics.ready.set(1 if self.ready_reason() is None else 0)

    def ready_reason(self) -> str | None:
        if self.load_error:
            return "load_failed"
        if not self.loaded or not all(e.loaded and e.warm for e in self.engines.values()):
            return "loading"
        if self.draining:
            return "draining"
        if self.settings.role == "live" and self.live_available() <= 0:
            return "no_capacity"
        if self.settings.role == "batch" and self.batch and self.batch.queued >= self.settings.batch_queue_limit:
            return "no_capacity"
        if self.settings.role == "both" and self.live_available() <= 0 and (
            self.batch is None or self.batch.queued >= self.settings.batch_queue_limit
        ):
            return "no_capacity"
        return None

    def acquire_stream(self) -> bool:
        if self.active_streams >= self.settings.max_streams:
            return False
        self.active_streams += 1
        self._update_capacity()
        return True

    def release_stream(self) -> None:
        self.active_streams = max(0, self.active_streams - 1)
        self._update_capacity()

    def model_entries(self) -> list[dict]:
        out = []
        for spec in self.settings.allowed_specs():
            engine = self.engines.get(spec.id)
            entry = {
                "id": spec.id,
                "kind": spec.kind,
                "engine": engine.name if engine else spec.engine,
                "languages": list(spec.languages),
                "chunk_ms": list(spec.chunk_ms),
                "loaded": bool(engine and engine.loaded),
                "warm": bool(engine and engine.warm),
                "revision": spec.revision,
                "license": spec.license,
            }
            if spec.kind == "streaming":
                entry["default_chunk_ms"] = spec.default_chunk_ms
            else:
                entry["word_timestamps"] = spec.word_timestamps
            out.append(entry)
        return out

    def capacity(self) -> dict:
        batch = self.batch
        return {
            "worker_id": self.settings.worker_id,
            "role": self.settings.role,
            "draining": self.draining,
            "live": {
                "max_streams": self.settings.max_streams if self.settings.serves_live else 0,
                "active_streams": self.active_streams,
                "available": self.live_available(),
            },
            "batch": {
                "max_concurrency": self.settings.batch_max_concurrency if self.settings.serves_batch else 0,
                "running": batch.running if batch else 0,
                "queued": batch.queued if batch else 0,
                "queue_limit": self.settings.batch_queue_limit if self.settings.serves_batch else 0,
            },
            "models": self.model_entries(),
        }

    # ---- drain --------------------------------------------------------------
    def begin_drain(self, on_done=None) -> asyncio.Task:
        if self._drain_task is None:
            self.draining = True
            self._update_capacity()
            logger.info(
                "drain started active_streams=%d timeout_s=%.0f", self.active_streams, self.settings.drain_timeout_s
            )
            self._drain_task = asyncio.create_task(self._drain(on_done), name="hopstt-drain")
        return self._drain_task

    async def _drain(self, on_done) -> None:
        deadline = time.monotonic() + self.settings.drain_timeout_s
        while time.monotonic() < deadline:
            batch_busy = self.batch is not None and (self.batch.running or self.batch.queued)
            if self.active_streams == 0 and not batch_busy:
                break
            await asyncio.sleep(0.1)
        else:
            logger.info("drain timeout; ending %d streams", self.active_streams)
        self.force_drain.set()
        if self.batch is not None:
            self.batch.fail_all_for_drain()
        for _ in range(100):
            if self.active_streams == 0:
                break
            await asyncio.sleep(0.05)
        self.drained.set()
        logger.info("drain complete")
        if on_done is not None:
            on_done()


# ==========================================================================
# helpers
# ==========================================================================
def _token_ok(settings: Settings, provided: str | None) -> bool:
    expected = settings.api_key
    if not expected:
        return settings.dev_insecure
    if not provided:
        hmac.compare_digest(b"x" * len(expected), expected.encode())
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _http_error(status: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
    )


class BodySizeLimit:
    """Pure ASGI middleware: refuses bodies above ``limit`` bytes on the
    batch upload paths while they stream in (Content-Length is checked up
    front; chunked bodies are counted as they arrive)."""

    def __init__(self, app, limit: int, paths: tuple[str, ...]):
        self.app = app
        self.limit = limit
        self.paths = paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in self.paths:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) > self.limit:
                    return await self._reject(send)
            except ValueError:
                pass
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise PayloadTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except PayloadTooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send):
        body = json.dumps(
            {"error": {"code": "too_large", "message": "upload exceeds HOPSTT_MAX_UPLOAD_MB", "retryable": False}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.lower() in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def parse_stream_config(message: dict, rt: Runtime) -> StreamConfig:
    s = rt.settings
    if not isinstance(message, dict) or message.get("type") != "config":
        raise ValueError("first message must be a config")
    model = message.get("model") or s.default_streaming_model
    if not isinstance(model, str) or model not in s.allowed_models:
        raise ValueError("unknown model")
    spec = s.spec(model)
    if spec.kind != "streaming":
        raise ValueError("model is not a streaming model")
    if model not in rt.engines:
        raise ValueError("model is not loaded on this worker")
    language = message.get("language", "en")
    if language not in spec.languages:
        raise ValueError(f"language must be one of {list(spec.languages)}")
    sample_rate = message.get("sample_rate", 16000)
    if isinstance(sample_rate, bool) or sample_rate not in SAMPLE_RATES:
        raise ValueError("sample_rate must be 8000 or 16000")
    encoding = message.get("encoding", "pcm_s16le")
    if encoding not in ENCODINGS:
        raise ValueError(f"encoding must be one of {list(ENCODINGS)}")
    chunk_ms = message.get("chunk_ms", spec.default_chunk_ms)
    if isinstance(chunk_ms, bool) or chunk_ms not in spec.chunk_ms:
        raise ValueError(f"chunk_ms must be one of {list(spec.chunk_ms)}")
    endpointing = message.get("endpointing") or {}
    if not isinstance(endpointing, dict):
        raise ValueError("endpointing must be an object")
    mode = endpointing.get("mode", "client")
    if mode not in ("client", "server"):
        raise ValueError("endpointing.mode must be client or server")
    silence_ms = endpointing.get("silence_ms", 500)
    if isinstance(silence_ms, bool) or not isinstance(silence_ms, int) or not 100 <= silence_ms <= 10000:
        raise ValueError("endpointing.silence_ms must be 100..10000")
    generation = message.get("generation", 0)
    offset = message.get("stream_offset_ms", 0)
    for name, value in (("generation", generation), ("stream_offset_ms", offset)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    session_id = message.get("session_id") or ""
    correlation_id = message.get("correlation_id") or ""
    for name, value in (("session_id", session_id), ("correlation_id", correlation_id)):
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError(f"{name} must be a string of at most 256 chars")
    keyterms = message.get("keyterms") or []
    if not isinstance(keyterms, list) or len(keyterms) > 200 or not all(isinstance(k, str) for k in keyterms):
        raise ValueError("keyterms must be a list of strings")
    return StreamConfig(
        model=model,
        language=language,
        sample_rate=int(sample_rate),
        encoding=encoding,
        chunk_ms=int(chunk_ms),
        interim_results=_as_bool(message.get("interim_results", True), "interim_results"),
        vad_events=_as_bool(message.get("vad_events", False), "vad_events"),
        endpointing_mode=mode,
        silence_ms=silence_ms,
        session_id=session_id,
        generation=generation,
        stream_offset_ms=offset,
        correlation_id=correlation_id,
        keyterms=tuple(keyterms),
    )


# ==========================================================================
# Live stream handler
# ==========================================================================
class StreamHandler:
    def __init__(self, ws: WebSocket, rt: Runtime):
        self.ws = ws
        self.rt = rt
        self.s = rt.settings
        self.seq = 0
        self.generation = 0
        self.session: LiveSession | None = None
        self.cfg: StreamConfig | None = None
        self.close_reason = "unknown"
        self._send_lock = asyncio.Lock()
        # finalize items enqueued but not yet answered by the engine
        self._pending_finals = 0
        self._finals_progress = asyncio.Event()

    # ---- sending ------------------------------------------------------------
    async def send(self, message: dict) -> None:
        async with self._send_lock:
            self.seq += 1
            message["seq"] = self.seq
            message.setdefault("generation", self.generation)
            await self.ws.send_text(json.dumps(message, separators=(",", ":")))

    async def send_error(self, code: str, message: str) -> None:
        retryable, fatal, _ = ERRORS[code]
        self.rt.metrics.stream_errors.labels(code).inc()
        try:
            await self.send(
                {"type": "error", "code": code, "message": message, "retryable": retryable, "fatal": fatal}
            )
        except Exception:
            pass

    async def close(self, code: int) -> None:
        if self.ws.client_state == WebSocketState.CONNECTED:
            try:
                await self.ws.close(code=code)
            except Exception:
                pass

    async def reject(self, code: str, message: str) -> None:
        self.rt.metrics.stream_rejects.labels(code).inc()
        await self.send_error(code, message)
        await self.close(ERRORS[code][2])

    # ---- main ---------------------------------------------------------------
    async def run(self) -> None:
        ws, rt, s = self.ws, self.rt, self.s
        await ws.accept()
        token = _bearer(ws.headers.get("authorization"))
        if token is None and s.allow_query_token:
            token = ws.query_params.get("token")
        if not _token_ok(s, token):
            return await self.reject("auth_failed", "invalid or missing API key")
        if not s.serves_live:
            return await self.reject("capacity", "this worker does not serve live streams")
        if rt.draining:
            return await self.reject("draining", "worker is draining")
        if not rt.loaded:
            return await self.reject("capacity", "worker is not ready")
        if not rt.acquire_stream():
            return await self.reject("capacity", "worker is at max streams")
        try:
            await self._run_admitted()
        finally:
            rt.release_stream()

    async def _run_admitted(self) -> None:
        rt, s = self.rt, self.s
        try:
            first = await asyncio.wait_for(self.ws.receive(), timeout=s.config_timeout_s)
        except asyncio.TimeoutError:
            return await self.reject("config_timeout", "no config received in time")
        except WebSocketDisconnect:
            rt.metrics.stream_rejects.labels("disconnect").inc()
            return
        if first.get("type") == "websocket.disconnect":
            rt.metrics.stream_rejects.labels("disconnect").inc()
            return
        try:
            if first.get("text") is None:
                raise ValueError("first message must be a JSON config")
            cfg = parse_stream_config(json.loads(first["text"]), rt)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return await self.reject("invalid_config", str(exc))
        self.cfg = cfg
        self.generation = cfg.generation
        engine = rt.engines[cfg.model]
        loop = asyncio.get_running_loop()
        session = LiveSession(cfg, engine, loop, s.max_queue_ms)
        self.session = session
        rt.active_by_model[cfg.model] = rt.active_by_model.get(cfg.model, 0) + 1
        rt.metrics.active_streams.labels(cfg.model).set(rt.active_by_model[cfg.model])
        rt.metrics.stream_opens.labels(cfg.model, metric_language(cfg.language)).inc()
        if cfg.generation > 0:
            rt.metrics.stream_reconnects.labels(cfg.model).inc()
        rt.live.register(session)
        opened = time.monotonic()
        logger.info(
            "stream open sid=%s gen=%d model=%s lang=%s rate=%d enc=%s chunk=%d",
            session.log_id, cfg.generation, cfg.model, cfg.language, cfg.sample_rate, cfg.encoding, cfg.chunk_ms,
        )
        close_code = 1000
        try:
            await self.send(
                {
                    "type": "ready",
                    "session_id": cfg.session_id,
                    "generation": cfg.generation,
                    "model": cfg.model,
                    "engine": engine.name,
                    "chunk_ms": cfg.chunk_ms,
                    "sample_rate": cfg.sample_rate,
                    "worker_id": s.worker_id,
                }
            )
            close_code = await self._serve()
        except WebSocketDisconnect:
            self.close_reason = "disconnect"
        except Exception:
            logger.exception("stream failed sid=%s", session.log_id)
            self.close_reason = "internal"
            await self.send_error("internal", "unexpected server error")
            close_code = 1011
        finally:
            # Teardown on every exit path: stop scheduling, free engine state
            # on the inference thread, release admission.
            rt.live.unregister(session)
            session.drop_pending()
            rt.peak_queued_ms = max(rt.peak_queued_ms, session.peak_queued_ms)
            rt.active_by_model[cfg.model] = max(0, rt.active_by_model.get(cfg.model, 1) - 1)
            rt.metrics.active_streams.labels(cfg.model).set(rt.active_by_model[cfg.model])
            rt.metrics.stream_closes.labels(self.close_reason).inc()
            await self.close(close_code)
            logger.info(
                "stream close sid=%s gen=%d reason=%s duration_s=%.1f audio_s=%.1f",
                session.log_id, cfg.generation, self.close_reason, time.monotonic() - opened,
                session.processed_ms / 1000.0,
            )

    async def _serve(self) -> int:
        receiver = asyncio.create_task(self._receiver(), name="hopstt-recv")
        pump = asyncio.create_task(self._pump(), name="hopstt-pump")
        drain = asyncio.create_task(self.rt.force_drain.wait(), name="hopstt-drain-watch")
        tasks = {receiver, pump, drain}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finished = done.pop()
        if finished is drain:
            raise_close = StreamClosed("draining", "worker is shutting down")
        else:
            exc = finished.exception()
            if exc is None:
                raise_close = finished.result()
            elif isinstance(exc, StreamClosed):
                raise_close = exc
            else:
                raise exc
        return await self._finish(raise_close)

    async def _finish(self, closed: StreamClosed) -> int:
        self.close_reason = closed.reason
        if closed.code is None:
            if closed.reason in ("end_of_stream", "cancel"):
                try:
                    await self.send({"type": "closed", "reason": closed.reason})
                except Exception:
                    pass
            return 1000
        if closed.code == "disconnect":
            return 1000
        await self.send_error(closed.code, closed.message)
        return ERRORS[closed.code][2]

    # ---- receiving ----------------------------------------------------------
    async def _receiver(self) -> StreamClosed:
        s, cfg, session = self.s, self.cfg, self.session
        assert cfg is not None and session is not None
        decoder = FrameDecoder(cfg.encoding)
        resampler = StreamingResampler(cfg.sample_rate, session.engine.sample_rate)
        chunk_samples = session.engine.sample_rate * cfg.chunk_ms // 1000
        chunker = Chunker(chunk_samples)
        timeline = Timeline(cfg.sample_rate, cfg.stream_offset_ms)
        out_samples = 0
        engine_rate = session.engine.sample_rate
        vad = None
        if cfg.vad_events or cfg.endpointing_mode == "server":
            vad = EnergyVad(cfg.sample_rate, s.vad_threshold_dbfs, s.vad_min_speech_ms, cfg.silence_ms)
        last_activity = time.monotonic()
        blocked_since: float | None = None
        first_audio_at: float | None = None
        ahead_ms = 0.0
        last_lag_sample = 0.0

        def lag_ms() -> float:
            """How far processing trails the client's real-time position.

            When we stop reading, audio piles up in kernel TCP buffers where
            we cannot count it, so the client's position is estimated from
            the wall clock: a real-time client stays as far ahead of
            wall-clock as it has been since we were last caught up. While
            the session is caught up (nothing queued or in flight, socket
            not paused) the estimate re-anchors, so client pauses
            (keepalive-only periods) never count as lag.
            """
            nonlocal ahead_ms
            if first_audio_at is None:
                return session.backlog_ms
            wall = (time.monotonic() - first_audio_at) * 1000.0
            received = timeline.samples * 1000.0 / cfg.sample_rate
            if session.backlog_ms <= 0 and blocked_since is None:
                ahead_ms = received - wall
            else:
                ahead_ms = max(ahead_ms, received - wall)
            return max(session.backlog_ms, wall + ahead_ms - session.processed_ms)

        def enqueue_finalize(from_finalize: bool, eos: bool = False) -> None:
            tail = chunker.take_rest()
            if eos:
                extra = resampler.flush()
                tail = np.concatenate([tail, extra]) if len(extra) else tail
            item = WorkItem(
                kind="finalize",
                audio=tail,
                end_ms=timeline.now_ms,
                audio_ms=len(tail) * 1000.0 / engine_rate,
                from_finalize=from_finalize,
            )
            self._pending_finals += 1
            session.enqueue(item)
            self.rt.live.notify()

        while True:
            if not session.space.is_set():
                if blocked_since is None:
                    blocked_since = time.monotonic()
                while not session.space.is_set():
                    lag = lag_ms()
                    if lag > s.max_lag_ms:
                        self.rt.metrics.lag_ms.observe(lag)
                        raise StreamClosed("overloaded", "session fell behind real time")
                    try:
                        await asyncio.wait_for(session.space.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
                blocked_since = None
                last_activity = time.monotonic()
            remaining = s.idle_timeout_s - (time.monotonic() - last_activity)
            if remaining <= 0:
                raise StreamClosed("idle_timeout", "no audio or keepalive received")
            try:
                message = await asyncio.wait_for(self.ws.receive(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                lag = lag_ms()
                if lag > s.max_lag_ms:
                    raise StreamClosed("overloaded", "session fell behind real time")
                continue
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                return StreamClosed(None, reason="disconnect")
            data = message.get("bytes")
            if data is not None:
                if len(data) > s.max_frame_bytes:
                    raise StreamClosed("invalid_config", "frame exceeds HOPSTT_MAX_FRAME_BYTES")
                last_activity = time.monotonic()
                audio = decoder.decode(data)
                if not len(audio):
                    continue
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                timeline.advance(len(audio))
                if vad is not None:
                    for kind, at_sample in vad.push(audio):
                        at_ms = timeline.at(at_sample)
                        if cfg.vad_events:
                            await self.send(
                                {"type": "speech_started" if kind == "start" else "speech_ended", "at_ms": at_ms}
                            )
                        if kind == "end" and cfg.endpointing_mode == "server":
                            enqueue_finalize(from_finalize=False)
                resampled = resampler.process(audio)
                chunks = chunker.push(resampled)
                for chunk in chunks:
                    out_samples += len(chunk)
                    session.enqueue(
                        WorkItem(
                            kind="audio",
                            audio=chunk,
                            end_ms=cfg.stream_offset_ms + out_samples * 1000 // engine_rate,
                            audio_ms=len(chunk) * 1000.0 / engine_rate,
                        )
                    )
                if chunks:
                    self.rt.live.notify()
                lag = lag_ms()
                now = time.monotonic()
                if now - last_lag_sample >= 1.0:
                    last_lag_sample = now
                    self.rt.metrics.lag_ms.observe(max(0.0, lag))
                if lag > s.max_lag_ms:
                    self.rt.metrics.lag_ms.observe(lag)
                    raise StreamClosed("overloaded", "session fell behind real time")
                continue
            text = message.get("text")
            if text is None:
                continue
            last_activity = time.monotonic()
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                continue
            ctype = control.get("type") if isinstance(control, dict) else None
            if ctype == "keepalive":
                continue
            if ctype == "finalize":
                enqueue_finalize(from_finalize=True)
                continue
            if ctype == "end_of_stream":
                enqueue_finalize(from_finalize=True, eos=True)
                await self._wait_finals()
                return StreamClosed(None, reason="end_of_stream")
            if ctype == "cancel":
                session.drop_pending()
                chunker.clear()
                return StreamClosed(None, reason="cancel")
            # unknown control messages are ignored (forward compatibility)

    async def _wait_finals(self) -> None:
        while self._pending_finals > 0:
            await self._finals_progress.wait()
            self._finals_progress.clear()

    # ---- engine events -> protocol messages ---------------------------------
    async def _pump(self) -> StreamClosed:
        cfg, session, metrics = self.cfg, self.session, self.rt.metrics
        assert cfg is not None and session is not None
        utterance_id = 1
        last_interim = ""
        committed_ms = cfg.stream_offset_ms
        first_text_ms: int | None = None
        while True:
            event: EngineEvent = await session.events.get()
            if event.kind == "error":
                raise StreamClosed(event.code, event.message)
            language = event.language or cfg.language
            if event.kind == "hyp":
                if not event.text:
                    continue
                if first_text_ms is None:
                    first_text_ms = max(committed_ms, event.end_ms - 2 * cfg.chunk_ms)
                if not cfg.interim_results or event.text == last_interim:
                    continue
                last_interim = event.text
                msg = {
                    "type": "transcript",
                    "text": event.text,
                    "is_final": False,
                    "from_finalize": False,
                    "language": language,
                    "utterance_id": utterance_id,
                    "start_ms": first_text_ms,
                    "end_ms": event.end_ms,
                }
                if event.confidence is not None:
                    msg["confidence"] = round(float(event.confidence), 3)
                await self.send(msg)
                metrics.partial_latency.labels(cfg.model).observe(time.perf_counter() - event.enqueued_at)
            elif event.kind == "final":
                if event.text:
                    start = first_text_ms if first_text_ms is not None else committed_ms
                    start = max(committed_ms, min(start, event.end_ms))
                    msg = {
                        "type": "transcript",
                        "text": event.text,
                        "is_final": True,
                        "from_finalize": event.from_finalize,
                        "language": language,
                        "utterance_id": utterance_id,
                        "start_ms": start,
                        "end_ms": event.end_ms,
                    }
                    if event.confidence is not None:
                        msg["confidence"] = round(float(event.confidence), 3)
                    await self.send(msg)
                    metrics.final_latency.labels(cfg.model).observe(time.perf_counter() - event.enqueued_at)
                    utterance_id += 1
                committed_ms = max(committed_ms, event.end_ms)
                last_interim = ""
                first_text_ms = None
                self._pending_finals = max(0, self._pending_finals - 1)
                self._finals_progress.set()


# ==========================================================================
# App
# ==========================================================================
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    holder: dict[str, Runtime] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime(settings)
        holder["rt"] = rt
        app.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="HopSTT", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    batch_paths = ("/v1/transcriptions", "/v1/jobs")
    app.add_middleware(BodySizeLimit, limit=settings.max_upload_mb * 1024 * 1024, paths=batch_paths)

    def runtime() -> Runtime:
        return holder["rt"]

    def authorized(request: Request) -> bool:
        return _token_ok(settings, _bearer(request.headers.get("authorization")))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        rt = runtime()
        reason = rt.ready_reason()
        rt._update_capacity()
        if reason is None:
            return {"ready": True}
        return JSONResponse(status_code=503, content={"ready": False, "reason": reason})

    @app.get("/v1/capacity")
    async def capacity(request: Request):
        if not authorized(request):
            return _http_error(401, "auth_failed", "invalid or missing API key")
        return runtime().capacity()

    @app.get("/v1/models")
    async def models(request: Request):
        if not authorized(request):
            return _http_error(401, "auth_failed", "invalid or missing API key")
        return {"models": runtime().model_entries()}

    @app.get("/metrics")
    async def metrics_endpoint():
        rt = runtime()
        rt.metrics.queue_audio_ms.set(rt.live.queued_audio_ms())
        rt.metrics.refresh_device(settings.device)
        rt._update_capacity()
        return Response(rt.metrics.render(), media_type="text/plain; version=0.0.4")

    async def _stream(websocket: WebSocket):
        await StreamHandler(websocket, runtime()).run()

    app.add_api_websocket_route("/v1/stt/stream", _stream)
    app.add_api_websocket_route("/api/v1/stt/stream", _stream)

    # ---- batch ----------------------------------------------------------------
    async def _parse_batch(request: Request) -> tuple[BatchParams, str] | JSONResponse:
        rt = runtime()
        if not settings.serves_batch:
            return _http_error(404, "not_found", "batch is not served by this worker")
        if not authorized(request):
            return _http_error(401, "auth_failed", "invalid or missing API key")
        if rt.draining:
            return _http_error(503, "draining", "worker is draining", retryable=True)
        if not rt.loaded:
            return _http_error(503, "not_ready", "worker is not ready", retryable=True)
        try:
            form = await request.form(max_files=1, max_fields=16)
        except PayloadTooLarge:
            raise
        except Exception:
            return _http_error(400, "invalid_request", "expected multipart/form-data")
        upload = form.get("file")
        path = None
        try:
            if upload is None or isinstance(upload, str):
                return _http_error(400, "invalid_config", "file is required")
            model = str(form.get("model") or settings.default_batch_model)
            if model not in settings.allowed_models or settings.spec(model).kind != "batch" or model not in rt.engines:
                return _http_error(400, "invalid_config", "unknown batch model")
            spec = settings.spec(model)
            language = str(form.get("language") or "en")
            if language not in spec.languages:
                return _http_error(400, "invalid_config", f"language must be one of {list(spec.languages)}")
            channels = str(form.get("channels") or "split")
            if channels not in ("split", "mix"):
                return _http_error(400, "invalid_config", "channels must be split or mix")
            priority = str(form.get("priority") or "normal")
            if priority not in ("normal", "low"):
                return _http_error(400, "invalid_config", "priority must be normal or low")
            try:
                word_ts = _as_bool(form.get("word_timestamps", "true"), "word_timestamps")
            except ValueError as exc:
                return _http_error(400, "invalid_config", str(exc))
            suffix = os.path.splitext(upload.filename or "")[1][:10]
            fd, path = tempfile.mkstemp(prefix="hopstt-", suffix=suffix)

            def copy() -> None:
                with os.fdopen(fd, "wb") as out:
                    upload.file.seek(0)
                    while chunk := upload.file.read(1024 * 1024):
                        out.write(chunk)

            await asyncio.to_thread(copy)
            params = BatchParams(model, language, channels, word_ts, priority)
            ready = (params, path)
            path = None
            return ready
        finally:
            if upload is not None and not isinstance(upload, str):
                await upload.close()
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    def _submit(params: BatchParams, path: str, kind: str, max_seconds: float | None):
        rt = runtime()
        try:
            return rt.batch.submit(params, path, kind=kind, max_seconds=max_seconds)
        except QueueFull:
            os.unlink(path)
            rt.metrics.batch_jobs.labels(kind, "rejected").inc()
            return _http_error(429, "queue_full", "batch queue is full", retryable=True)
        except LivePriority:
            os.unlink(path)
            rt.metrics.batch_jobs.labels(kind, "rejected").inc()
            return _http_error(503, "live_priority", "live utilisation is high; batch paused", retryable=True)

    @app.post("/v1/transcriptions")
    async def transcriptions(request: Request):
        parsed = await _parse_batch(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        params, path = parsed
        job = _submit(params, path, "sync", settings.sync_max_seconds)
        if isinstance(job, JSONResponse):
            return job
        try:
            await job.done.wait()
        except asyncio.CancelledError:
            runtime().batch.cancel(job.id)
            raise
        runtime().batch.jobs.pop(job.id, None)
        if job.status == "succeeded":
            return job.result
        err = job.error or {"code": "internal", "retryable": True}
        if err.get("code") == "too_long":
            return _http_error(413, "too_long", "audio exceeds HOPSTT_SYNC_MAX_SECONDS; use /v1/jobs")
        if err.get("code") == "invalid_audio":
            return _http_error(400, "invalid_audio", err.get("message", "could not decode audio"))
        if job.status == "cancelled" or err.get("code") == "draining":
            return _http_error(503, "draining", "request was cancelled", retryable=True)
        return _http_error(503, err.get("code", "model_error"), "inference failed", retryable=True)

    @app.post("/v1/jobs")
    async def create_job(request: Request):
        parsed = await _parse_batch(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        params, path = parsed
        job = _submit(params, path, "job", None)
        if isinstance(job, JSONResponse):
            return job
        return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status})

    def _job_guard(request: Request) -> JSONResponse | None:
        if not settings.serves_batch:
            return _http_error(404, "not_found", "batch is not served by this worker")
        if not authorized(request):
            return _http_error(401, "auth_failed", "invalid or missing API key")
        return None

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        if (err := _job_guard(request)) is not None:
            return err
        job = runtime().batch.get(job_id)
        if job is None:
            return _http_error(404, "not_found", "unknown or expired job")
        return job.public()

    @app.delete("/v1/jobs/{job_id}")
    async def delete_job(job_id: str, request: Request):
        if (err := _job_guard(request)) is not None:
            return err
        job = runtime().batch.cancel(job_id)
        if job is None:
            return _http_error(404, "not_found", "unknown or expired job")
        return job.public()

    return app


def _app_from_env() -> FastAPI:
    try:
        return create_app()
    except ConfigError as exc:
        raise SystemExit(f"hopstt: {exc}") from None


# ==========================================================================
# Entrypoint with drain-on-SIGTERM
# ==========================================================================
def main() -> None:  # pragma: no cover - exercised in deployment
    import uvicorn

    try:
        settings = Settings.from_env()
        settings.validate()
    except ConfigError as exc:
        raise SystemExit(f"hopstt: {exc}") from None
    app = create_app(settings)
    host = os.getenv("HOPSTT_HOST", "0.0.0.0")
    port = int(os.getenv("HOPSTT_PORT", "8790"))
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        workers=1,
        lifespan="on",
        ws_max_size=max(settings.max_frame_bytes * 2, 1 << 20),
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        log_level=os.getenv("HOPSTT_LOG_LEVEL", "info").lower(),
        access_log=False,
    )

    class DrainingServer(uvicorn.Server):
        """First SIGTERM/SIGINT drains (PROTOCOL §5); a second one exits now."""

        _drain_started = False

        def handle_exit(self, sig, frame):
            rt = getattr(app.state, "runtime", None)
            if self._drain_started or rt is None:
                return super().handle_exit(sig, frame)
            self._drain_started = True
            loop = asyncio.get_event_loop()

            def done() -> None:
                self.should_exit = True

            loop.call_soon_threadsafe(lambda: rt.begin_drain(on_done=done))

    server = DrainingServer(config)
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
