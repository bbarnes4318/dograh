"""run_batch / run_stream / report against an in-process FAKE HopSTT (bench.fake_server).

The fake is a protocol stand-in, not the real service: it returns scripted text
for synthetic tones, so these tests exercise the clients, timing, recovery and
report plumbing without any model or real speech.
"""

import asyncio
import csv
import json
import pathlib
import stat
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bench import audio as A  # noqa: E402
from bench import report, run_batch, run_stream  # noqa: E402
from bench.client import StreamConfig, stability, stream_once  # noqa: E402
from bench.fake_server import FakeHopSTT, ThreadedFake  # noqa: E402
from bench.runs import load_run  # noqa: E402

KEY = "test-key"
SECRET_WORD = "zanzibarian"  # appears only in transcripts; must never leak into reports

ITEMS = [
    ("hs_enaaaa", "en", f"my name is john {SECRET_WORD}", ["names"], [{"type": "first_name", "value": "John"}]),
    ("hs_enbbbb", "en", "no I am not interested", ["short_no"], [{"type": "yes_no", "value": "no"}]),
    ("hs_escccc", "es", "tengo sesenta y cinco años", [], [{"type": "age", "value": "65"}]),
    ("hs_sildd", "en", "", ["silence"], []),
]


def make_corpus(root: pathlib.Path, seconds: float = 0.6) -> pathlib.Path:
    (root / "audio").mkdir(parents=True)
    rows = []
    for iid, lang, ref, tags, fields in ITEMS:
        sig = A.silence(seconds) if not ref else A.tone(seconds, 8000, 440)
        A.write_wav(root / "audio" / f"{iid}.wav", sig, 8000)
        baseline = ref.replace("john", "jon") if ref else "uh"
        rows.append({"id": iid, "audio": f"audio/{iid}.wav", "language": lang, "reference": ref,
                     "reference_status": "verified", "tags": tags, "critical_fields": fields,
                     "baseline": {"cartesia": baseline}})
    (root / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return root


def scripted(drop_word=None):
    out = {iid: ref for iid, _, ref, _, _ in ITEMS if ref}
    if drop_word:
        out["hs_enbbbb"] = "I am interested"  # dropped negation
    return out


@pytest.fixture
def corpus(tmp_path):
    return make_corpus(tmp_path / "corpus")


def test_run_batch_sync_and_jobs(corpus, tmp_path):
    with ThreadedFake(scripted(), api_key=KEY) as fake:
        out = tmp_path / "runs"
        for mode in ("sync", "jobs"):
            rc = run_batch.main(["--url", fake.http_url, "--api-key", KEY, "--corpus", str(corpus),
                                 "--models", "fake-batch", "--mode", mode, "--concurrency", "2",
                                 "--out", str(out / mode)])
            assert rc == 0
            meta, rows = load_run(out / mode / "batch_fake-batch")
            assert meta["kind"] == "batch" and meta["n_errors"] == 0
            hyps = {r["id"]: r["hypothesis"] for r in rows}
            assert hyps["hs_enaaaa"] == f"my name is john {SECRET_WORD}"
            assert hyps["hs_sildd"] == ""
            assert rows[0]["server_timings"]["inference_ms"] == 10
            assert stat.S_IMODE((out / mode / "batch_fake-batch" / "items.jsonl").stat().st_mode) == 0o600


def test_run_batch_bad_key_records_errors(corpus, tmp_path):
    with ThreadedFake(scripted(), api_key=KEY) as fake:
        run_batch.main(["--url", fake.http_url, "--api-key", "wrong", "--corpus", str(corpus),
                        "--out", str(tmp_path / "r")])
        meta, rows = load_run(tmp_path / "r" / "batch_default")
        assert meta["n_errors"] == len(ITEMS) and rows[0]["error"] == "http_401"


def test_run_stream_sweep_and_report(corpus, tmp_path):
    with ThreadedFake(scripted(drop_word=True), api_key=KEY, word_ms=100, final_delay_ms=30) as fake:
        out = tmp_path / "stream"
        rc = run_stream.main(["--url", fake.http_url, "--ws-url", fake.ws_url, "--api-key", KEY,
                              "--corpus", str(corpus), "--models", "fake-streaming",
                              "--chunk-ms", "80,320", "--speed", "0", "--final-timeout", "0.5",
                              "--out", str(out)])
        assert rc == 0
        cfg = fake.configs[0]
        assert cfg["type"] == "config" and cfg["generation"] == 0 and cfg["correlation_id"] == "hs_enaaaa"
        assert {c["chunk_ms"] for c in fake.configs} == {80, 320}
    meta, rows = load_run(out / "stream_fake-streaming@80ms")
    by_id = {r["id"]: r for r in rows}
    assert meta["kind"] == "stream" and meta["engine"] == "stream:fake-streaming@80ms"
    assert by_id["hs_enaaaa"]["hypothesis"] == f"my name is john {SECRET_WORD}"
    assert by_id["hs_enaaaa"]["finalize_to_final_ms"] >= 25  # fake final_delay_ms=30
    assert by_id["hs_enaaaa"]["ttfp_ms"] is not None and by_id["hs_enaaaa"]["n_interims"] >= 1
    assert by_id["hs_enaaaa"]["server_timings"] == [{"finalize_ms": 30}]
    assert by_id["hs_sildd"]["no_final"] and by_id["hs_sildd"]["hypothesis"] == "" and by_id["hs_sildd"]["ok"]
    assert meta["latency"]["finalize_to_final_ms"]["n"] == 3
    sweep = list(csv.DictReader((out / "sweep.csv").open()))
    assert [r["chunk_ms"] for r in sweep] == ["80", "320"]

    rep = tmp_path / "report"
    rc = report.main(["--corpus", str(corpus), "--runs", str(out), "--baseline", "cartesia",
                      "--out", str(rep), "--bootstrap", "200", "--min-items", "2", "--min-es-items", "1",
                      "--min-silence-items", "1"])
    assert rc == 0
    results = json.loads((rep / "results.json").read_text())
    assert set(results["engines"]) == {"stream:fake-streaming@80ms", "stream:fake-streaming@320ms",
                                       "imported:cartesia"}
    eng = results["engines"]["stream:fake-streaming@80ms"]
    assert eng["negation"]["negative_flips"] == 1  # scripted dropped negation
    assert eng["fields"]["by_type"]["first_name"]["accuracy"] == 1.0
    assert results["engines"]["imported:cartesia"]["fields"]["by_type"]["first_name"]["accuracy"] == 0.0
    assert results["engines"]["imported:cartesia"]["hallucination"]["words_total"] == 0  # "uh" is a filler
    gates = {g["candidate"]: g for g in results["gates"]}
    assert gates["stream:fake-streaming@80ms"]["verdict"] in ("FAIL", "INSUFFICIENT_DATA")
    assert gates["stream:fake-streaming@80ms"]["checks"]["negative_flips"]["status"] != "PASS"
    # Anonymized by default: no transcript text anywhere in the report directory.
    for f in rep.iterdir():
        assert SECRET_WORD not in f.read_text(), f.name
    assert "## Acceptance gate" in (rep / "summary.md").read_text()

    rep2 = tmp_path / "report_text"
    report.main(["--corpus", str(corpus), "--runs", str(out), "--out", str(rep2), "--bootstrap", "50",
                 "--include-text"])
    private = rep2 / "items_text.private.csv"
    assert SECRET_WORD in private.read_text()
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_report_refuses_in_repo_output(corpus):
    from bench.safety import UnsafePathError

    with pytest.raises(UnsafePathError):
        report.main(["--corpus", str(corpus), "--out", str(pathlib.Path(__file__).parent / "nope")])


# --- client-level behaviour ----------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _cfg(fake, **kw):
    base = dict(url=fake.http_url, ws_url=fake.ws_url, api_key=KEY, model="fake-streaming", chunk_ms=160,
                speed=0, final_timeout_s=1.0)
    base.update(kw)
    return StreamConfig(**base)


def test_realtime_pacing_and_mulaw():
    async def go():
        async with FakeHopSTT({"x1": "hello there friend"}, api_key=KEY, word_ms=100) as fake:
            t0 = time.perf_counter()
            rec = await stream_once(_cfg(fake, speed=1.0, encoding="mulaw", frame_ms=40), A.tone(0.5),
                                    item_id="x1")
            return rec, time.perf_counter() - t0, fake.configs[0]
    rec, wall, cfg = _run(go())
    assert rec.ok and rec.hypothesis == "hello there friend"
    assert wall >= 0.45  # 0.5 s of audio streamed in real time
    assert cfg["encoding"] == "mulaw" and rec.wall_rtf >= 0.9


def test_partial_stability_detects_revisions():
    async def go():
        async with FakeHopSTT({"x2": "one two three four five"}, api_key=KEY, word_ms=80, revise=True) as fake:
            return await stream_once(_cfg(fake, chunk_ms=80), A.tone(0.6), item_id="x2")
    rec = _run(go())
    assert rec.revision_rate is not None and rec.revision_rate > 0
    assert rec.partial_edit_mean > 0
    r, e, f = stability(["a", "a b", "a b c"], "a b c")
    assert (r, e, f) == (0.0, 1.0, 0.0)
    r, _, _ = stability(["a x", "a b c"], None)
    assert r == 0.0  # only the last word changed (still growing)
    r, _, _ = stability(["a x c", "a b c d"], None)
    assert r == 1.0


def test_finalize_at_speech_end_sends_less_audio():
    async def go():
        async with FakeHopSTT({"x3": "short answer"}, api_key=KEY, word_ms=100) as fake:
            rec = await stream_once(_cfg(fake, finalize_at="speech_end", vad_stop_ms=100, chunk_ms=80),
                                    np.concatenate([A.tone(0.4), A.silence(1.0)]), item_id="x3",
                                    speech_end_s=0.4)
            return rec
    rec = _run(go())
    assert rec.ok and rec.hypothesis == "short answer" and rec.speech_end_to_final_ms is not None


def test_recovery_client_kill_reconnects_and_replays():
    async def go():
        async with FakeHopSTT({"x4": "please call me back tomorrow"}, api_key=KEY, word_ms=100) as fake:
            rec = await stream_once(_cfg(fake), A.tone(0.8), item_id="x4", kill_at_s=0.4,
                                    session_id="sess-1")
            return rec, fake.configs
    rec, configs = _run(go())
    assert rec.killed and rec.recovered and rec.reconnects == 1 and rec.recovery_ms is not None
    assert rec.hypothesis == "please call me back tomorrow" and rec.duplicate_finals == 0
    assert [c["generation"] for c in configs] == [0, 1]
    assert {c["session_id"] for c in configs} == {"sess-1"}
    assert configs[1]["stream_offset_ms"] == 0  # nothing committed yet: replay from the start


def test_recovery_after_server_drop():
    async def go():
        async with FakeHopSTT({"x5": "yes that is right"}, api_key=KEY, word_ms=100, drop_after_ms=300) as fake:
            return await stream_once(_cfg(fake), A.tone(0.8), item_id="x5")
    rec = _run(go())
    assert rec.dropped and rec.reconnects >= 1 and rec.ok and rec.hypothesis == "yes that is right"


def test_recovery_cli_reports_success_rate(corpus, tmp_path):
    with ThreadedFake(scripted(), api_key=KEY, word_ms=100) as fake:
        run_stream.main(["--url", fake.http_url, "--ws-url", fake.ws_url, "--api-key", KEY,
                         "--corpus", str(corpus), "--models", "fake-streaming", "--chunk-ms", "160",
                         "--speed", "0", "--final-timeout", "0.5", "--kill-fraction", "0.5",
                         "--language", "en", "--tags", "names,short_no", "--out", str(tmp_path / "rec")])
    meta, _ = load_run(tmp_path / "rec" / "stream_fake-streaming@160ms")
    rec = meta["failures"]["recovery"]
    assert rec["n_killed"] == 2 and rec["success_rate"] == 1.0 and rec["duplicate_final_rate"] == 0.0


def test_fatal_errors_are_not_retried():
    async def go():
        async with FakeHopSTT({}, api_key=KEY) as fake:
            bad_key = await stream_once(_cfg(fake, api_key="nope"), A.tone(0.3), item_id="x6")
            bad_cfg = await stream_once(_cfg(fake, chunk_ms=123), A.tone(0.3), item_id="x7")
            return bad_key, bad_cfg
    bad_key, bad_cfg = _run(go())
    assert not bad_key.ok and bad_key.error_code == "auth_failed" and bad_key.reconnects == 0
    assert not bad_cfg.ok and bad_cfg.error_code == "invalid_config"


def test_capacity_rejection_surfaces():
    async def go():
        async with FakeHopSTT({}, api_key=KEY, max_streams=0) as fake:
            return await stream_once(_cfg(fake), A.tone(0.3), item_id="x8")
    rec = _run(go())
    assert not rec.ok and rec.error_code == "capacity"
