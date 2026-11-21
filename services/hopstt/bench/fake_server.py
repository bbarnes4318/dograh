"""A tiny in-process FAKE of the HopSTT protocol, for tests and dry runs only.

It is NOT the real service and does no speech recognition: it "transcribes" an
item by looking up a scripted text keyed by the WS ``correlation_id`` (the bench
sends the item id) or the uploaded file name stem. Non-silent audio reveals one
word per ``word_ms`` as interims (after server-side ``chunk_ms`` buffering);
``finalize`` returns the full text after ``final_delay_ms``. Admission control
(``max_streams``), batch jobs, ``/v1/capacity`` and ``/metrics`` are emulated
so the runners and the capacity test can be exercised end to end.

    python -m bench.fake_server --port 8790 --transcripts scripted.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import numpy as np
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .audio import ulaw2lin

MODELS = [
    {"id": "fake-streaming", "kind": "streaming", "languages": ["en", "es"],
     "chunk_ms": [80, 160, 320, 560, 1120], "loaded": True, "warm": True},
    {"id": "fake-batch", "kind": "batch", "languages": ["en", "es"], "chunk_ms": [],
     "loaded": True, "warm": True},
]


class FakeHopSTT:
    def __init__(self, transcripts: dict[str, str] | None = None, *, api_key: str = "test-key",
                 max_streams: int = 100, word_ms: int = 200, final_delay_ms: int = 20,
                 revise: bool = False, hallucination: str | None = None, silence_rms: float = 100.0,
                 batch_delay_ms: int = 10, batch_concurrency: int = 2, batch_queue_limit: int = 100,
                 drop_after_ms: int | None = None, load_delay_ms_per_stream: float = 0.0):
        self.transcripts = dict(transcripts or {})
        self.api_key = api_key
        self.max_streams = max_streams
        self.word_ms = word_ms
        self.final_delay_ms = final_delay_ms
        self.revise = revise
        self.hallucination = hallucination
        self.silence_rms = silence_rms
        self.batch_delay_ms = batch_delay_ms
        self.batch_sem = asyncio.Semaphore(batch_concurrency)
        self.batch_concurrency = batch_concurrency
        self.batch_queue_limit = batch_queue_limit
        self.drop_after_ms = drop_after_ms
        self.load_delay_ms_per_stream = load_delay_ms_per_stream
        self.active = 0
        self.counters = {"streams_total": 0, "capacity_rejections": 0, "finals_total": 0,
                         "batch_requests_total": 0}
        self.configs: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.queued = 0
        self.running = 0
        self._ws_server = None
        self._http_server = None
        self.http_url = self.ws_url = ""

    # -- lifecycle ---------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", http_port: int = 0, ws_port: int = 0) -> "FakeHopSTT":
        self._ws_server = await serve(self._ws_handler, host, ws_port, max_size=None)
        wport = self._ws_server.sockets[0].getsockname()[1]
        self._http_server = await asyncio.start_server(self._http_handler, host, http_port)
        hport = self._http_server.sockets[0].getsockname()[1]
        self.http_url = f"http://{host}:{hport}"
        self.ws_url = f"ws://{host}:{wport}/v1/stt/stream"
        return self

    async def stop(self) -> None:
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http_server:
            self._http_server.close()
            await self._http_server.wait_closed()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    # -- helpers --------------------------------------------------------------------

    def _text_for(self, key: str | None) -> str:
        return self.transcripts.get(key or "", "")

    def _is_speech(self, pcm: np.ndarray) -> bool:
        if pcm.size == 0:
            return False
        return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) > self.silence_rms

    # -- WebSocket ----------------------------------------------------------------------

    async def _ws_handler(self, ws) -> None:
        auth = ws.request.headers.get("Authorization", "")
        seq = 0

        async def send(msg: dict) -> None:
            nonlocal seq
            seq += 1
            msg.setdefault("seq", seq)
            await ws.send(json.dumps(msg))

        async def fail(code: str, close: int, retryable: bool, fatal: bool) -> None:
            await send({"type": "error", "code": code, "message": code, "retryable": retryable, "fatal": fatal})
            await ws.close(close, code)

        if auth != f"Bearer {self.api_key}":
            await fail("auth_failed", 1008, False, True)
            return
        try:
            cfg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except (asyncio.TimeoutError, json.JSONDecodeError, ConnectionClosed):
            try:
                await fail("config_timeout", 1008, True, False)
            except ConnectionClosed:
                pass
            return
        self.configs.append(cfg)
        model = cfg.get("model", "fake-streaming")
        spec = next((m for m in MODELS if m["id"] == model and m["kind"] == "streaming"), None)
        rate, enc = cfg.get("sample_rate", 8000), cfg.get("encoding", "pcm_s16le")
        chunk_ms = cfg.get("chunk_ms", 320)
        if (spec is None or rate not in (8000, 16000) or enc not in ("pcm_s16le", "mulaw", "alaw")
                or chunk_ms not in spec["chunk_ms"] or cfg.get("language", "en") not in spec["languages"]):
            await fail("invalid_config", 1008, False, True)
            return
        if self.active >= self.max_streams:
            self.counters["capacity_rejections"] += 1
            await fail("capacity", 1013, True, False)
            return
        self.active += 1
        self.counters["streams_total"] += 1
        gen = cfg.get("generation", 0)
        offset = int(cfg.get("stream_offset_ms", 0))
        text = self._text_for(cfg.get("correlation_id"))
        words = text.split()
        bps = 2 if enc == "pcm_s16le" else 1
        chunk_bytes = int(rate * chunk_ms / 1000) * bps
        buf = bytearray()
        state = {"received_ms": 0.0, "speech_ms": 0.0, "revealed": 0, "utt": 0, "utt_start": 0.0,
                 "last_interim": None}
        try:
            await send({"type": "ready", "session_id": cfg.get("session_id"), "generation": gen,
                        "model": model, "engine": "fake", "chunk_ms": chunk_ms, "sample_rate": rate,
                        "worker_id": "fake-0"})

            async def process(block: bytes) -> None:
                if enc == "pcm_s16le":
                    pcm = np.frombuffer(block[: len(block) // 2 * 2], dtype="<i2")
                elif enc == "mulaw":
                    pcm = ulaw2lin(block)
                else:
                    pcm = np.full(len(block), 1000, dtype=np.int16)
                ms = len(pcm) * 1000 / rate
                state["received_ms"] += ms
                if self.load_delay_ms_per_stream:
                    await asyncio.sleep(self.load_delay_ms_per_stream * self.active / 1000)
                if self._is_speech(pcm):
                    state["speech_ms"] += ms
                    k = min(len(words), int(state["speech_ms"] // self.word_ms))
                    if k > state["revealed"] and words:
                        state["revealed"] = k
                        shown = list(words[:k])
                        if self.revise and k == 3:  # revise a non-final word on the next interim
                            shown[1] = "wrong"
                        interim = " ".join(shown)
                        if interim != state["last_interim"]:
                            state["last_interim"] = interim
                            await send({"type": "transcript", "text": interim, "is_final": False,
                                        "from_finalize": False, "language": cfg.get("language", "en"),
                                        "utterance_id": state["utt"], "generation": gen,
                                        "start_ms": int(offset + state["utt_start"]),
                                        "end_ms": int(offset + state["received_ms"])})

            async def finalize(from_finalize: bool) -> None:
                if buf:
                    await process(bytes(buf))
                    buf.clear()
                await asyncio.sleep(self.final_delay_ms / 1000)
                out = text if state["speech_ms"] > 0 else (self.hallucination or "")
                if out:
                    self.counters["finals_total"] += 1
                    await send({"type": "transcript", "text": out, "is_final": True,
                                "from_finalize": from_finalize, "confidence": 0.9,
                                "language": cfg.get("language", "en"), "utterance_id": state["utt"],
                                "generation": gen, "start_ms": int(offset + state["utt_start"]),
                                "end_ms": int(offset + state["received_ms"]),
                                "timings": {"finalize_ms": self.final_delay_ms}})
                    state["utt"] += 1
                state.update(speech_ms=0.0, revealed=0, last_interim=None, utt_start=state["received_ms"])

            async for msg in ws:
                if isinstance(msg, bytes):
                    buf.extend(msg)
                    while len(buf) >= chunk_bytes:
                        block = bytes(buf[:chunk_bytes])
                        del buf[:chunk_bytes]
                        await process(block)
                    if self.drop_after_ms is not None and gen == 0 and state["received_ms"] >= self.drop_after_ms:
                        ws.transport.abort()  # simulate a crashed worker / network drop
                        return
                    continue
                ctrl = json.loads(msg)
                typ = ctrl.get("type")
                if typ == "finalize":
                    await finalize(True)
                elif typ == "end_of_stream":
                    await finalize(True)
                    await send({"type": "closed", "reason": "end_of_stream"})
                    await ws.close(1000)
                    return
                elif typ == "cancel":
                    await send({"type": "closed", "reason": "cancel"})
                    await ws.close(1000)
                    return
        except ConnectionClosed:
            pass
        finally:
            self.active -= 1

    # -- HTTP -------------------------------------------------------------------------------

    def capacity(self) -> dict:
        return {"worker_id": "fake-0", "role": "both", "draining": False,
                "live": {"max_streams": self.max_streams, "active_streams": self.active,
                         "available": max(0, self.max_streams - self.active)},
                "batch": {"max_concurrency": self.batch_concurrency, "running": self.running,
                          "queued": self.queued, "queue_limit": self.batch_queue_limit},
                "models": MODELS}

    def metrics_text(self) -> str:
        lines = [
            "# TYPE hopstt_active_streams gauge",
            f'hopstt_active_streams{{role="live"}} {self.active}',
            f"hopstt_batch_queue_depth {self.queued}",
            f"hopstt_streams_total {self.counters['streams_total']}",
            f'hopstt_stream_rejections_total{{reason="capacity"}} {self.counters["capacity_rejections"]}',
            f"hopstt_finals_total {self.counters['finals_total']}",
        ]
        return "\n".join(lines) + "\n"

    async def _batch_transcribe(self, form: dict) -> dict:
        self.queued += 1
        async with self.batch_sem:
            self.queued -= 1
            self.running += 1
            try:
                await asyncio.sleep(self.batch_delay_ms / 1000)
            finally:
                self.running -= 1
        self.counters["batch_requests_total"] += 1
        stem = Path(form.get("_filename") or "x").stem
        audio = form.get("file", b"")
        pcm = np.frombuffer(audio[44:][: (len(audio) - 44) // 2 * 2], dtype="<i2") if len(audio) > 44 else np.zeros(0, np.int16)
        text = self._text_for(stem) if self._is_speech(pcm) else (self.hallucination or "")
        return {"model": form.get("model", "fake-batch"), "engine": "fake", "duration_sec": len(pcm) / 8000,
                "sample_rate": 8000,
                "channels": [{"channel": 0, "language": form.get("language", "en"), "text": text,
                              "segments": []}],
                "timings": {"decode_ms": 1, "queue_ms": 0, "inference_ms": self.batch_delay_ms}}

    async def _run_job(self, job_id: str, form: dict) -> None:
        self.jobs[job_id]["status"] = "running"
        self.jobs[job_id]["attempts"] = 1
        result = await self._batch_transcribe(form)
        self.jobs[job_id].update(status="succeeded", result=result)

    async def _http_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body = await reader.readexactly(int(headers.get("content-length", 0) or 0))
            status, payload, ctype = await self._route(method, path.split("?")[0], headers, body)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
                         f"Connection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _route(self, method: str, path: str, headers: dict, body: bytes):
        js = "application/json"
        if path == "/healthz":
            return 200, {"status": "ok"}, js
        if path == "/readyz":
            return 200, {"ready": True}, js
        if path == "/metrics":
            return 200, self.metrics_text().encode(), "text/plain"
        if headers.get("authorization") != f"Bearer {self.api_key}":
            return 401, {"error": "auth_failed"}, js
        if path == "/v1/capacity":
            return 200, self.capacity(), js
        if path == "/v1/models":
            return 200, {"models": MODELS}, js
        if path in ("/v1/transcriptions", "/v1/jobs") and method == "POST":
            form = self._parse_form(headers.get("content-type", ""), body)
            if path == "/v1/transcriptions":
                return 200, await self._batch_transcribe(form), js
            if self.queued + self.running >= self.batch_queue_limit:
                return 429, {"error": "queue_full"}, js
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"job_id": job_id, "status": "queued", "attempts": 0}
            asyncio.create_task(self._run_job(job_id, form))
            return 202, {"job_id": job_id, "status": "queued"}, js
        if path.startswith("/v1/jobs/"):
            job = self.jobs.get(path.rsplit("/", 1)[1])
            if job is None:
                return 404, {"error": "not_found"}, js
            if method == "DELETE":
                job["status"] = "cancelled"
            return 200, job, js
        return 404, {"error": "not_found"}, js

    @staticmethod
    def _parse_form(ctype: str, body: bytes) -> dict:
        msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        form: dict = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if part.get_filename():
                form["_filename"] = part.get_filename()
                form[name] = payload
            else:
                form[name] = payload.decode()
        return form


class ThreadedFake:
    """Run a FakeHopSTT on its own event loop in a daemon thread (for sync callers / CLI tests)."""

    def __init__(self, transcripts: dict[str, str] | None = None, **kwargs):
        import threading

        self._transcripts, self._kwargs = transcripts, kwargs
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.fake: FakeHopSTT | None = None

    def __enter__(self) -> FakeHopSTT:
        self.thread.start()

        async def make() -> FakeHopSTT:
            return await FakeHopSTT(self._transcripts, **self._kwargs).start()

        self.fake = asyncio.run_coroutine_threadsafe(make(), self.loop).result(10)
        return self.fake

    def __exit__(self, *exc) -> None:
        if self.fake is not None:
            asyncio.run_coroutine_threadsafe(self.fake.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


def main() -> None:  # pragma: no cover - manual smoke runs
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790, help="HTTP port; WS listens on port+1")
    ap.add_argument("--transcripts", help="JSON map item id -> scripted text")
    ap.add_argument("--api-key", default="test-key")
    ap.add_argument("--max-streams", type=int, default=100)
    args = ap.parse_args()
    scripted = json.loads(Path(args.transcripts).read_text()) if args.transcripts else {}

    async def run() -> None:
        fake = FakeHopSTT(scripted, api_key=args.api_key, max_streams=args.max_streams)
        await fake.start(args.host, args.port, args.port + 1)
        print(f"fake HopSTT: HTTP {fake.http_url}  WS {fake.ws_url}")
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
