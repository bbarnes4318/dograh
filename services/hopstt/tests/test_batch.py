import io
import subprocess
import time
import wave

import numpy as np
import pytest

from conftest import HAS_FFMPEG, pcm16, silence, tone, wait_for

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="batch decode needs ffmpeg")


def wav_bytes(channels: list[np.ndarray], rate: int = 8000) -> bytes:
    n = max(len(c) for c in channels)
    data = np.zeros((n, len(channels)), np.float32)
    for i, c in enumerate(channels):
        data[: len(c), i] = c
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16(data.reshape(-1)))
    return buf.getvalue()


def stereo_call() -> bytes:
    # caller (ch0) speaks 0-1 s, agent (ch1) speaks 2-3.5 s
    ch0 = np.concatenate([tone(1.0, 8000), silence(3.0, 8000)])
    ch1 = np.concatenate([silence(2.0, 8000), tone(1.5, 8000, freq=600), silence(0.5, 8000)])
    return wav_bytes([ch0, ch1])


def post(client, path, data: bytes, filename="call.wav", **fields):
    return client.post(path, files={"file": (filename, data)}, data={k: str(v) for k, v in fields.items()})


def test_sync_stereo_split_preserves_channels(server):
    r = post(server.http(), "/v1/transcriptions", stereo_call())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "stub-batch" and body["engine"] == "stub"
    assert body["sample_rate"] == 8000
    assert abs(body["duration_sec"] - 4.0) < 0.05
    assert set(body["timings"]) == {"decode_ms", "queue_ms", "inference_ms"}
    ch0, ch1 = body["channels"]
    assert (ch0["channel"], ch1["channel"]) == (0, 1)
    seg0, seg1 = ch0["segments"][0], ch1["segments"][0]
    assert seg0["start"] < 0.1 and abs(seg0["end"] - 1.0) < 0.1
    assert abs(seg1["start"] - 2.0) < 0.1 and abs(seg1["end"] - 3.5) < 0.1
    assert len(ch0["segments"]) == 1 and len(ch1["segments"]) == 1
    words = seg1["words"]
    assert words and {"word", "start", "end", "confidence"} <= set(words[0])
    assert ch0["language"] == "en"


def test_sync_mix_and_no_words(server):
    r = post(server.http(), "/v1/transcriptions", stereo_call(), channels="mix", word_timestamps="false")
    body = r.json()
    assert len(body["channels"]) == 1
    assert all("words" not in s for s in body["channels"][0]["segments"])
    assert len(body["channels"][0]["segments"]) == 2


@pytest.mark.parametrize("ext,args", [("mp3", ["-c:a", "libmp3lame"]), ("flac", ["-c:a", "flac"])])
def test_sync_compressed_upload(server, tmp_path, ext, args):
    path = tmp_path / f"a.{ext}"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-ar", "16000", *args, str(path)],
        check=True,
    )
    r = post(server.http(), "/v1/transcriptions", path.read_bytes(), filename=f"a.{ext}")
    assert r.status_code == 200, r.text
    assert r.json()["channels"][0]["segments"]


def test_batch_auth_and_validation(server):
    data = stereo_call()
    assert post(server.http(auth=False), "/v1/transcriptions", data).status_code == 401
    assert post(server.http(), "/v1/transcriptions", data, model="nope").status_code == 400
    assert post(server.http(), "/v1/transcriptions", data, model="stub-streaming").status_code == 400
    assert post(server.http(), "/v1/transcriptions", data, language="de").status_code == 400
    assert post(server.http(), "/v1/transcriptions", data, channels="left").status_code == 400
    assert post(server.http(), "/v1/transcriptions", data, priority="urgent").status_code == 400
    r = post(server.http(), "/v1/transcriptions", b"not audio at all", filename="x.wav")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_audio"
    assert server.http(auth=False).get("/v1/jobs/abc").status_code == 401


def test_upload_size_limit(server_factory):
    srv = server_factory(max_upload_mb=1)
    big = wav_bytes([tone(80.0, 8000)])  # ~1.3 MB
    r = post(srv.http(), "/v1/transcriptions", big)
    assert r.status_code == 413
    # also enforced for chunked uploads without Content-Length
    def gen():
        boundary = b"--b0undary"
        yield boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n\r\n'
        for i in range(0, len(big), 65536):
            yield big[i : i + 65536]
        yield b"\r\n" + boundary + b"--\r\n"

    r = srv.http().post(
        "/v1/transcriptions",
        content=gen(),
        headers={"Content-Type": "multipart/form-data; boundary=b0undary"},
    )
    assert r.status_code == 413


def test_sync_duration_limit(server_factory):
    srv = server_factory(sync_max_seconds=2.0)
    r = post(srv.http(), "/v1/transcriptions", stereo_call())
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_long"
    # jobs have no sync limit
    r = post(srv.http(), "/v1/jobs", stereo_call())
    assert r.status_code == 202


def poll(client, job_id, statuses=("succeeded", "failed", "cancelled"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/jobs/{job_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.02)
    raise TimeoutError(body)


def test_job_lifecycle(server):
    c = server.http()
    r = post(c, "/v1/jobs", stereo_call())
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
    body = poll(c, r.json()["job_id"])
    assert body["status"] == "succeeded" and body["attempts"] == 1
    assert len(body["result"]["channels"]) == 2
    assert c.get("/v1/jobs/unknown").status_code == 404


def test_job_cancel_running_and_idempotent(server_factory):
    srv = server_factory(stub_batch_latency_ms=3000)
    c = srv.http()
    job_id = post(c, "/v1/jobs", stereo_call()).json()["job_id"]
    poll(c, job_id, statuses=("running",))
    assert c.delete(f"/v1/jobs/{job_id}").status_code == 200
    assert poll(c, job_id)["status"] == "cancelled"
    assert c.delete(f"/v1/jobs/{job_id}").status_code == 200
    assert c.get(f"/v1/jobs/{job_id}").json()["status"] == "cancelled"


def test_job_retry_then_success_and_exhaustion(server):
    c = server.http()
    eng = server.engine("stub-batch")
    eng.fail_next_calls = 1
    body = poll(c, post(c, "/v1/jobs", stereo_call()).json()["job_id"])
    assert body["status"] == "succeeded" and body["attempts"] == 2
    eng.fail_next_calls = 5
    body = poll(c, post(c, "/v1/jobs", stereo_call()).json()["job_id"])
    assert body["status"] == "failed" and body["attempts"] == 2
    assert body["error"]["code"] == "model_error" and body["error"]["retryable"] is True


def test_priority_low_runs_after_normal(server_factory):
    srv = server_factory(stub_batch_latency_ms=300, batch_max_concurrency=1)
    c = srv.http()
    first = post(c, "/v1/jobs", stereo_call()).json()["job_id"]
    poll(c, first, statuses=("running",))
    low = post(c, "/v1/jobs", stereo_call(), priority="low").json()["job_id"]
    normal = post(c, "/v1/jobs", stereo_call()).json()["job_id"]
    for j in (first, low, normal):
        poll(c, j)
    jobs = srv.runtime.batch.jobs
    assert jobs[normal].started_at < jobs[low].started_at


def test_queue_limit_429(server_factory):
    srv = server_factory(stub_batch_latency_ms=2000, batch_queue_limit=2)
    c = srv.http()
    first = post(c, "/v1/jobs", stereo_call()).json()["job_id"]
    poll(c, first, statuses=("running",))
    assert post(c, "/v1/jobs", stereo_call()).status_code == 202
    assert post(c, "/v1/jobs", stereo_call()).status_code == 202
    r = post(c, "/v1/jobs", stereo_call())
    assert r.status_code == 429 and r.json()["error"]["retryable"] is True
    cap = c.get("/v1/capacity").json()["batch"]
    assert cap == {"max_concurrency": 1, "running": 1, "queued": 2, "queue_limit": 2}


def test_job_ttl(server_factory):
    srv = server_factory(job_ttl_s=0.3)
    c = srv.http()
    job_id = post(c, "/v1/jobs", stereo_call()).json()["job_id"]
    poll(c, job_id)
    assert wait_for(lambda: c.get(f"/v1/jobs/{job_id}").status_code == 404, timeout=3)


def test_upload_temp_files_are_deleted(server, tmp_path, monkeypatch):
    import glob
    import tempfile

    before = set(glob.glob(f"{tempfile.gettempdir()}/hopstt-*"))
    c = server.http()
    post(c, "/v1/transcriptions", stereo_call())
    post(c, "/v1/transcriptions", b"garbage", filename="x.wav")
    poll(c, post(c, "/v1/jobs", stereo_call()).json()["job_id"])
    assert set(glob.glob(f"{tempfile.gettempdir()}/hopstt-*")) - before == set()


def test_role_live_has_no_batch(server_factory):
    srv = server_factory(role="live", allowed_models=("stub-streaming",))
    c = srv.http()
    assert post(c, "/v1/transcriptions", stereo_call()).status_code == 404
    assert post(c, "/v1/jobs", stereo_call()).status_code == 404
    assert c.get("/v1/jobs/x").status_code == 404
    assert c.get("/v1/capacity").json()["role"] == "live"


def test_both_role_refuses_batch_at_live_util(server_factory):
    from conftest import open_stream

    srv = server_factory(max_streams=2, batch_pause_at_live_util=0.5)
    ws, _ = open_stream(srv)
    r = post(srv.http(), "/v1/transcriptions", stereo_call())
    assert r.status_code == 503 and r.json()["error"]["code"] == "live_priority"
    ws.close()
    assert wait_for(lambda: srv.runtime.active_streams == 0)
    assert post(srv.http(), "/v1/transcriptions", stereo_call()).status_code == 200
