import json
import threading
import time

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed

from conftest import (
    API_KEY,
    close_code,
    config,
    connect,
    frames,
    open_stream,
    pcm16,
    recv,
    recv_until,
    silence,
    tone,
    wait_for,
)

FRAME = 640  # 20 ms of 16 kHz PCM16


def send_audio(ws, audio, frame_bytes=FRAME):
    for f in frames(pcm16(audio), frame_bytes):
        ws.send(f)


def is_final(m):
    return m.get("type") == "transcript" and m.get("is_final")


def finish(ws):
    ws.send(json.dumps({"type": "end_of_stream"}))
    msgs = recv_until(ws, lambda m: m["type"] == "closed")
    return msgs


# --------------------------------------------------------------------------
# handshake, auth, config
# --------------------------------------------------------------------------
@pytest.mark.parametrize("token", [None, "wrong-key-wrong-key-wrong"])
def test_ws_auth_failure(server, token):
    ws = connect(server, token=token)
    msg = recv(ws)
    assert msg["type"] == "error" and msg["code"] == "auth_failed"
    assert msg["retryable"] is False and msg["fatal"] is True
    assert close_code(ws) == 1008


def test_query_token_disabled_by_default(server):
    ws = connect(server, token=None, query=f"?token={API_KEY}")
    assert recv(ws)["code"] == "auth_failed"


def test_query_token_when_enabled(server_factory):
    srv = server_factory(allow_query_token=True)
    ws = connect(srv, token=None, query=f"?token={API_KEY}")
    ws.send(config())
    assert recv(ws)["type"] == "ready"


def test_ready_message_and_alias(server):
    for path in ("/v1/stt/stream", "/api/v1/stt/stream"):
        ws = connect(server, path=path)
        ws.send(config(generation=2, session_id="abc"))
        ready = recv(ws)
        assert ready == {
            "type": "ready",
            "session_id": "abc",
            "generation": 2,
            "model": "stub-streaming",
            "engine": "stub",
            "chunk_ms": 80,
            "sample_rate": 16000,
            "worker_id": "test-worker",
            "seq": 1,
        }
        ws.close()


@pytest.mark.parametrize(
    "override",
    [
        {"model": "nope"},
        {"model": "stub-batch"},
        {"language": "fr"},
        {"sample_rate": 44100},
        {"encoding": "opus"},
        {"chunk_ms": 100},
        {"endpointing": {"mode": "magic"}},
        {"generation": -1},
        {"type": "hello"},
    ],
)
def test_invalid_config(server, override):
    ws = connect(server)
    ws.send(config(**override))
    msg = recv(ws)
    assert msg["type"] == "error" and msg["code"] == "invalid_config", msg
    assert msg["fatal"] is True
    assert close_code(ws) == 1008


def test_binary_first_message_is_invalid(server):
    ws = connect(server)
    ws.send(b"\x00\x01")
    assert recv(ws)["code"] == "invalid_config"


def test_config_timeout(server_factory):
    srv = server_factory(config_timeout_s=0.3)
    ws = connect(srv)
    msg = recv(ws)
    assert msg["code"] == "config_timeout" and msg["retryable"] is True and msg["fatal"] is False
    assert close_code(ws) == 1008


def test_frame_too_large(server_factory):
    srv = server_factory(max_frame_bytes=1024)
    ws, _ = open_stream(srv)
    ws.send(b"\x00" * 2048)
    msg = recv(ws)
    assert msg["code"] == "invalid_config"
    assert close_code(ws) == 1008


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------
def test_partial_then_final_exactly_once_and_seq(server):
    ws, ready = open_stream(server)
    send_audio(ws, tone(1.2))
    send_audio(ws, silence(0.2))
    ws.send(json.dumps({"type": "finalize"}))
    msgs = recv_until(ws, is_final)
    interims = [m for m in msgs if m["type"] == "transcript" and not m["is_final"]]
    final = msgs[-1]
    assert interims, "expected interim results before the final"
    texts = [m["text"] for m in interims]
    assert all(a != b for a, b in zip(texts, texts[1:])), "interims must only be sent on change"
    assert final["from_finalize"] is True and final["utterance_id"] == 1
    assert final["text"].endswith(".") and final["text"][0].isupper()
    assert final["language"] == "en" and "confidence" in final
    assert final["start_ms"] <= final["end_ms"]
    assert final["end_ms"] == 1400  # commit point = all audio received
    # second utterance gets utterance_id + 1; a finalize with no speech -> no final
    ws.send(json.dumps({"type": "finalize"}))
    send_audio(ws, tone(0.6))
    ws.send(json.dumps({"type": "finalize"}))
    msgs2 = recv_until(ws, is_final)
    assert msgs2[-1]["utterance_id"] == 2
    tail = finish(ws)
    assert not any(is_final(m) for m in tail), "no duplicate final after end_of_stream"
    seqs = [ready["seq"]] + [m["seq"] for m in msgs + msgs2 + tail]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[0] == 1
    assert close_code(ws) == 1000


def test_silence_produces_no_final(server):
    ws, _ = open_stream(server)
    send_audio(ws, silence(1.5))
    ws.send(json.dumps({"type": "finalize"}))
    msgs = finish(ws)
    assert [m["type"] for m in msgs] == ["closed"]
    assert msgs[0]["reason"] == "end_of_stream"


def test_interim_results_disabled(server):
    ws, _ = open_stream(server, interim_results=False)
    send_audio(ws, tone(1.0))
    ws.send(json.dumps({"type": "finalize"}))
    msgs = recv_until(ws, is_final)
    assert len(msgs) == 1


def test_end_of_stream_flushes_final(server):
    ws, _ = open_stream(server)
    send_audio(ws, tone(0.9))  # not a multiple of the chunk: tail must be flushed
    msgs = finish(ws)
    finals = [m for m in msgs if is_final(m)]
    assert len(finals) == 1 and finals[0]["from_finalize"] is True
    assert msgs[-1] == {"type": "closed", "reason": "end_of_stream", "seq": msgs[-1]["seq"], "generation": 0}
    assert close_code(ws) == 1000


def test_cancel_drops_audio_without_final(server_factory):
    srv = server_factory(stub_step_latency_ms=50)
    ws, _ = open_stream(srv, interim_results=False)
    send_audio(ws, tone(2.0))
    ws.send(json.dumps({"type": "cancel"}))
    msgs = recv_until(ws, lambda m: m["type"] == "closed")
    assert msgs[-1]["reason"] == "cancel"
    assert not any(m.get("type") == "transcript" for m in msgs)
    assert close_code(ws) == 1000
    assert wait_for(lambda: srv.runtime.active_streams == 0)


def test_generation_and_offset_in_timestamps(server):
    ws, ready = open_stream(server, generation=3, stream_offset_ms=10_000)
    assert ready["generation"] == 3
    send_audio(ws, tone(1.0))
    ws.send(json.dumps({"type": "finalize"}))
    msgs = recv_until(ws, is_final)
    for m in msgs:
        assert m["generation"] == 3
        assert m["start_ms"] >= 10_000
    assert msgs[-1]["end_ms"] == 11_000


def test_mulaw_8k_input(server):
    from hopstt.audio import ULAW_TO_PCM16

    # encode a tone to mu-law by nearest table entry
    x = (tone(1.0, rate=8000) * 32768).astype(np.int32)
    table = ULAW_TO_PCM16.astype(np.int32)
    order = np.argsort(table)
    idx = np.searchsorted(table[order], x).clip(0, 255)
    codes = order[idx].astype(np.uint8).tobytes()
    ws, ready = open_stream(server, sample_rate=8000, encoding="mulaw")
    assert ready["sample_rate"] == 8000
    for f in frames(codes, 160):
        ws.send(f)
    ws.send(json.dumps({"type": "finalize"}))
    final = recv_until(ws, is_final)[-1]
    assert final["text"] and final["end_ms"] == 1000


def test_server_endpointing_and_vad_events(server):
    ws, _ = open_stream(server, vad_events=True, endpointing={"mode": "server", "silence_ms": 300})
    send_audio(ws, silence(0.2))
    send_audio(ws, tone(0.8))
    send_audio(ws, silence(0.6))
    msgs = recv_until(ws, is_final)
    kinds = [m["type"] for m in msgs]
    assert "speech_started" in kinds and "speech_ended" in kinds
    started = next(m for m in msgs if m["type"] == "speech_started")
    ended = next(m for m in msgs if m["type"] == "speech_ended")
    assert 150 <= started["at_ms"] <= 260
    assert 950 <= ended["at_ms"] <= 1100
    assert msgs[-1]["from_finalize"] is False


def test_keepalive_and_idle_timeout(server_factory):
    srv = server_factory(idle_timeout_s=0.6)
    ws, _ = open_stream(srv)
    for _ in range(4):
        time.sleep(0.3)
        ws.send(json.dumps({"type": "keepalive"}))
    msg = recv(ws, timeout=3)
    assert msg["code"] == "idle_timeout" and msg["retryable"] is True
    assert close_code(ws) == 1000


def test_model_error_closes_and_cleans_up(server):
    ws, _ = open_stream(server)
    server.engine().fail_next_steps = 1
    # The server closes as soon as the first step fails, which can race the
    # remaining sends; the error message is still queued for recv below.
    try:
        send_audio(ws, tone(0.2))
    except ConnectionClosed:
        pass
    msg = recv_until(ws, lambda m: m["type"] == "error")[-1]
    assert msg["code"] == "model_error" and msg["retryable"] is True
    assert close_code(ws) == 1011
    assert wait_for(lambda: server.runtime.active_streams == 0 and not server.engine().live_states)


# --------------------------------------------------------------------------
# isolation, capacity, flow control
# --------------------------------------------------------------------------
def test_many_simultaneous_streams_are_isolated(server):
    n = 24
    results: dict[int, dict] = {}
    errors: list[BaseException] = []

    def client(i: int):
        try:
            ws, _ = open_stream(server, session_id=f"s{i}", interim_results=False)
            voiced = 0.2 * (1 + i % 6)  # 1..6 words
            send_audio(ws, tone(voiced, freq=300 + 10 * i))
            send_audio(ws, silence(0.3))
            ws.send(json.dumps({"type": "finalize"}))
            results[i] = recv_until(ws, is_final, timeout=20)[-1]
            finish(ws)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    for i, final in results.items():
        assert len(final["text"].split()) == 1 + i % 6, (i, final)
        assert final["utterance_id"] == 1
    assert len(results) == n
    assert max(server.engine().batch_sizes) > 1, "sessions should share inference steps"
    assert wait_for(lambda: server.runtime.active_streams == 0)


def test_admission_capacity(server_factory):
    srv = server_factory(max_streams=2)
    a, _ = open_stream(srv)
    b, _ = open_stream(srv)
    c = connect(srv)
    msg = recv(c)
    assert msg["code"] == "capacity" and msg["retryable"] is True
    assert close_code(c) == 1013
    a.close()
    assert wait_for(lambda: srv.runtime.active_streams == 1)
    d, ready = open_stream(srv)
    assert ready["type"] == "ready"
    cap = srv.http().get("/v1/capacity").json()
    assert cap["live"] == {"max_streams": 2, "active_streams": 2, "available": 0}


def test_backpressure_bounds_queue(server_factory):
    srv = server_factory(stub_step_latency_ms=40, max_queue_ms=400, max_lag_ms=60_000)
    ws, _ = open_stream(srv, interim_results=False)
    send_audio(ws, tone(3.0), frame_bytes=3200)
    ws.send(json.dumps({"type": "finalize"}))
    final = recv_until(ws, is_final, timeout=30)[-1]
    assert len(final["text"].split()) == 15  # all audio processed, nothing dropped
    finish(ws)
    assert wait_for(lambda: srv.runtime.active_streams == 0)
    # never more than one frame above the bound
    assert srv.runtime.peak_queued_ms <= 400 + 100


def test_lag_triggers_overloaded(server_factory):
    srv = server_factory(stub_step_latency_ms=400, max_queue_ms=240, max_lag_ms=900)
    ws, _ = open_stream(srv, interim_results=False)
    try:
        send_audio(ws, tone(6.0), frame_bytes=1280)
    except ConnectionClosed:
        pass
    msgs = recv_until(ws, lambda m: m["type"] == "error", timeout=15)
    assert msgs[-1]["code"] == "overloaded" and msgs[-1]["retryable"] is True
    assert close_code(ws) == 1013
    assert wait_for(lambda: srv.runtime.active_streams == 0 and not srv.engine().live_states)


def _send_realtime(ws, audio, seconds_frame=0.02):
    start = time.perf_counter()
    for i, f in enumerate(frames(pcm16(audio), FRAME)):
        ws.send(f)
        delay = start + (i + 1) * seconds_frame - time.perf_counter()
        if delay > 0:
            time.sleep(delay)


def test_realtime_client_behind_tcp_buffers_gets_overloaded(server_factory):
    # Processing at 1.5x real time with a small queue: the backlog itself stays
    # under the lag limit while audio piles up in kernel socket buffers; the
    # wall-clock estimate must still detect the lag.
    srv = server_factory(stub_step_latency_ms=120, max_queue_ms=240, max_lag_ms=1200)
    ws, _ = open_stream(srv, interim_results=False)
    try:
        _send_realtime(ws, tone(8.0))
    except ConnectionClosed:
        pass
    msgs = recv_until(ws, lambda m: m["type"] == "error", timeout=15)
    assert msgs[-1]["code"] == "overloaded"


def test_client_pause_is_not_lag(server_factory):
    srv = server_factory(max_lag_ms=500, idle_timeout_s=10)
    ws, _ = open_stream(srv, interim_results=False)
    _send_realtime(ws, tone(0.4))
    for _ in range(8):  # 1.6 s of keepalive-only (e.g. client-side mute)
        time.sleep(0.2)
        ws.send(json.dumps({"type": "keepalive"}))
    _send_realtime(ws, tone(0.4))
    ws.send(json.dumps({"type": "finalize"}))
    final = recv_until(ws, is_final)[-1]
    assert len(final["text"].split()) == 4


def test_disconnect_cleanup_no_leaks(server_factory):
    srv = server_factory(stub_step_latency_ms=20)
    sockets = []
    for i in range(8):
        ws, _ = open_stream(srv, session_id=f"d{i}")
        send_audio(ws, tone(0.5))
        sockets.append(ws)
    assert wait_for(lambda: len(srv.engine().live_states) == 8)
    for i, ws in enumerate(sockets):
        if i % 2:
            ws.close()  # clean close
        else:
            ws.socket.close()  # abrupt TCP drop, no close frame
    assert wait_for(lambda: srv.runtime.active_streams == 0, timeout=10)
    assert wait_for(lambda: not srv.engine().live_states, timeout=5)
    assert srv.runtime.live.session_count == 0
    metrics = srv.http().get("/metrics").text
    assert 'hopstt_active_streams{model="stub-streaming"} 0.0' in metrics


def test_drain(server_factory):
    srv = server_factory(drain_timeout_s=1.0)
    ws, _ = open_stream(srv)
    srv.call(_begin_drain, srv.runtime)
    assert srv.http(auth=False).get("/readyz").status_code == 503
    late = connect(srv)
    msg = recv(late)
    assert msg["code"] == "draining" and msg["retryable"] is True
    assert close_code(late) == 1012
    # running stream keeps working during the drain window
    send_audio(ws, tone(0.6))
    ws.send(json.dumps({"type": "finalize"}))
    assert recv_until(ws, is_final)[-1]["text"]
    # after the drain timeout the remaining stream is told to reconnect elsewhere
    msg = recv_until(ws, lambda m: m["type"] == "error", timeout=5)[-1]
    assert msg["code"] == "draining"
    assert close_code(ws) == 1012
    assert wait_for(lambda: srv.runtime.drained.is_set())


async def _begin_drain(rt):
    rt.begin_drain()


def test_batch_role_refuses_streams(server_factory):
    srv = server_factory(role="batch", allowed_models=("stub-batch",))
    ws = connect(srv)
    assert recv(ws)["code"] == "capacity"
    assert close_code(ws) == 1013
