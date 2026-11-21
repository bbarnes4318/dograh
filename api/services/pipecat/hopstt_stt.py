"""Pipecat adapter for HopSTT, Hopwhistle's self-hosted streaming STT service.

Implements the live-stream client of ``services/hopstt/PROTOCOL.md`` §2,
including the §2.5 reconnect contract. Lives in the AI Voice runtime; no
Pipecat changes.

Reliability model
-----------------
The pipeline ends the call on *any* upstream ``ErrorFrame``, so this service
never pushes one for a transient problem. It subclasses ``STTService``
directly (not ``WebsocketSTTService``) because Pipecat's websocket reconnect
helpers push a non-fatal ``ErrorFrame`` on every failed attempt.

* Every connection has a ``generation``. Messages from older generations are
  dropped.
* All audio since the last committed point (the ``end_ms`` of the last
  accepted final) is kept in a bounded ring buffer. After a drop the client
  reconnects (same or next endpoint, exponential backoff with jitter, within a
  total recovery deadline), sends ``config`` with ``generation + 1`` and
  ``stream_offset_ms`` = the replay start, replays only the uncommitted audio,
  then resumes live audio. Audio arriving meanwhile lands in the same buffer
  and is sent in order after the replay.
* Finals are deduplicated by ``(generation, utterance_id)`` and dropped when
  they end at or before the committed point.
* Only fatal server errors (``auth_failed``, ``invalid_config``), an HTTP
  401/403 on the handshake, or an outage outlasting the recovery deadline push
  a single ``ErrorFrame(fatal=True)``, which takes the existing graceful
  end-call path.

Logs carry generation, endpoint host, error code and timings only: never
transcript text, audio, or the API key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from pipecat.utils.tracing.service_decorators import traced_stt

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.protocol import State
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("HopSTT requires the 'websockets' package") from exc


STREAM_PATH = "/v1/stt/stream"
FATAL_ERROR_CODES = frozenset({"auth_failed", "invalid_config"})
# Retryable codes that mean "this instance cannot take the stream right now":
# the next attempt prefers another endpoint.
INSTANCE_UNAVAILABLE_CODES = frozenset({"capacity", "draining", "overloaded"})
# Server frame limit is 64 KiB (PROTOCOL §2.4); replay well under it.
MAX_SEND_BYTES = 32_000
DEFAULT_TTFS_P99 = 0.5

# /v1/capacity answers, shared by every call in this process for a few seconds.
_capacity_cache: dict[str, tuple[float, int, bool]] = {}


def parse_endpoints(endpoint: str | Sequence[str] | None) -> list[str]:
    """Split a comma-separated endpoint setting into distinct base URLs."""
    if endpoint is None:
        return []
    parts = endpoint.split(",") if isinstance(endpoint, str) else list(endpoint)
    out: list[str] = []
    for part in parts:
        part = (part or "").strip().rstrip("/")
        if part and part not in out:
            out.append(part)
    return out


def stream_url(endpoint: str) -> str:
    """Normalize a HopSTT base URL to its live-stream WebSocket URL."""
    parsed = urlparse(endpoint.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError("HopSTT endpoint must be an absolute http(s) or ws(s) URL")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1/stt/stream", STREAM_PATH):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunparse(
        parsed._replace(
            scheme=scheme, path=f"{path}{STREAM_PATH}", params="", query="", fragment=""
        )
    )


def http_base_url(endpoint: str) -> str:
    """The HTTP base of an endpoint (for /readyz and /v1/capacity)."""
    parsed = urlparse(stream_url(endpoint))
    scheme = {"ws": "http", "wss": "https"}[parsed.scheme]
    path = parsed.path[: -len(STREAM_PATH)]
    return urlunparse(parsed._replace(scheme=scheme, path=path))


def endpoint_host(endpoint: str) -> str:
    """Host[:port] only, for logs."""
    return urlparse(endpoint).netloc or "?"


def hopstt_session_id(call_identifier: str | None = None) -> str:
    """Opaque per-call session id. Hashed, so no phone number or run id leaks."""
    seed = call_identifier or uuid.uuid4().hex
    return hashlib.sha256(f"hopstt:{seed}".encode()).hexdigest()[:32]


async def fetch_capacity(
    endpoints: Sequence[str],
    api_key: str | None,
    *,
    timeout_s: float = 0.5,
    cache_s: float = 3.0,
) -> dict[str, tuple[int, bool]]:
    """``{endpoint: (available_live_streams, draining)}`` from ``/v1/capacity``.

    Answers are cached per process for ``cache_s``. The whole call is bounded
    by ``timeout_s``; endpoints that did not answer in time are omitted.
    """
    now = time.monotonic()
    result: dict[str, tuple[int, bool]] = {}
    missing = []
    for ep in endpoints:
        cached = _capacity_cache.get(ep)
        if cached and now - cached[0] < cache_s:
            result[ep] = (cached[1], cached[2])
        else:
            missing.append(ep)
    if not missing:
        return result

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def one(session: aiohttp.ClientSession, ep: str):
        async with session.get(
            f"{http_base_url(ep)}/v1/capacity", headers=headers
        ) as resp:
            if resp.status != 200:
                return ep, None
            body = await resp.json(content_type=None)
            live = body.get("live") or {}
            return ep, (int(live.get("available") or 0), bool(body.get("draining")))

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            done = await asyncio.wait_for(
                asyncio.gather(
                    *(one(session, ep) for ep in missing), return_exceptions=True
                ),
                timeout=timeout_s,
            )
    except Exception:
        return result
    stamp = time.monotonic()
    for item in done:
        if isinstance(item, BaseException) or item[1] is None:
            continue
        ep, (available, draining) = item
        _capacity_cache[ep] = (stamp, available, draining)
        result[ep] = (available, draining)
    return result


class _AudioRing:
    """Bounded audio buffer addressed by absolute byte offset in the call.

    ``start``/``end`` are offsets of the oldest kept byte and one past the
    newest. Offsets stay sample-aligned because callers only append whole
    samples and only discard at sample-aligned offsets.
    """

    def __init__(self, max_bytes: int):
        self._max_bytes = max(2, max_bytes - (max_bytes % 2))
        self._chunks: deque[tuple[int, bytes]] = deque()
        self.start = 0
        self.end = 0
        self.dropped_bytes = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append((self.end, data))
        self.end += len(data)
        overflow = self.end - self._max_bytes
        if overflow > self.start:
            self.dropped_bytes += overflow - self.start
            self.discard_before(overflow)

    def discard_before(self, offset: int) -> None:
        offset = min(max(offset, self.start), self.end)
        offset -= offset % 2
        while self._chunks:
            chunk_start, data = self._chunks[0]
            chunk_end = chunk_start + len(data)
            if chunk_end <= offset:
                self._chunks.popleft()
            elif chunk_start < offset:
                self._chunks[0] = (offset, data[offset - chunk_start :])
                break
            else:
                break
        self.start = max(self.start, offset)

    def read(self, pos: int, max_len: int) -> bytes:
        pos = max(pos, self.start)
        out = bytearray()
        for chunk_start, data in self._chunks:
            chunk_end = chunk_start + len(data)
            if chunk_end <= pos:
                continue
            take_from = max(pos, chunk_start) - chunk_start
            out += data[take_from : take_from + max_len - len(out)]
            if len(out) >= max_len:
                break
        return bytes(out)


class _FatalHopSTTError(Exception):
    """A failure that reconnecting cannot fix (bad key, invalid config)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _RetryableHopSTTError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class HopSTTSettings(STTSettings):
    """Runtime settings for HopSTTService (model + language)."""

    pass


class HopSTTService(STTService):
    """Streaming HopSTT client with silent reconnect and failover."""

    Settings = HopSTTSettings

    def __init__(
        self,
        *,
        endpoints: str | Sequence[str],
        api_key: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        sample_rate: int | None = None,
        chunk_ms: int = 320,
        interim_results: bool = True,
        endpointing_mode: str = "client",
        endpointing_silence_ms: int = 500,
        keyterms: list[str] | None = None,
        connect_timeout_s: float = 2.0,
        recovery_deadline_s: float = 5.0,
        max_reconnect_attempts: int = 6,
        backoff_base_s: float = 0.1,
        backoff_max_s: float = 1.0,
        max_replay_ms: int = 10_000,
        keepalive_interval_s: float = 5.0,
        stable_connection_s: float = 2.0,
        capacity_routing: bool = True,
        capacity_timeout_s: float = 0.5,
        capacity_cache_s: float = 3.0,
        settings: HopSTTSettings | None = None,
        ttfs_p99_latency: float | None = DEFAULT_TTFS_P99,
        **kwargs,
    ):
        defaults = HopSTTSettings(model="nemotron-3.5-streaming", language="en")
        if settings is not None:
            defaults.apply_update(settings)
        super().__init__(
            sample_rate=sample_rate,
            settings=defaults,
            ttfs_p99_latency=ttfs_p99_latency,
            **kwargs,
        )
        self._endpoints = parse_endpoints(endpoints)
        if not self._endpoints:
            raise ValueError("HopSTT needs at least one endpoint")
        for ep in self._endpoints:
            stream_url(ep)  # validate early
        self._api_key = api_key or None
        self._session_id = session_id or hopstt_session_id()
        self._correlation_id = correlation_id or self._session_id
        self._chunk_ms = chunk_ms
        self._interim_results = interim_results
        self._endpointing_mode = endpointing_mode
        self._endpointing_silence_ms = endpointing_silence_ms
        self._keyterms = list(keyterms or [])
        self._connect_timeout_s = connect_timeout_s
        self._recovery_deadline_s = recovery_deadline_s
        self._max_reconnect_attempts = max(1, max_reconnect_attempts)
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._max_replay_ms = max_replay_ms
        self._keepalive_interval_s = keepalive_interval_s
        self._stable_connection_s = stable_connection_s
        self._capacity_routing = capacity_routing
        self._capacity_timeout_s = capacity_timeout_s
        self._capacity_cache_s = capacity_cache_s

        # Connection state
        self._ws = None
        self._ws_endpoint: str | None = None
        self._generation = 0
        self._connected_once = False
        self._live = False  # True when live audio may be sent on self._ws
        self._closing = False
        self._fatal_sent = False
        self._recovery_task: asyncio.Task | None = None
        self._receive_tasks: set[asyncio.Task] = set()
        self._keepalive_task_handle: asyncio.Task | None = None
        self._penalized_until: dict[str, float] = {}
        self._last_endpoint_index = -1
        self._outage_started_at: float | None = None
        self._outage_attempts = 0
        self._live_since: float | None = None
        self._pending_finalize = False
        self._closed_event = asyncio.Event()

        # Audio timeline (bytes since call start, as sent to HopSTT)
        self._ring: _AudioRing | None = None
        self._committed_ms = 0
        self._seen_finals: deque[tuple[int, int]] = deque(maxlen=512)
        self._last_seq: dict[int, int] = {}

        # Counters (exposed for tests / shadow metrics)
        self.reconnect_count = 0
        self.dropped_stale_messages = 0
        self.dropped_duplicate_finals = 0

    # ------------------------------------------------------------------ props

    def can_generate_metrics(self) -> bool:
        return True

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def committed_ms(self) -> int:
        return self._committed_ms

    def _bytes_per_ms(self) -> float:
        return (self.sample_rate or 8000) * 2 / 1000

    def _ms_to_byte(self, ms: int | float) -> int:
        samples = int(ms * (self.sample_rate or 8000) / 1000)
        return samples * 2

    def _byte_to_ms(self, offset: int) -> int:
        return int(round(offset / self._bytes_per_ms()))

    # -------------------------------------------------------------- lifecycle

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._closing = False
        if self._ring is None:
            self._ring = _AudioRing(self._ms_to_byte(self._max_replay_ms))
        # Connect in the background: call start never waits on HopSTT. Audio
        # that arrives first is buffered and replayed once connected.
        self._start_recovery("initial_connect", initial=True)
        if self._keepalive_task_handle is None:
            self._keepalive_task_handle = self.create_task(
                self._keepalive_loop(), name="hopstt_keepalive"
            )

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._shutdown(graceful=True)

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._shutdown(graceful=False)

    async def cleanup(self):
        await super().cleanup()
        await self._shutdown(graceful=False)

    async def _shutdown(self, *, graceful: bool) -> None:
        if self._closing and self._ws is None and not self._receive_tasks:
            return
        self._closing = True
        self._live = False
        if self._recovery_task and not self._recovery_task.done():
            await self.cancel_task(self._recovery_task)
        self._recovery_task = None
        if self._keepalive_task_handle:
            await self.cancel_task(self._keepalive_task_handle)
            self._keepalive_task_handle = None
        ws = self._ws
        if ws is not None and ws.state is State.OPEN:
            try:
                if graceful:
                    self._closed_event.clear()
                    await ws.send(json.dumps({"type": "end_of_stream"}))
                    # Give the last final a moment to arrive.
                    try:
                        await asyncio.wait_for(self._closed_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await ws.send(json.dumps({"type": "cancel"}))
            except Exception:
                pass
        await self._close_ws(ws)
        self._ws = None
        for task in list(self._receive_tasks):
            await self.cancel_task(task)
        self._receive_tasks.clear()

    async def _close_ws(self, ws) -> None:
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=1.0)
        except Exception:
            pass

    # ------------------------------------------------------------- frames

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self.start_processing_metrics()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Same as Cartesia/Dograh STT: client VAD stop ends the utterance.
            await self.finalize()

    async def finalize(self) -> None:
        """End the current utterance now; deferred until replay if reconnecting."""
        self.request_finalize()
        if self._live and self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "finalize"}))
            except Exception as exc:
                # The receive loop notices the drop; finalize after replay.
                self._pending_finalize = True
                logger.debug(f"HopSTT finalize send failed: {type(exc).__name__}")
        else:
            self._pending_finalize = True

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self._ring is None:
            self._ring = _AudioRing(self._ms_to_byte(self._max_replay_ms))
        self._ring.append(audio)
        if self._live and self._ws is not None and not self._fatal_sent:
            try:
                await self._ws.send(audio)
            except Exception as exc:
                # Kept in the ring buffer; recovery replays it.
                self._on_connection_lost(self._generation, f"send:{type(exc).__name__}")
        yield None

    async def _update_settings(self, delta: STTSettings):
        changed = await super()._update_settings(delta)
        if changed and ("model" in changed or "language" in changed):
            if self._connected_once and not self._closing:
                logger.info("HopSTT settings changed; reconnecting with new config")
                self._live = False
                self._start_recovery("settings_changed")
        return changed

    # ---------------------------------------------------------- recovery

    def _on_connection_lost(self, generation: int, reason: str) -> None:
        if self._closing or self._fatal_sent or generation != self._generation:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._live = False
        if self._ws_endpoint:
            self._penalize(self._ws_endpoint, 2.0)
        self._start_recovery(reason)

    def _start_recovery(self, reason: str, *, initial: bool = False) -> None:
        if self._closing or self._fatal_sent:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._recovery_task = self.create_task(
            self._recover(reason, initial=initial), name="hopstt_recovery"
        )

    def _penalize(self, endpoint: str, seconds: float) -> None:
        self._penalized_until[endpoint] = max(
            self._penalized_until.get(endpoint, 0.0), time.monotonic() + seconds
        )

    def _pick_endpoint(self) -> str:
        now = time.monotonic()
        n = len(self._endpoints)
        for step in range(1, n + 1):
            idx = (self._last_endpoint_index + step) % n
            if self._penalized_until.get(self._endpoints[idx], 0.0) <= now:
                self._last_endpoint_index = idx
                return self._endpoints[idx]
        # Every instance is penalized: retry the one that recovers soonest.
        idx = min(
            range(n), key=lambda i: self._penalized_until.get(self._endpoints[i], 0.0)
        )
        self._last_endpoint_index = idx
        return self._endpoints[idx]

    async def _rank_endpoints(self) -> None:
        if not self._capacity_routing or len(self._endpoints) < 2:
            return
        capacity = await fetch_capacity(
            self._endpoints,
            self._api_key,
            timeout_s=self._capacity_timeout_s,
            cache_s=self._capacity_cache_s,
        )
        if not capacity:
            return
        order = {ep: i for i, ep in enumerate(self._endpoints)}

        def key(ep: str):
            available, draining = capacity.get(ep, (0, False))
            known = ep in capacity
            return (draining, not known, -available, order[ep])

        self._endpoints.sort(key=key)
        for ep, (available, draining) in capacity.items():
            if draining or available <= 0:
                self._penalize(ep, self._capacity_cache_s)
        self._last_endpoint_index = -1

    def _backoff(self, attempt: int) -> float:
        delay = min(
            self._backoff_max_s, self._backoff_base_s * (2 ** max(0, attempt - 1))
        )
        return delay * random.uniform(0.5, 1.0)

    async def _recover(self, reason: str, *, initial: bool = False) -> None:
        now = time.monotonic()
        # A connection that died right after recovering is the same outage:
        # keep its deadline and attempt count so flapping cannot loop forever.
        same_outage = (
            self._outage_started_at is not None
            and self._live_since is not None
            and now - self._live_since < self._stable_connection_s
        )
        if not same_outage:
            self._outage_started_at = now
            self._outage_attempts = 0
        deadline = self._outage_started_at + self._recovery_deadline_s
        old_ws = self._ws
        self._ws = None
        self._live = False
        if old_ws is not None:
            await self._close_ws(old_ws)

        if initial:
            try:
                await self._rank_endpoints()
            except Exception as exc:  # never let routing block the call
                logger.debug(f"HopSTT capacity routing skipped: {type(exc).__name__}")

        first = initial and not self._connected_once
        last_code = reason
        while not self._closing:
            remaining = deadline - time.monotonic()
            if self._outage_attempts >= self._max_reconnect_attempts or remaining <= 0:
                await self._fatal(
                    f"recovery deadline exceeded after {self._outage_attempts} attempts "
                    f"(last: {last_code})"
                )
                return
            if self._outage_attempts > 0:
                await asyncio.sleep(
                    min(self._backoff(self._outage_attempts), remaining)
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    continue
            self._outage_attempts += 1
            if not first:
                self._generation += 1
            first = False
            generation = self._generation
            endpoint = self._pick_endpoint()
            ring = self._ring
            replay_start = max(self._ms_to_byte(self._committed_ms), ring.start)
            replay_start = min(replay_start, ring.end)
            offset_ms = self._byte_to_ms(replay_start)
            t0 = time.monotonic()
            try:
                ws = await asyncio.wait_for(
                    self._open(endpoint, generation, offset_ms),
                    timeout=max(0.05, min(self._connect_timeout_s, remaining)),
                )
            except _FatalHopSTTError as exc:
                await self._fatal(exc.code)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_code = getattr(exc, "code", None) or type(exc).__name__
                hold = 30.0 if last_code == "draining" else 5.0
                self._penalize(endpoint, hold)
                logger.warning(
                    f"HopSTT connect failed: endpoint={endpoint_host(endpoint)} "
                    f"generation={generation} attempt={self._outage_attempts} "
                    f"reason={last_code}"
                )
                continue

            self._ws = ws
            self._ws_endpoint = endpoint
            self._last_seq[generation] = 0
            task = self.create_task(
                self._receive_loop(ws, generation), name=f"hopstt_recv_{generation}"
            )
            self._receive_tasks.add(task)
            task.add_done_callback(self._receive_tasks.discard)

            # Replay the uncommitted audio, then whatever arrived meanwhile.
            try:
                pos = replay_start
                while pos < ring.end:
                    chunk = ring.read(pos, MAX_SEND_BYTES)
                    if not chunk:
                        break
                    await ws.send(chunk)
                    pos += len(chunk)
                if ws.state is not State.OPEN or task.done():
                    raise ConnectionError("closed during replay")
                if self._pending_finalize:
                    self._pending_finalize = False
                    await ws.send(json.dumps({"type": "finalize"}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_code = f"replay:{type(exc).__name__}"
                self._penalize(endpoint, 2.0)
                self._ws = None
                await self._close_ws(ws)
                continue

            # No await between the last replay check and going live, so no
            # audio frame can slip between the replay and live sends.
            self._live = True
            self._live_since = time.monotonic()
            replayed_ms = self._byte_to_ms(ring.end - replay_start)
            if self._connected_once:
                self.reconnect_count += 1
                logger.info(
                    f"HopSTT reconnected: endpoint={endpoint_host(endpoint)} "
                    f"generation={generation} attempts={self._outage_attempts} "
                    f"reason={reason} replayed_ms={replayed_ms} "
                    f"stream_offset_ms={offset_ms} "
                    f"outage_ms={int((time.monotonic() - self._outage_started_at) * 1000)} "
                    f"connect_ms={int((time.monotonic() - t0) * 1000)}"
                )
            else:
                logger.info(
                    f"HopSTT connected: endpoint={endpoint_host(endpoint)} "
                    f"generation={generation} model={self._settings.model}"
                )
            self._connected_once = True
            if ring.dropped_bytes:
                logger.warning(
                    "HopSTT replay buffer overflowed; "
                    f"{self._byte_to_ms(ring.dropped_bytes)} ms of uncommitted audio "
                    "was not replayable"
                )
                ring.dropped_bytes = 0
            return

    async def _open(self, endpoint: str, generation: int, offset_ms: int):
        headers = (
            {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        )
        try:
            ws = await websocket_connect(
                stream_url(endpoint),
                additional_headers=headers,
                open_timeout=self._connect_timeout_s,
                max_size=2**20,
            )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                raise _FatalHopSTTError("auth_failed") from exc
            raise _RetryableHopSTTError(f"http_{status}") from exc
        try:
            await ws.send(json.dumps(self._config_message(generation, offset_ms)))
            raw = await asyncio.wait_for(ws.recv(), timeout=self._connect_timeout_s)
            msg = json.loads(raw) if isinstance(raw, str) else {}
            if msg.get("type") == "error":
                code = str(msg.get("code") or "internal")
                if code in FATAL_ERROR_CODES or msg.get("fatal") is True:
                    raise _FatalHopSTTError(code)
                raise _RetryableHopSTTError(code)
            if msg.get("type") != "ready":
                raise _RetryableHopSTTError("no_ready")
            return ws
        except BaseException:
            await self._close_ws(ws)
            raise

    def _config_message(self, generation: int, offset_ms: int) -> dict:
        language = self._settings.language
        language = getattr(language, "value", language) or "en"
        msg = {
            "type": "config",
            "model": self._settings.model,
            "language": str(language),
            "sample_rate": self.sample_rate,
            "encoding": "pcm_s16le",
            "chunk_ms": self._chunk_ms,
            "interim_results": self._interim_results,
            "vad_events": False,
            "endpointing": {
                "mode": self._endpointing_mode,
                "silence_ms": self._endpointing_silence_ms,
            },
            "session_id": self._session_id,
            "generation": generation,
            "stream_offset_ms": offset_ms,
            "correlation_id": self._correlation_id,
        }
        if self._keyterms:
            msg["keyterms"] = self._keyterms
        return msg

    async def _fatal(self, reason: str) -> None:
        if self._fatal_sent:
            return
        self._fatal_sent = True
        self._live = False
        logger.error(f"HopSTT unavailable, ending call: {reason}")
        await self.push_error(f"HopSTT unavailable: {reason}", fatal=True)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self._keepalive_interval_s)
            ws = self._ws
            if self._live and ws is not None and ws.state is State.OPEN:
                try:
                    await ws.send(json.dumps({"type": "keepalive"}))
                except Exception:
                    pass  # the receive loop handles the drop

    # ------------------------------------------------------------ receive

    async def _receive_loop(self, ws, generation: int) -> None:
        reason = "closed"
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                stop = await self._handle_message(msg, generation)
                if stop:
                    return
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            reason = f"closed:{getattr(exc.rcvd, 'code', None) if exc.rcvd else 'none'}"
        except Exception as exc:
            reason = f"receive:{type(exc).__name__}"
        if not self._closing:
            logger.warning(
                f"HopSTT connection lost: generation={generation} reason={reason}"
            )
        self._on_connection_lost(generation, reason)

    async def _handle_message(self, msg: dict, generation: int) -> bool:
        """Handle one server message. Returns True to stop this receive loop."""
        if generation != self._generation:
            self.dropped_stale_messages += 1
            return True
        msg_gen = msg.get("generation")
        if msg_gen is not None and msg_gen != self._generation:
            self.dropped_stale_messages += 1
            return False
        seq = msg.get("seq")
        if isinstance(seq, int):
            if seq <= self._last_seq.get(generation, 0):
                return False
            self._last_seq[generation] = seq

        msg_type = msg.get("type")
        if msg_type == "transcript":
            await self._handle_transcript(msg, generation)
        elif msg_type == "error":
            code = str(msg.get("code") or "internal")
            if code in FATAL_ERROR_CODES or msg.get("fatal") is True:
                await self._fatal(code)
                return True
            if code in INSTANCE_UNAVAILABLE_CODES and self._ws_endpoint:
                self._penalize(self._ws_endpoint, 30.0 if code == "draining" else 5.0)
            logger.warning(
                f"HopSTT retryable error: generation={generation} code={code}"
            )
            self._on_connection_lost(generation, code)
            return True
        elif msg_type == "closed":
            self._closed_event.set()
            if self._closing:
                return True
        return False

    @traced_stt
    async def _handle_transcription_traced(
        self, transcript: str, is_final: bool, language: Language | None = None
    ):
        """Handle a transcription result with tracing."""
        pass

    def _parse_language(self, code) -> Language | None:
        if not code:
            return None
        try:
            return Language(code)
        except ValueError:
            settings_language = self._settings.language
            return (
                settings_language if isinstance(settings_language, Language) else None
            )

    async def _handle_transcript(self, msg: dict, generation: int) -> None:
        text = msg.get("text") or ""
        if not text:
            return
        is_final = bool(msg.get("is_final"))
        end_ms = msg.get("end_ms")
        start_ms = msg.get("start_ms")
        committed = self._committed_ms
        # Anything lying entirely at/before the committed point was already
        # delivered (replay overlap): drop it.
        if (
            isinstance(end_ms, (int, float))
            and end_ms <= committed
            and (not isinstance(start_ms, (int, float)) or start_ms <= committed)
        ):
            if is_final:
                self.dropped_duplicate_finals += 1
            return
        confidence = msg.get("confidence") or 0.0
        language = self._parse_language(msg.get("language"))
        result = {"confidence": confidence} if confidence else None

        if not is_final:
            await self.push_frame(
                InterimTranscriptionFrame(
                    text=text,
                    user_id=self._user_id,
                    timestamp=time_now_iso8601(),
                    language=language,
                    result=result,
                )
            )
            return

        utterance_id = msg.get("utterance_id")
        if utterance_id is not None:
            key = (generation, int(utterance_id))
            if key in self._seen_finals:
                self.dropped_duplicate_finals += 1
                return
            self._seen_finals.append(key)
        if isinstance(end_ms, (int, float)):
            self._committed_ms = max(self._committed_ms, int(end_ms))
            if self._ring is not None:
                self._ring.discard_before(self._ms_to_byte(self._committed_ms))
        if msg.get("from_finalize"):
            self.confirm_finalize()
        await self.push_frame(
            TranscriptionFrame(
                text=text,
                user_id=self._user_id,
                timestamp=time_now_iso8601(),
                language=language,
                result=result,
            )
        )
        await self._handle_transcription_traced(text, True, language)
        await self.stop_processing_metrics()
