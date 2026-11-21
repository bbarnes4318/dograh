import asyncio
import threading
import time

import numpy as np

from conftest import make_settings
from hopstt.engines.base import Hypothesis, JobCancelled, StreamingEngine
from hopstt.metrics import Metrics
from hopstt.models import STUB_STREAMING
from hopstt.scheduler import BatchParams, BatchPool, LiveScheduler
from hopstt.session import LiveSession, StreamConfig, WorkItem


class RecordingEngine(StreamingEngine):
    """Records the sequence number embedded in every chunk."""

    name = "recording"

    def __init__(self):
        super().__init__(STUB_STREAMING, None)
        self.seen: dict[int, list[int]] = {}
        self.batches: list[int] = []
        self.freed: list[int] = []
        self.lock = threading.Lock()

    def load(self):
        self.loaded = True

    def new_state(self, language, chunk_ms):
        return {"id": None, "chunk_ms": chunk_ms}

    def step(self, states, chunks):
        self.batches.append(len(states))
        for st, ch in zip(states, chunks):
            self.seen.setdefault(st["sid"], []).append(int(ch[0]))
        return [Hypothesis(text=str(len(self.seen[st["sid"]]))) for st in states]

    def finalize(self, state, tail):
        self.seen.setdefault(state["sid"], []).append(-1)
        return Hypothesis(text="final")

    def free(self, state):
        self.freed.append(state["sid"])

    def batch_key(self, state):
        return state["chunk_ms"]


def test_scheduler_preserves_per_session_order_and_batches():
    async def run():
        loop = asyncio.get_running_loop()
        engine = RecordingEngine()
        settings = make_settings(max_batch_size=8)
        sched = LiveScheduler({"m": engine}, settings, Metrics("both"))
        sessions = []
        for sid in range(12):
            cfg = StreamConfig(model="m", language="en", sample_rate=16000, encoding="pcm_s16le", chunk_ms=80)
            s = LiveSession(cfg, engine, loop, max_queue_ms=10_000)
            sched.register(s)
            sessions.append(s)
        # state is created lazily on the inference thread; tag it with sid
        sid_by_session = {s.key: sid for sid, s in enumerate(sessions)}

        def ensure(session, _orig=sched._ensure_state):
            ok = _orig(session)
            if ok:
                session.state["sid"] = sid_by_session[session.key]
            return ok

        sched._ensure_state = ensure
        for n in range(20):
            for s in sessions:
                s.enqueue(WorkItem(kind="audio", audio=np.full(1280, n, np.float32), audio_ms=80))
        for s in sessions:
            s.enqueue(WorkItem(kind="finalize", audio=np.zeros(0, np.float32)))
        sched.start()
        sched.notify()
        finals = 0
        for s in sessions:
            while True:
                ev = await asyncio.wait_for(s.events.get(), 10)
                if ev.kind == "final":
                    finals += 1
                    break
        for s in sessions:
            sched.unregister(s)
        assert sched.wait_freed()
        sched.stop()
        return engine, finals

    engine, finals = asyncio.run(run())
    assert finals == 12
    for sid in range(12):
        assert engine.seen[sid] == list(range(20)) + [-1]
    assert max(engine.batches) == 8  # capped by max_batch_size
    assert sorted(engine.freed) == list(range(12))


def test_batch_gate_waits_for_live_idle_and_honours_cancel():
    async def run():
        settings = make_settings(role="both")
        live = LiveScheduler({}, settings, Metrics("both"))
        pool = BatchPool({}, settings, Metrics("both"), live, lambda: 0.0)
        from hopstt.scheduler import Job

        job = Job(id="j", params=BatchParams("stub-batch"), path=None)
        gate = pool._gate_for(job)
        live.idle.clear()  # live work pending
        released = threading.Event()

        def worker():
            gate()
            released.set()

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.2)
        blocked = not released.is_set()
        live.idle.set()
        t.join(2)
        job.cancelled = True
        try:
            gate()
            cancelled = False
        except JobCancelled:
            cancelled = True
        return blocked, released.is_set(), cancelled

    blocked, released, cancelled = asyncio.run(run())
    assert blocked, "batch must wait while live work is pending"
    assert released and cancelled


def test_batch_gate_is_open_in_batch_role():
    settings = make_settings(role="batch", allowed_models=("stub-batch",))
    live = LiveScheduler({}, settings, Metrics("batch"))
    live.idle.clear()

    async def run():
        pool = BatchPool({}, settings, Metrics("batch"), live, lambda: 0.0)
        from hopstt.scheduler import Job

        gate = pool._gate_for(Job(id="j", params=BatchParams("stub-batch"), path=None))
        started = time.time()
        gate()
        return time.time() - started

    assert asyncio.run(run()) < 0.05
