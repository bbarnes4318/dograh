"""HopSTT protocol clients used by the runners and the capacity test.

Only HopSTT endpoints are contacted (PROTOCOL.md §2 live WS, §3 batch, §1 ops).

Clock: every timestamp is ``loop.time()`` (monotonic) on the client. What each
latency includes is documented on ``StreamRecord`` and in README.md.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from dataclasses import asdict, dataclass, field

import httpx
import numpy as np
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .audio import bytes_per_sample, encode
from .metrics import edit_counts

# ---------------------------------------------------------------------------
# URLs / auth
# ---------------------------------------------------------------------------


def http_base(url: str) -> str:
    url = url.rstrip("/")
    return url.replace("ws://", "http://").replace("wss://", "https://")


def stream_url(url: str, ws_url: str | None = None) -> str:
    if ws_url:
        return ws_url
    base = http_base(url).replace("http://", "ws://").replace("https://", "wss://")
    return base + "/v1/stt/stream"


def auth_headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


# ---------------------------------------------------------------------------
# Live streaming
# ---------------------------------------------------------------------------


@dataclass
class StreamConfig:
    url: str
    api_key: str | None = None
    ws_url: str | None = None
    model: str | None = None
    language: str = "en"
    sample_rate: int = 8000
    encoding: str = "pcm_s16le"
    chunk_ms: int | None = 320          # server-side model chunk (config.chunk_ms)
    frame_ms: int = 20                  # client send frame (Dograh sends ~20 ms frames)
    speed: float = 1.0                  # 1.0 = real time; 0 = as fast as possible
    interim_results: bool = True
    finalize_at: str = "end"            # "end" | "speech_end" (simulated VAD stop)
    vad_stop_ms: int = 200              # Dograh Silero stop_secs=0.2
    final_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    close_timeout_s: float = 2.0
    max_reconnects: int = 3
    recovery_deadline_s: float = 10.0
    replay_cap_ms: int = 30000


@dataclass
class StreamRecord:
    """Per-stream measurements. All *_ms are client-clock milliseconds.

    connect_ms            TCP+TLS+WS upgrade+config -> `ready` (first connection)
    ttfp_ms               first speech frame sent -> first non-empty transcript
                          (includes network, server chunk buffering = chunk_ms,
                          inference, queue wait)
    finalize_to_final_ms  `finalize` sent -> final received (end-of-speech ->
                          final when finalize_at=end/speech_end; VAD delay NOT
                          included because finalize is explicit)
    speech_end_to_final_ms last speech frame sent -> final (adds the simulated
                          VAD stop delay and remaining frame pacing)
    first_partial_to_final_ms / last_partial_to_final_ms  within the final's utterance
    revision_rate         fraction of successive interims that changed a word
                          other than the last one of the previous interim
    partial_edit_mean     mean token edit distance between successive interims
    wall_rtf              (final received - first audio sent) / audio seconds
    """

    id: str
    model: str | None
    chunk_ms: int | None
    audio_s: float
    ok: bool = False
    error_code: str | None = None
    error_message: str | None = None
    errors: list[str] = field(default_factory=list)
    hypothesis: str = ""
    n_finals: int = 0
    n_interims: int = 0
    connect_ms: float | None = None
    ttfp_ms: float | None = None
    finalize_to_final_ms: float | None = None
    speech_end_to_final_ms: float | None = None
    first_partial_to_final_ms: float | None = None
    last_partial_to_final_ms: float | None = None
    revision_rate: float | None = None
    partial_edit_mean: float | None = None
    final_vs_last_partial_edit: float | None = None
    wall_rtf: float | None = None
    no_final: bool = False
    dropped: bool = False               # socket closed abnormally without an error message
    killed: bool = False                # client deliberately aborted (recovery test)
    reconnects: int = 0
    recovered: bool | None = None
    recovery_ms: float | None = None
    duplicate_finals: int = 0
    stale_dropped: int = 0
    server_timings: list[dict] = field(default_factory=list)

    def to_row(self, include_text: bool = True) -> dict:
        d = asdict(self)
        if not include_text:
            d.pop("hypothesis", None)
        return d


def _simple_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def stability(interims: list[str], final: str | None) -> tuple[float | None, float | None, float | None]:
    revised = pairs = 0
    edits: list[int] = []
    toks = [_simple_tokens(t) for t in interims]
    for prev, cur in zip(toks, toks[1:], strict=False):
        pairs += 1
        stable = prev[:-1]
        if cur[: len(stable)] != stable:
            revised += 1
        edits.append(edit_counts(prev, cur).errors)
    fin = None
    if final is not None and toks:
        fin = float(edit_counts(toks[-1], _simple_tokens(final)).errors)
    return (revised / pairs if pairs else None, float(np.mean(edits)) if edits else None, fin)


class _Conn:
    def __init__(self, ws, generation: int):
        self.ws = ws
        self.generation = generation
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()        # `closed` message received
        self.gone = asyncio.Event()          # socket finished (any reason)
        self.ready_msg: dict | None = None
        self.error: dict | None = None
        self.task: asyncio.Task | None = None


class StreamSession:
    """One logical stream (one item), possibly spanning reconnects (PROTOCOL §2.5)."""

    def __init__(self, cfg: StreamConfig, samples: np.ndarray, *, item_id: str,
                 speech_start_s: float | None = None, speech_end_s: float | None = None,
                 kill_at_s: float | None = None, session_id: str | None = None):
        self.cfg = cfg
        self.samples = np.asarray(samples, dtype=np.int16)
        self.sr = cfg.sample_rate
        self.item_id = item_id
        self.session_id = session_id or uuid.uuid4().hex
        self.kill_at = None if kill_at_s is None else int(kill_at_s * self.sr)
        self.speech_start = int((speech_start_s or 0.0) * self.sr)
        n = len(self.samples)
        speech_end = n if speech_end_s is None else min(n, int(speech_end_s * self.sr))
        self.speech_end = max(speech_end, self.speech_start)
        if cfg.finalize_at == "speech_end" and speech_end_s is not None:
            self.finalize_sample = min(n, self.speech_end + int(cfg.vad_stop_ms * self.sr / 1000))
        else:
            self.finalize_sample = n
        self.rec = StreamRecord(id=item_id, model=cfg.model, chunk_ms=cfg.chunk_ms, audio_s=n / self.sr)
        self.loop = asyncio.get_running_loop()
        self.generation = -1
        self.conn: _Conn | None = None
        self.pos = 0                       # samples handed to the socket so far
        self.committed_ms = 0
        self.finals: list[dict] = []       # accepted finals
        self.seen_utt: set[tuple[int, int]] = set()
        self.interims: dict[tuple[int, int], list[tuple[float, str]]] = {}
        self.first_text_t: float | None = None
        self.first_speech_t: float | None = None
        self.last_speech_t: float | None = None
        self.finalize_t: float | None = None
        self.final_t: float | None = None
        self.final_event = asyncio.Event()
        self.fatal = False

    # -- connection management ------------------------------------------------

    def _config(self, offset_ms: int) -> dict:
        c = self.cfg
        cfg = {"type": "config", "language": c.language, "sample_rate": c.sample_rate,
               "encoding": c.encoding, "interim_results": c.interim_results, "vad_events": False,
               "endpointing": {"mode": "client"}, "session_id": self.session_id,
               "generation": self.generation, "stream_offset_ms": offset_ms,
               "correlation_id": self.item_id}
        if c.model:
            cfg["model"] = c.model
        if c.chunk_ms:
            cfg["chunk_ms"] = c.chunk_ms
        return cfg

    async def _open(self, offset_ms: int) -> _Conn:
        url = stream_url(self.cfg.url, self.cfg.ws_url)
        ws = await asyncio.wait_for(
            ws_connect(url, additional_headers=auth_headers(self.cfg.api_key), max_size=None,
                       open_timeout=self.cfg.connect_timeout_s, close_timeout=self.cfg.close_timeout_s),
            self.cfg.connect_timeout_s)
        conn = _Conn(ws, self.generation)
        conn.task = asyncio.create_task(self._receive(conn))
        try:
            await ws.send(json.dumps(self._config(offset_ms)))
        except ConnectionClosed:
            pass  # server rejected before reading config (auth/capacity); the error is in conn.error
        done, _ = await asyncio.wait(
            [asyncio.create_task(conn.ready.wait()), asyncio.create_task(conn.gone.wait())],
            timeout=self.cfg.connect_timeout_s, return_when=asyncio.FIRST_COMPLETED)
        for t in _:
            t.cancel()
        if not conn.ready.is_set():
            code = (conn.error or {}).get("code") or ("closed" if conn.gone.is_set() else "ready_timeout")
            await self._close_quietly(conn)
            raise _StreamError(code, (conn.error or {}).get("message"), conn.error)
        return conn

    async def _close_quietly(self, conn: _Conn) -> None:
        try:
            await asyncio.wait_for(conn.ws.close(), self.cfg.close_timeout_s)
        except Exception:
            pass
        if conn.task:
            conn.task.cancel()

    async def _receive(self, conn: _Conn) -> None:
        try:
            async for raw in conn.ws:
                if isinstance(raw, bytes):
                    continue
                t = self.loop.time()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                gen = msg.get("generation")
                if gen is not None and gen != self.generation:
                    self.rec.stale_dropped += 1
                    continue
                typ = msg.get("type")
                if typ == "ready":
                    conn.ready_msg = msg
                    conn.ready.set()
                elif typ == "transcript":
                    self._on_transcript(msg, t)
                elif typ == "error":
                    conn.error = msg
                    self.rec.errors.append(str(msg.get("code")))
                    if msg.get("fatal"):
                        self.fatal = True
                elif typ == "closed":
                    conn.closed.set()
        except ConnectionClosed:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self.rec.errors.append(f"receiver:{type(exc).__name__}")
        finally:
            conn.gone.set()

    def _on_transcript(self, msg: dict, t: float) -> None:
        text = (msg.get("text") or "").strip()
        key = (self.generation, int(msg.get("utterance_id") or 0))
        if text and self.first_text_t is None:
            self.first_text_t = t
        if not msg.get("is_final"):
            self.rec.n_interims += 1
            if text:
                self.interims.setdefault(key, []).append((t, text))
            return
        start_ms, end_ms = msg.get("start_ms"), msg.get("end_ms")
        if end_ms is not None and self.committed_ms and end_ms <= self.committed_ms:
            self.rec.stale_dropped += 1  # PROTOCOL §2.5 step 4: already committed
            return
        if key in self.seen_utt:
            self.rec.duplicate_finals += 1  # protocol violation: two finals for one utterance
        self.seen_utt.add(key)
        if start_ms is not None and end_ms is not None and end_ms > start_ms:
            for f in self.finals:
                if f.get("start_ms") is None or f.get("end_ms") is None:
                    continue
                overlap = min(end_ms, f["end_ms"]) - max(start_ms, f["start_ms"])
                if overlap > 0.5 * (end_ms - start_ms) and f["generation"] != self.generation:
                    self.rec.duplicate_finals += 1
                    break
        self.finals.append({"text": text, "t": t, "start_ms": start_ms, "end_ms": end_ms,
                            "generation": self.generation, "key": key})
        if end_ms is not None:
            self.committed_ms = max(self.committed_ms, int(end_ms))
        if isinstance(msg.get("timings"), dict):
            self.rec.server_timings.append(msg["timings"])
        if self.finalize_t is not None and t >= self.finalize_t and self.final_t is None:
            self.final_t = t
            self.final_event.set()
        elif msg.get("from_finalize") and self.final_t is None:
            self.final_t = t
            self.final_event.set()

    async def _connect_with_retry(self, first: bool) -> bool:
        deadline = self.loop.time() + self.cfg.recovery_deadline_s
        attempt = 0
        while True:
            self.generation += 1
            committed_sample = int(self.committed_ms * self.sr / 1000)
            replay_from = max(committed_sample, self.pos - int(self.cfg.replay_cap_ms * self.sr / 1000))
            offset_ms = int(replay_from * 1000 / self.sr)
            t0 = self.loop.time()
            try:
                self.conn = await self._open(offset_ms)
                if first:
                    self.rec.connect_ms = (self.loop.time() - t0) * 1000
                if replay_from < self.pos:
                    await self._send_samples(replay_from, self.pos)
                return True
            except _StreamError as exc:
                self.rec.errors.append(exc.code)
                self.rec.error_code, self.rec.error_message = exc.code, exc.message
                retryable = (exc.payload or {}).get("retryable", exc.code in ("closed", "ready_timeout"))
                if (exc.payload or {}).get("fatal") or not retryable:
                    self.fatal = True
                    return False
            except (OSError, asyncio.TimeoutError, InvalidStatus, ConnectionClosed) as exc:
                code = f"connect:{type(exc).__name__}"
                self.rec.errors.append(code)
                self.rec.error_code = code
            attempt += 1
            if first or attempt > self.cfg.max_reconnects or self.loop.time() >= deadline:
                return False
            delay = min(0.1 * 2 ** (attempt - 1), 2.0) * (0.5 + random.random())
            await asyncio.sleep(min(delay, max(0.0, deadline - self.loop.time())))

    async def _send_samples(self, a: int, b: int) -> None:
        data = encode(self.samples[a:b], self.cfg.encoding)
        step = 32768 // bytes_per_sample(self.cfg.encoding) * bytes_per_sample(self.cfg.encoding)
        for i in range(0, len(data), step):
            await self.conn.ws.send(data[i:i + step])

    async def _reconnect(self) -> bool:
        t_lost = self.loop.time()
        if self.conn is not None:
            await self._close_quietly(self.conn)
        if self.cfg.max_reconnects <= 0:
            return False
        self.rec.reconnects += 1
        ok = await self._connect_with_retry(first=False)
        if ok:
            self.rec.recovery_ms = (self.loop.time() - t_lost) * 1000
        return ok

    def _abort(self) -> None:
        transport = getattr(self.conn.ws, "transport", None)
        if transport is not None:
            transport.abort()

    # -- main ---------------------------------------------------------------------

    async def run(self) -> StreamRecord:
        rec = self.rec
        try:
            if not await self._connect_with_retry(first=True):
                self.fatal = True
                self._compute()
                return rec
            await self._stream_audio()
            if not self.fatal:
                await self._finalize_and_close()
        except Exception as exc:
            rec.errors.append(f"client:{type(exc).__name__}")
            rec.error_code = rec.error_code or f"client:{type(exc).__name__}"
        finally:
            if self.conn is not None:
                await self._close_quietly(self.conn)
        self._compute()
        return rec

    async def _stream_audio(self) -> None:
        cfg = self.cfg
        frame = max(1, int(self.sr * cfg.frame_ms / 1000))
        t0 = self.loop.time()
        self.t_start = t0
        k = 0
        while self.pos < self.finalize_sample and not self.fatal:
            if self.kill_at is not None and not self.rec.killed and self.pos >= self.kill_at:
                self.rec.killed = True
                self._abort()
                await asyncio.sleep(0)
                if not await self._reconnect():
                    self.rec.recovered = False
                    self.fatal = True
                    return
            elif self.conn.gone.is_set():
                if self.conn.error is None:
                    self.rec.dropped = True
                if (self.conn.error or {}).get("fatal") or not await self._reconnect():
                    self.fatal = True
                    return
            if cfg.speed > 0:
                due = t0 + k * cfg.frame_ms / 1000 / cfg.speed
                delay = due - self.loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            end = min(self.pos + frame, self.finalize_sample)
            try:
                await self.conn.ws.send(encode(self.samples[self.pos:end], cfg.encoding))
            except ConnectionClosed:
                continue  # handled at the top of the loop (gone is set)
            now = self.loop.time()
            if self.first_speech_t is None and end > self.speech_start:
                self.first_speech_t = now
            if self.last_speech_t is None and end >= self.speech_end:
                self.last_speech_t = now
            self.pos = end
            k += 1
            if cfg.speed <= 0 and k % 50 == 0:
                await asyncio.sleep(0)
        if self.last_speech_t is None:
            self.last_speech_t = self.loop.time()

    async def _finalize_and_close(self) -> None:
        for _ in range(self.cfg.max_reconnects + 1):
            try:
                self.finalize_t = self.loop.time()
                await self.conn.ws.send(json.dumps({"type": "finalize"}))
                done, pending = await asyncio.wait(
                    [asyncio.create_task(self.final_event.wait()), asyncio.create_task(self.conn.gone.wait())],
                    timeout=self.cfg.final_timeout_s, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
            except ConnectionClosed:
                pass
            if self.final_event.is_set() or not self.conn.gone.is_set():
                break
            if self.conn.error is None:
                self.rec.dropped = True
            if (self.conn.error or {}).get("fatal") or not await self._reconnect():
                self.fatal = True
                return
        if not self.final_event.is_set():
            self.rec.no_final = True
        try:
            await self.conn.ws.send(json.dumps({"type": "end_of_stream"}))
            await asyncio.wait_for(self.conn.closed.wait(), self.cfg.close_timeout_s)
        except (ConnectionClosed, asyncio.TimeoutError):
            pass

    def _compute(self) -> None:
        rec = self.rec
        finals = sorted(self.finals, key=lambda f: (f["start_ms"] if f["start_ms"] is not None else 1e18, f["t"]))
        rec.hypothesis = " ".join(f["text"] for f in finals if f["text"]).strip()
        rec.n_finals = len(self.finals)
        ms = lambda a, b: None if a is None or b is None else round((b - a) * 1000, 2)  # noqa: E731
        rec.ttfp_ms = ms(self.first_speech_t, self.first_text_t)
        rec.finalize_to_final_ms = ms(self.finalize_t, self.final_t)
        rec.speech_end_to_final_ms = ms(self.last_speech_t, self.final_t)
        if self.final_t is not None and getattr(self, "t_start", None) is not None and rec.audio_s > 0:
            rec.wall_rtf = round((self.final_t - self.t_start) / rec.audio_s, 4)
        final = next((f for f in self.finals if f["t"] == self.final_t), None)
        if final is not None:
            its = self.interims.get(final["key"], [])
            if its:
                rec.first_partial_to_final_ms = ms(its[0][0], final["t"])
                rec.last_partial_to_final_ms = ms(its[-1][0], final["t"])
        rr, pe, fe = None, None, None
        revs, edits, fins = [], [], []
        for key, its in self.interims.items():
            fin_text = next((f["text"] for f in self.finals if f["key"] == key), None)
            r, e, f = stability([x[1] for x in its], fin_text)
            n_pairs = max(0, len(its) - 1)
            if r is not None:
                revs.append((r, n_pairs))
                edits.append((e, n_pairs))
            if f is not None:
                fins.append(f)
        if revs:
            total = sum(n for _, n in revs)
            rr = sum(r * n for r, n in revs) / total
            pe = sum(e * n for e, n in edits) / total
        if fins:
            fe = float(np.mean(fins))
        rec.revision_rate, rec.partial_edit_mean, rec.final_vs_last_partial_edit = rr, pe, fe
        if rec.killed:
            rec.recovered = bool(not self.fatal and self.final_t is not None)
        # ok = the stream ran to completion (finalize + end_of_stream) without an
        # unrecovered failure. A missing final is reported separately (no_final):
        # it is expected on silence items.
        rec.ok = not self.fatal
        if self.fatal and rec.error_code is None:
            err = self.conn.error if self.conn is not None else None
            rec.error_code = (err or {}).get("code") or ("dropped" if rec.dropped else "failed")


class _StreamError(Exception):
    def __init__(self, code: str, message: str | None = None, payload: dict | None = None):
        super().__init__(code)
        self.code, self.message, self.payload = code, message, payload


async def stream_once(cfg: StreamConfig, samples: np.ndarray, *, item_id: str, **kw) -> StreamRecord:
    return await StreamSession(cfg, samples, item_id=item_id, **kw).run()


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

TERMINAL_JOB = {"succeeded", "failed", "cancelled"}


def _join_text(result: dict) -> str:
    chans = result.get("channels") or []
    return " ".join((c.get("text") or "").strip() for c in sorted(chans, key=lambda c: c.get("channel", 0))).strip()


async def transcribe(client: httpx.AsyncClient, base_url: str, wav: bytes, *, filename: str,
                     model: str | None, language: str, mode: str = "sync", priority: str = "normal",
                     poll_s: float = 0.2, timeout_s: float = 900.0, max_429_retries: int = 20) -> dict:
    """Transcribe one WAV. Returns {text, timings, client_ms, status, error, attempts_429, queue_wait_ms?}."""
    data = {"language": language, "channels": "mix", "word_timestamps": "false", "priority": priority}
    if model:
        data["model"] = model
    files = {"file": (filename, wav, "audio/wav")}
    base = http_base(base_url)
    t0 = time.perf_counter()
    out: dict = {"text": None, "timings": None, "status": None, "error": None, "attempts_429": 0}
    try:
        if mode == "sync":
            r = await client.post(f"{base}/v1/transcriptions", data=data, files=files, timeout=timeout_s)
            out["status"] = r.status_code
            if r.status_code == 200:
                body = r.json()
                out["text"], out["timings"] = _join_text(body), body.get("timings")
                out["duration_sec"] = body.get("duration_sec")
            else:
                out["error"] = f"http_{r.status_code}"
        else:
            for _ in range(max_429_retries + 1):
                r = await client.post(f"{base}/v1/jobs", data=data, files=files, timeout=60)
                if r.status_code != 429:
                    break
                out["attempts_429"] += 1
                await asyncio.sleep(0.5)
            out["status"] = r.status_code
            if r.status_code != 202:
                out["error"] = f"http_{r.status_code}"
            else:
                job_id = r.json()["job_id"]
                t_submit = time.perf_counter()
                t_running = None
                while True:
                    g = await client.get(f"{base}/v1/jobs/{job_id}", timeout=30)
                    body = g.json()
                    st = body.get("status")
                    if st == "running" and t_running is None:
                        t_running = time.perf_counter()
                    if st in TERMINAL_JOB:
                        break
                    if time.perf_counter() - t0 > timeout_s:
                        out["error"] = "job_timeout"
                        break
                    await asyncio.sleep(poll_s)
                if t_running is not None:
                    out["queue_wait_ms_client"] = round((t_running - t_submit) * 1000, 1)
                if st == "succeeded":
                    res = body.get("result") or {}
                    out["text"], out["timings"] = _join_text(res), res.get("timings")
                    out["duration_sec"] = res.get("duration_sec")
                elif out["error"] is None:
                    out["error"] = f"job_{st}:{body.get('error')}"
                out["job_attempts"] = body.get("attempts")
    except httpx.HTTPError as exc:
        out["error"] = f"http:{type(exc).__name__}"
    out["client_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


# ---------------------------------------------------------------------------
# Ops endpoints
# ---------------------------------------------------------------------------

_PROM_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|[+-]Inf)")


def parse_prometheus(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _PROM_LINE.match(line)
        if m:
            try:
                out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
            except ValueError:
                pass
    return out


async def scrape(client: httpx.AsyncClient, base_url: str, api_key: str | None) -> dict:
    base = http_base(base_url)
    snap: dict = {"t": time.time(), "capacity": None, "metrics": None, "errors": []}
    try:
        r = await client.get(f"{base}/v1/capacity", headers=auth_headers(api_key), timeout=5)
        if r.status_code == 200:
            snap["capacity"] = r.json()
        else:
            snap["errors"].append(f"capacity:http_{r.status_code}")
    except httpx.HTTPError as exc:
        snap["errors"].append(f"capacity:{type(exc).__name__}")
    try:
        r = await client.get(f"{base}/metrics", timeout=5)
        if r.status_code == 200:
            snap["metrics"] = parse_prometheus(r.text)
        else:
            snap["errors"].append(f"metrics:http_{r.status_code}")
    except httpx.HTTPError as exc:
        snap["errors"].append(f"metrics:{type(exc).__name__}")
    return snap
