import json
import re

from conftest import API_KEY, open_stream, pcm16, recv_until, tone

ALLOWED_LABELS = {
    "model": {"stub-streaming", "stub-batch"},
    "role": {"live", "batch", "both"},
    "kind": {"live", "batch", "sync", "job"},
    "language": {"en", "es", "auto", "other"},
    "priority": {"normal", "low"},
    "status": {"queued", "running", "succeeded", "failed", "cancelled", "rejected"},
    "reason": {
        "end_of_stream", "cancel", "disconnect", "internal", "auth_failed", "invalid_config",
        "config_timeout", "capacity", "draining", "overloaded", "idle_timeout", "model_error", "unknown",
    },
    "code": {
        "auth_failed", "invalid_config", "config_timeout", "capacity", "draining", "overloaded",
        "idle_timeout", "model_error", "internal",
    },
    "le": None,  # histogram buckets
}


def test_health_and_ready_need_no_auth(server):
    c = server.http(auth=False)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"ready": True}
    assert c.get("/v1/capacity").status_code == 401
    assert c.get("/v1/models").status_code == 401
    bad = server.http(auth=False)
    bad.headers["Authorization"] = "Bearer " + API_KEY[:-1] + "x"
    assert bad.get("/v1/models").status_code == 401


def test_capacity_and_models_shape(server):
    cap = server.http().get("/v1/capacity").json()
    assert cap["worker_id"] == "test-worker" and cap["role"] == "both" and cap["draining"] is False
    assert cap["live"] == {"max_streams": 32, "active_streams": 0, "available": 32}
    assert cap["batch"]["max_concurrency"] == 1 and cap["batch"]["queue_limit"] == 100
    models = {m["id"]: m for m in server.http().get("/v1/models").json()["models"]}
    assert models["stub-streaming"]["kind"] == "streaming"
    assert models["stub-streaming"]["chunk_ms"] == [80, 160, 320, 560, 1120]
    assert models["stub-streaming"]["languages"] == ["en", "es", "auto"]
    assert models["stub-streaming"]["loaded"] and models["stub-streaming"]["warm"]
    assert models["stub-batch"]["kind"] == "batch"
    assert cap["models"] == list(models.values())


def test_metrics_labels_are_bounded(server):
    secret_ids = []
    for i in range(5):
        sid = f"tenant-42-call-{i}-+15551234567"
        secret_ids.append(sid)
        ws, _ = open_stream(server, session_id=sid, correlation_id=f"corr-{i}", generation=i)
        for j in range(0, 32000, 640):
            ws.send(pcm16(tone(1.0))[j : j + 640])
        ws.send(json.dumps({"type": "end_of_stream"}))
        recv_until(ws, lambda m: m["type"] == "closed")
    text = server.http().get("/metrics").text
    for sid in secret_ids:
        assert sid not in text
    assert "corr-" not in text and "+1555" not in text
    for line in text.splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        for key, value in re.findall(r'(\w+)="([^"]*)"', line):
            allowed = ALLOWED_LABELS.get(key, "missing")
            assert allowed != "missing", f"unexpected label {key} in {line}"
            if allowed is not None:
                assert value in allowed, line
    assert 'hopstt_stream_reconnect_opens_total{model="stub-streaming"} 4.0' in text
    assert 'hopstt_stream_closes_total{reason="end_of_stream"} 5.0' in text
    assert 'hopstt_audio_seconds_total{kind="live",model="stub-streaming"} 5.0' in text
    assert "hopstt_final_latency_seconds_count" in text
    assert "hopstt_inference_step_seconds_bucket" in text
