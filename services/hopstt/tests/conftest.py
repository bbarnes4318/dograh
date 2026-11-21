"""Shared fixtures: a real uvicorn server in a thread (so WebSocket tests get
real sockets, timeouts and forced disconnects) running the stub engines."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import threading
import time
from dataclasses import dataclass

import httpx
import numpy as np
import pytest

from hopstt.config import Settings

API_KEY = "test-key-0123456789abcdef"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
REAL = os.getenv("HOPSTT_REAL_MODEL_TESTS") == "1"


def pytest_collection_modifyitems(config, items):
    if REAL:
        return
    skip = pytest.mark.skip(reason="set HOPSTT_REAL_MODEL_TESTS=1 to run real-model tests")
    for item in items:
        if "gpu" in item.keywords or "real_model" in item.keywords:
            item.add_marker(skip)


def make_settings(**overrides) -> Settings:
    base = dict(
        role="both",
        worker_id="test-worker",
        api_key=API_KEY,
        allowed_models=("stub-streaming", "stub-batch"),
        default_streaming_model="stub-streaming",
        default_batch_model="stub-batch",
        device="cpu",
        model_cache="/nonexistent",
        max_streams=32,
        config_timeout_s=2.0,
        idle_timeout_s=10.0,
        drain_timeout_s=5.0,
        job_retry_backoff_s=0.01,
    )
    base.update(overrides)
    return Settings(**base)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Server:
    url: str
    ws_url: str
    app: object
    settings: Settings

    @property
    def runtime(self):
        return self.app.state.runtime

    def engine(self, model_id: str = "stub-streaming"):
        return self.runtime.engines[model_id]

    def http(self, auth: bool = True) -> httpx.Client:
        headers = {"Authorization": f"Bearer {API_KEY}"} if auth else {}
        return httpx.Client(base_url=self.url, headers=headers, timeout=30)

    def call(self, fn, *args):
        """Run a coroutine function on the server's event loop."""
        import asyncio

        fut = asyncio.run_coroutine_threadsafe(fn(*args), self._loop)
        return fut.result(10)


@contextlib.contextmanager
def run_server(settings: Settings):
    import asyncio

    import uvicorn

    from hopstt.server import create_app

    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", ws_max_size=1 << 22)
    server = uvicorn.Server(config)
    loop_box: dict = {}

    def target():
        loop = asyncio.new_event_loop()
        loop_box["loop"] = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    srv = Server(f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}", app, settings)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            r = httpx.get(srv.url + "/readyz", timeout=1)
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.02)
    else:
        server.should_exit = True
        raise RuntimeError("server did not become ready")
    srv._loop = loop_box["loop"]
    try:
        yield srv
    finally:
        server.should_exit = True
        thread.join(15)


@pytest.fixture
def server_factory():
    stack = contextlib.ExitStack()

    def factory(**overrides) -> Server:
        return stack.enter_context(run_server(make_settings(**overrides)))

    with stack:
        yield factory


@pytest.fixture
def server(server_factory) -> Server:
    return server_factory()


# ---------------------------------------------------------------------------
# audio helpers
# ---------------------------------------------------------------------------
def tone(seconds: float, rate: int = 16000, amp: float = 0.3, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float, rate: int = 16000) -> np.ndarray:
    return np.zeros(int(seconds * rate), np.float32)


def pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def frames(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


# ---------------------------------------------------------------------------
# WebSocket client helpers
# ---------------------------------------------------------------------------
def connect(srv: Server, path: str = "/v1/stt/stream", token: str | None = API_KEY, query: str = ""):
    from websockets.sync.client import connect as ws_connect

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return ws_connect(srv.ws_url + path + query, additional_headers=headers, open_timeout=5, max_size=None)


def config(**overrides) -> str:
    msg = {
        "type": "config",
        "model": "stub-streaming",
        "language": "en",
        "sample_rate": 16000,
        "encoding": "pcm_s16le",
        "chunk_ms": 80,
        "interim_results": True,
        "session_id": "call-123",
        "generation": 0,
        "stream_offset_ms": 0,
    }
    msg.update(overrides)
    return json.dumps(msg)


def recv(ws, timeout: float = 5.0) -> dict:
    return json.loads(ws.recv(timeout=timeout))


def recv_until(ws, predicate, timeout: float = 10.0) -> list[dict]:
    """Collect messages until ``predicate(msg)`` is true (inclusive)."""
    out = []
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(f"no matching message; got {out}")
        msg = recv(ws, remaining)
        out.append(msg)
        if predicate(msg):
            return out


def close_code(ws, timeout: float = 5.0) -> int | None:
    """Drain until the server closes; return the close code."""
    from websockets.exceptions import ConnectionClosed

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ws.recv(timeout=max(0.01, deadline - time.time()))
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else None
    except TimeoutError:
        return None
    return None


def open_stream(srv: Server, **cfg):
    ws = connect(srv)
    ws.send(config(**cfg))
    ready = recv(ws)
    assert ready["type"] == "ready", ready
    return ws, ready


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
