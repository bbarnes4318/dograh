"""Capacity ramp / batch throughput / mixed mode against the in-process FAKE HopSTT."""

import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bench import capacity as C  # noqa: E402
from bench.client import StreamRecord, parse_prometheus  # noqa: E402
from bench.fake_server import ThreadedFake  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_bench_runners import KEY, SECRET_WORD, make_corpus, scripted  # noqa: E402


def test_live_ramp_stops_at_capacity_and_reports_last_passing_step(tmp_path):
    corpus = make_corpus(tmp_path / "corpus", seconds=0.4)
    with ThreadedFake(scripted(), api_key=KEY, word_ms=50, max_streams=3) as fake:
        rc = C.main([*_argv(fake, corpus, tmp_path / "cap"), "--steps", "1,2,6,8"])
        assert rc == 0
    res = json.loads((tmp_path / "cap" / "capacity.json").read_text())
    live = res["live"]
    assert res["solo"]["ok"] == 4
    assert live["measured_capacity_streams"] == 2
    assert live["stopped"]["at_streams"] == 6
    last = live["steps"][-1]
    assert last["capacity_rejections"] > 0 and last["error_rate"] > 0.01
    first = live["steps"][0]
    assert first["passed"] and first["accuracy"]["wer_delta"] == 0.0 and first["accuracy"]["wer_vs_solo"] == 0.0
    assert first["server"]["before"]["capacity"]["live"]["max_streams"] == 3
    assert "hopstt_streams_total" in first["server"]["after"]["metrics"]
    assert first["server"]["counter_delta"]["hopstt_streams_total"] >= 1
    assert (tmp_path / "cap" / "steps.csv").is_file()
    for f in (tmp_path / "cap").iterdir():
        assert SECRET_WORD not in f.read_text(), f.name


def _argv(fake, corpus, out):
    return ["--url", fake.http_url, "--ws-url", fake.ws_url, "--api-key", KEY, "--corpus", str(corpus),
            "--model", "fake-streaming", "--chunk-ms", "160", "--speed", "4", "--step-duration", "0.8",
            "--stagger-ms", "20", "--cooldown", "0", "--final-timeout", "0.5", "--out", str(out)]


def test_latency_threshold_breach(tmp_path):
    corpus = make_corpus(tmp_path / "corpus", seconds=0.4)
    with ThreadedFake(scripted(), api_key=KEY, word_ms=50, final_delay_ms=150) as fake:
        C.main([*_argv(fake, corpus, tmp_path / "cap"), "--steps", "1,2", "--max-p95-final-ms", "100",
                "--no-solo"])
    live = json.loads((tmp_path / "cap" / "capacity.json").read_text())["live"]
    assert live["measured_capacity_streams"] is None
    assert "finalize->final p95" in live["stopped"]["reasons"][0]


def test_batch_throughput_mode(tmp_path):
    corpus = make_corpus(tmp_path / "corpus", seconds=0.4)
    with ThreadedFake(scripted(), api_key=KEY, batch_delay_ms=20, batch_concurrency=2) as fake:
        C.main([*_argv(fake, corpus, tmp_path / "cap"), "--mode", "batch", "--batch-jobs", "8",
                "--batch-concurrency", "3", "--batch-mode", "jobs"])
    b = json.loads((tmp_path / "cap" / "capacity.json").read_text())["batch"]
    assert b["jobs"] == 8 and b["succeeded"] == 8 and b["throughput_audio_s_per_s"] > 0
    assert b["rtf"]["n"] == 8 and b["server_inference_ms"]["p50"] == 20


def test_mixed_mode_compares_live_latency_with_and_without_batch(tmp_path):
    corpus = make_corpus(tmp_path / "corpus", seconds=0.4)
    with ThreadedFake(scripted(), api_key=KEY, word_ms=50, batch_delay_ms=20) as fake:
        C.main([*_argv(fake, corpus, tmp_path / "cap"), "--mode", "mixed", "--steps", "1,2",
                "--batch-concurrency", "2", "--batch-warmup", "0.1", "--batch-mode", "sync"])
    res = json.loads((tmp_path / "cap" / "capacity.json").read_text())
    assert res["live_only"]["measured_capacity_streams"] == 2
    assert res["with_batch"]["measured_capacity_streams"] == 2
    assert res["batch_backlog"]["succeeded"] > 0
    assert [c["streams"] for c in res["batch_impact"]] == [1, 2]
    assert "live latency impact" in (tmp_path / "cap" / "summary.md").read_text()


# --- pure logic -----------------------------------------------------------------------------


def _step(p95=100.0, err=0.0, delta=0.0, vs_solo=0.0):
    return {"finalize_to_final_ms": {"p95": p95}, "error_rate": err,
            "accuracy": {"wer_delta": delta, "wer_vs_solo": vs_solo}}


def test_breaches():
    th = C.Thresholds(300, 0.01, 0.02)
    assert C.breaches(_step(), th) == []
    assert "p95" in C.breaches(_step(p95=301), th)[0]
    assert "error rate" in C.breaches(_step(err=0.02), th)[0]
    assert "WER delta" in C.breaches(_step(delta=0.03), th)[0]
    assert "WER delta" in C.breaches(_step(delta=None, vs_solo=0.05), th)[0]  # no refs: divergence
    assert C.breaches(_step(p95=None), th)


def test_accuracy_vs_solo():
    item = C.PoolItem(id="p1", samples=np.zeros(8, np.int16), language="en", reference="a b c d")
    rec_same = StreamRecord(id="p1", model=None, chunk_ms=None, audio_s=1, ok=True, hypothesis="a b c d")
    rec_worse = StreamRecord(id="p1", model=None, chunk_ms=None, audio_s=1, ok=True, hypothesis="a b x")
    solo = {"p1": "a b c d"}
    assert C.accuracy_vs_solo([(item, rec_same)], solo)["wer_delta"] == 0.0
    acc = C.accuracy_vs_solo([(item, rec_same), (item, rec_worse)], solo)
    assert acc["wer_delta"] == 2 / 8 and acc["wer_vs_solo"] == 2 / 8


def test_parse_prometheus():
    text = '# HELP x\nhopstt_active_streams{role="live"} 3\nhopstt_gpu_util 0.75\nbad line\nnan_metric NaN\n'
    m = parse_prometheus(text)
    assert m['hopstt_active_streams{role="live"}'] == 3 and m["hopstt_gpu_util"] == 0.75
