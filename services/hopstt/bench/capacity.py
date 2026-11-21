"""Capacity test: ramp concurrent live streams, batch throughput, and mixed load.

    # live ramp (default steps 1,5,10,20,50,100,200; 60 s each)
    python -m bench.capacity --mode live --url http://gpu-host:8788 --corpus /secure/hopstt-corpus \
        --model nemotron-3.5-streaming --chunk-ms 320 --out /secure/hopstt-runs/capacity

    # batch throughput (concurrent jobs)
    python -m bench.capacity --mode batch --batch-jobs 200 --batch-concurrency 4 ...

    # mixed: live ramp alone, then the same ramp with a batch backlog running
    python -m bench.capacity --mode mixed ...

Live: each step starts N streams with staggered starts; every stream loops
over the audio pool (real-time paced, finalize at the end of each file) until
the step's duration has elapsed. Per step it records finalize->final and
time-to-first-partial p50/p95/p99, error codes (capacity / overloaded / lag),
dropped connections, the error rate, accuracy degradation versus a solo
(1-stream) pass over the same files, and ``/metrics`` + ``/v1/capacity``
snapshots before, during and after the step. The ramp stops at the first step
that breaches a threshold; the last passing step is the measured capacity.

Run it from a separate machine on the same network as Dograh, so network
latency is realistic and the client does not compete with the server for CPU.
One asyncio process drives a few hundred streams; check the client's own CPU
(it is printed per step) and shard across processes/machines beyond that.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

from .audio import wav_bytes
from .client import StreamConfig, StreamRecord, scrape, stream_once, transcribe
from .dataset import is_verified
from .metrics import edit_counts, summarize
from .normalize import normalize_tokens
from .runs import add_corpus_args, add_server_args, item_samples, select_items
from .safety import prepare_output_dir

DEFAULT_STEPS = "1,5,10,20,50,100,200"


@dataclass
class PoolItem:
    id: str
    samples: np.ndarray
    language: str
    reference: str | None
    speech_start: float | None = None
    speech_end: float | None = None
    sample_rate: int = 8000

    @property
    def seconds(self) -> float:
        return len(self.samples) / float(self.sample_rate)


@dataclass
class Thresholds:
    max_p95_final_ms: float = 300.0
    max_error_rate: float = 0.01
    max_wer_delta: float = 0.02


def _cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


# ---------------------------------------------------------------------------
# Live ramp
# ---------------------------------------------------------------------------


async def _stream_worker(pool: list[PoolItem], cfg: StreamConfig, start_index: int, delay_s: float,
                         deadline: float, out: list[tuple[PoolItem, StreamRecord]]) -> None:
    loop = asyncio.get_running_loop()
    await asyncio.sleep(delay_s)
    i = start_index
    while loop.time() < deadline:
        item = pool[i % len(pool)]
        i += 1
        item_cfg = StreamConfig(**{**cfg.__dict__, "language": item.language})
        rec = await stream_once(item_cfg, item.samples, item_id=item.id,
                                speech_start_s=item.speech_start, speech_end_s=item.speech_end)
        out.append((item, rec))
        if not rec.ok:
            await asyncio.sleep(0.5)  # do not hot-loop on rejections


def _is_failure(item: PoolItem, rec: StreamRecord) -> bool:
    expects_text = bool(normalize_tokens(item.reference or "", item.language)) if item.reference is not None else True
    return (not rec.ok) or (rec.no_final and expects_text)


def accuracy_vs_solo(records: list[tuple[PoolItem, StreamRecord]], solo: dict[str, str]) -> dict:
    """WER delta vs the 1-stream pass on the same files.

    wer_delta     (errors_step - errors_solo) / ref_words, against verified references
    wer_vs_solo   step hypothesis scored against the solo hypothesis (divergence),
                  used when the pool has no verified references
    """
    ref_err = solo_err = ref_words = div_err = div_words = 0
    for item, rec in records:
        if not rec.ok or item.id not in solo:
            continue
        hyp = normalize_tokens(rec.hypothesis, item.language)
        solo_toks = normalize_tokens(solo[item.id], item.language)
        d = edit_counts(solo_toks, hyp)
        div_err += d.errors
        div_words += d.ref_len
        if item.reference is not None:
            ref = normalize_tokens(item.reference, item.language)
            if ref:
                ref_err += edit_counts(ref, hyp).errors
                solo_err += edit_counts(ref, solo_toks).errors
                ref_words += len(ref)
    return {
        "wer_delta": (ref_err - solo_err) / ref_words if ref_words else None,
        "wer_step": ref_err / ref_words if ref_words else None,
        "wer_solo": solo_err / ref_words if ref_words else None,
        "wer_vs_solo": div_err / div_words if div_words else None,
        "ref_words": ref_words,
    }


def _counter_delta(before: dict | None, after: dict | None) -> dict:
    if not before or not after:
        return {}
    return {k: round(after[k] - before.get(k, 0.0), 3) for k in after
            if (k.endswith("_total") or "_total{" in k) and after[k] != before.get(k, 0.0)}


async def run_live_step(n: int, pool: list[PoolItem], cfg: StreamConfig, *, duration_s: float,
                        stagger_ms: float, http: httpx.AsyncClient, url: str, api_key: str | None,
                        solo: dict[str, str]) -> dict:
    loop = asyncio.get_running_loop()
    before = await scrape(http, url, api_key)
    cpu0, t0 = _cpu_seconds(), loop.time()
    deadline = t0 + duration_s
    records: list[tuple[PoolItem, StreamRecord]] = []
    tasks = [asyncio.create_task(_stream_worker(pool, cfg, k, k * stagger_ms / 1000, deadline, records))
             for k in range(n)]

    async def mid_scrape():
        await asyncio.sleep(duration_s / 2)
        return await scrape(http, url, api_key)

    mid_task = asyncio.create_task(mid_scrape())
    await asyncio.gather(*tasks)
    mid = await mid_task
    after = await scrape(http, url, api_key)
    wall = loop.time() - t0
    recs = [r for _, r in records]
    attempted = len(records)
    failures = sum(1 for it, r in records if _is_failure(it, r))
    codes: dict[str, int] = {}
    for r in recs:
        for c in r.errors:
            codes[c] = codes.get(c, 0) + 1
    ok = [r for r in recs if r.ok]
    return {
        "streams": n, "duration_s": round(wall, 2), "attempted": attempted, "completed": len(ok),
        "failures": failures, "error_rate": failures / attempted if attempted else None,
        "error_codes": codes,
        "capacity_rejections": codes.get("capacity", 0),
        "overloaded": codes.get("overloaded", 0),
        "dropped": sum(1 for r in recs if r.dropped),
        "no_final": sum(1 for r in recs if r.no_final),
        "finalize_to_final_ms": summarize(r.finalize_to_final_ms for r in ok),
        "ttfp_ms": summarize(r.ttfp_ms for r in ok),
        "connect_ms": summarize(r.connect_ms for r in ok),
        "accuracy": accuracy_vs_solo(records, solo),
        "client_cpu_util": round((_cpu_seconds() - cpu0) / wall, 3) if wall > 0 else None,
        "server": {"before": before, "mid": mid, "after": after,
                   "counter_delta": _counter_delta(before.get("metrics"), after.get("metrics"))},
    }


def breaches(step: dict, th: Thresholds) -> list[str]:
    out = []
    p95 = step["finalize_to_final_ms"]["p95"]
    if p95 is None:
        out.append("no completed stream (no finalize->final latency)")
    elif p95 > th.max_p95_final_ms:
        out.append(f"finalize->final p95 {p95:.0f} ms > {th.max_p95_final_ms:.0f}")
    if step["error_rate"] is None or step["error_rate"] > th.max_error_rate:
        out.append(f"error rate {step['error_rate']} > {th.max_error_rate}")
    acc = step["accuracy"]
    delta = acc["wer_delta"] if acc["wer_delta"] is not None else acc["wer_vs_solo"]
    if delta is not None and delta > th.max_wer_delta:
        out.append(f"WER delta vs solo {delta:.4f} > {th.max_wer_delta}")
    return out


async def solo_pass(pool: list[PoolItem], cfg: StreamConfig) -> tuple[dict[str, str], list[StreamRecord]]:
    hyps, recs = {}, []
    for item in pool:
        item_cfg = StreamConfig(**{**cfg.__dict__, "language": item.language})
        rec = await stream_once(item_cfg, item.samples, item_id=item.id,
                                speech_start_s=item.speech_start, speech_end_s=item.speech_end)
        recs.append(rec)
        if rec.ok:
            hyps[item.id] = rec.hypothesis
    return hyps, recs


async def live_ramp(pool: list[PoolItem], cfg: StreamConfig, steps: list[int], *, duration_s: float,
                    stagger_ms: float, cooldown_s: float, th: Thresholds, url: str, api_key: str | None,
                    solo: dict[str, str], label: str = "live", log=print) -> dict:
    results = []
    capacity = None
    stopped = None
    async with httpx.AsyncClient() as http:
        for n in steps:
            step = await run_live_step(n, pool, cfg, duration_s=duration_s, stagger_ms=stagger_ms,
                                       http=http, url=url, api_key=api_key, solo=solo)
            step["breaches"] = breaches(step, th)
            step["passed"] = not step["breaches"]
            results.append(step)
            f = step["finalize_to_final_ms"]
            log(f"[{label}] {n:>4} streams: {step['completed']}/{step['attempted']} ok, error rate "
                f"{step['error_rate']}, finalize->final p50/p95/p99 {f['p50']}/{f['p95']}/{f['p99']} ms, "
                f"client CPU {step['client_cpu_util']}" + (f"  BREACH: {'; '.join(step['breaches'])}" if step["breaches"] else ""))
            if step["breaches"]:
                stopped = {"at_streams": n, "reasons": step["breaches"]}
                break
            capacity = n
            await asyncio.sleep(cooldown_s)
    return {"steps": results, "measured_capacity_streams": capacity, "stopped": stopped,
            "thresholds": th.__dict__}


# ---------------------------------------------------------------------------
# Batch throughput
# ---------------------------------------------------------------------------


async def batch_throughput(pool: list[PoolItem], *, url: str, api_key: str | None, model: str | None,
                           jobs: int, concurrency: int, mode: str, stop: asyncio.Event | None = None) -> dict:
    """Submit ``jobs`` transcriptions (cycling the pool) with ``concurrency`` in flight.

    With ``stop`` set, keeps submitting until the event fires (mixed mode backlog).
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    results: list[dict] = []
    counter = {"next": 0}
    t0 = time.perf_counter()

    async with httpx.AsyncClient(headers=headers) as client:
        async def worker() -> None:
            while True:
                if stop is not None:
                    if stop.is_set():
                        return
                elif counter["next"] >= jobs:
                    return
                k = counter["next"]
                counter["next"] += 1
                item = pool[k % len(pool)]
                res = await transcribe(client, url, wav_bytes(item.samples, item.sample_rate), filename=f"{item.id}.wav",
                                       model=model, language=item.language, mode=mode)
                res["audio_s"] = item.seconds
                res.pop("text", None)  # never keep transcript text in capacity results
                results.append(res)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall = time.perf_counter() - t0
    ok = [r for r in results if not r.get("error")]
    audio = sum(r["audio_s"] for r in ok)
    return {
        "jobs": len(results), "succeeded": len(ok), "failed": len(results) - len(ok),
        "error_rate": (len(results) - len(ok)) / len(results) if results else None,
        "http_429": sum(r.get("attempts_429") or 0 for r in results),
        "concurrency": concurrency, "mode": mode, "wall_s": round(wall, 2),
        "audio_s": round(audio, 2), "throughput_audio_s_per_s": round(audio / wall, 3) if wall > 0 else None,
        "client_ms": summarize(r.get("client_ms") for r in ok),
        "server_queue_ms": summarize((r.get("timings") or {}).get("queue_ms") for r in ok),
        "server_inference_ms": summarize((r.get("timings") or {}).get("inference_ms") for r in ok),
        "rtf": summarize(((r.get("timings") or {}).get("inference_ms") or 0) / 1000 / r["audio_s"]
                         for r in ok if r["audio_s"] > 0 and (r.get("timings") or {}).get("inference_ms") is not None),
        "client_queue_wait_ms": summarize(r.get("queue_wait_ms_client") for r in ok),
    }


async def mixed(pool, cfg, steps, *, args, th, solo, log=print) -> dict:
    out: dict = {}
    if not args.skip_live_only:
        out["live_only"] = await live_ramp(pool, cfg, steps, duration_s=args.step_duration,
                                           stagger_ms=args.stagger_ms, cooldown_s=args.cooldown,
                                           th=th, url=args.url, api_key=args.api_key, solo=solo,
                                           label="live-only", log=log)
    stop = asyncio.Event()
    batch_task = asyncio.create_task(batch_throughput(
        pool, url=args.url, api_key=args.api_key, model=args.batch_model, jobs=0,
        concurrency=args.batch_concurrency, mode=args.batch_mode, stop=stop))
    await asyncio.sleep(args.batch_warmup)
    try:
        out["with_batch"] = await live_ramp(pool, cfg, steps, duration_s=args.step_duration,
                                            stagger_ms=args.stagger_ms, cooldown_s=args.cooldown,
                                            th=th, url=args.url, api_key=args.api_key, solo=solo,
                                            label="live+batch", log=log)
    finally:
        stop.set()
        out["batch_backlog"] = await batch_task
    if "live_only" in out:
        base = {s["streams"]: s for s in out["live_only"]["steps"]}
        cmp_ = []
        for s in out["with_batch"]["steps"]:
            b = base.get(s["streams"])
            if not b:
                continue
            p_a, p_b = s["finalize_to_final_ms"]["p95"], b["finalize_to_final_ms"]["p95"]
            t_a, t_b = s["ttfp_ms"]["p95"], b["ttfp_ms"]["p95"]
            cmp_.append({"streams": s["streams"],
                         "finalize_final_p95_live_only": p_b, "finalize_final_p95_with_batch": p_a,
                         "finalize_final_p95_delta_ms": None if p_a is None or p_b is None else round(p_a - p_b, 1),
                         "ttfp_p95_delta_ms": None if t_a is None or t_b is None else round(t_a - t_b, 1),
                         "error_rate_live_only": b["error_rate"], "error_rate_with_batch": s["error_rate"]})
        out["batch_impact"] = cmp_
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_pool(args) -> list[PoolItem]:
    args.all_items = True  # the pool may use unverified items; accuracy uses only verified refs
    corpus, items = select_items(args)
    items = items[: args.pool_size] if args.pool_size else items
    pool = []
    for it in items:
        pool.append(PoolItem(id=it["id"], samples=item_samples(corpus, it, args.sample_rate),
                             language=it.get("language", "en"),
                             reference=it.get("reference") if is_verified(it) else None,
                             speech_start=it.get("speech_start"), speech_end=it.get("speech_end"),
                             sample_rate=args.sample_rate))
    return pool


def write_outputs(out: Path, result: dict) -> None:
    (out / "capacity.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    rows = []
    for phase in ("live", "live_only", "with_batch"):
        for s in (result.get(phase) or {}).get("steps", []):
            rows.append({"phase": phase, "streams": s["streams"], "attempted": s["attempted"],
                         "completed": s["completed"], "error_rate": s["error_rate"],
                         "capacity_rejections": s["capacity_rejections"], "overloaded": s["overloaded"],
                         "dropped": s["dropped"],
                         **{f"final_{k}": s["finalize_to_final_ms"][k] for k in ("p50", "p95", "p99")},
                         **{f"ttfp_{k}": s["ttfp_ms"][k] for k in ("p50", "p95", "p99")},
                         "wer_delta": s["accuracy"]["wer_delta"], "wer_vs_solo": s["accuracy"]["wer_vs_solo"],
                         "client_cpu_util": s["client_cpu_util"], "passed": s["passed"],
                         "breaches": "; ".join(s["breaches"])})
    if rows:
        with (out / "steps.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    lines = ["# HopSTT capacity test", "", "Aggregates only; no transcript text.", ""]
    for phase in ("live", "live_only", "with_batch"):
        r = result.get(phase)
        if not r:
            continue
        lines += [f"## {phase}: measured capacity = {r['measured_capacity_streams']} streams", "",
                  f"Stopped: {r['stopped']}", "",
                  "| streams | ok/attempted | error rate | final p50/p95/p99 ms | TTFP p95 ms | WER delta | breaches |",
                  "|---|---|---|---|---|---|---|"]
        for s in r["steps"]:
            f = s["finalize_to_final_ms"]
            d = s["accuracy"]["wer_delta"] if s["accuracy"]["wer_delta"] is not None else s["accuracy"]["wer_vs_solo"]
            lines.append(f"| {s['streams']} | {s['completed']}/{s['attempted']} | {s['error_rate']} | "
                         f"{f['p50']}/{f['p95']}/{f['p99']} | {s['ttfp_ms']['p95']} | {d} | {'; '.join(s['breaches'])} |")
        lines.append("")
    if result.get("batch"):
        b = result["batch"]
        lines += ["## batch throughput", "", f"{b['succeeded']}/{b['jobs']} jobs, concurrency {b['concurrency']}, "
                  f"{b['throughput_audio_s_per_s']} audio-s/s, RTF p50 {b['rtf']['p50']}, "
                  f"queue p95 {b['server_queue_ms']['p95']} ms, 429s {b['http_429']}", ""]
    if result.get("batch_impact"):
        lines += ["## live latency impact of batch backlog", "",
                  "| streams | final p95 live-only | final p95 with batch | delta ms |", "|---|---|---|---|"]
        for c in result["batch_impact"]:
            lines.append(f"| {c['streams']} | {c['finalize_final_p95_live_only']} | "
                         f"{c['finalize_final_p95_with_batch']} | {c['finalize_final_p95_delta_ms']} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bench.capacity", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_server_args(ap)
    add_corpus_args(ap)
    ap.add_argument("--mode", choices=["live", "batch", "mixed"], default="live")
    ap.add_argument("--pool-size", type=int, default=20, help="number of files in the audio pool")
    ap.add_argument("--steps", default=DEFAULT_STEPS)
    ap.add_argument("--step-duration", type=float, default=60.0, help="seconds per step")
    ap.add_argument("--stagger-ms", type=float, default=100.0, help="delay between stream starts in a step")
    ap.add_argument("--cooldown", type=float, default=5.0, help="seconds between steps")
    ap.add_argument("--model")
    ap.add_argument("--chunk-ms", type=int, default=320)
    ap.add_argument("--frame-ms", type=int, default=20)
    ap.add_argument("--encoding", choices=["pcm_s16le", "mulaw", "alaw"], default="pcm_s16le")
    ap.add_argument("--sample-rate", type=int, choices=[8000, 16000], default=8000)
    ap.add_argument("--speed", type=float, default=1.0, help="1.0 = real time (keep 1.0 for real capacity numbers)")
    ap.add_argument("--final-timeout", type=float, default=5.0)
    ap.add_argument("--max-reconnects", type=int, default=0,
                    help="reconnect attempts per stream (default 0: every failure counts)")
    ap.add_argument("--no-solo", action="store_true", help="skip the 1-stream reference pass (no WER delta)")
    ap.add_argument("--max-p95-final-ms", type=float, default=300.0)
    ap.add_argument("--max-error-rate", type=float, default=0.01)
    ap.add_argument("--max-wer-delta", type=float, default=0.02)
    ap.add_argument("--batch-model")
    ap.add_argument("--batch-mode", choices=["sync", "jobs"], default="jobs")
    ap.add_argument("--batch-jobs", type=int, default=100)
    ap.add_argument("--batch-concurrency", type=int, default=4)
    ap.add_argument("--batch-warmup", type=float, default=5.0, help="mixed: seconds of backlog before the ramp")
    ap.add_argument("--skip-live-only", action="store_true", help="mixed: do not run the live-only ramp first")
    ap.add_argument("--out", help="output directory (default $HOPSTT_BENCH_OUT/capacity-<ts>)")
    return ap


async def run(args, log=print) -> dict:
    pool = load_pool(args)
    if not pool:
        raise SystemExit("empty audio pool")
    cfg = StreamConfig(url=args.url, api_key=args.api_key, ws_url=args.ws_url, model=args.model,
                       sample_rate=args.sample_rate, encoding=args.encoding, chunk_ms=args.chunk_ms,
                       frame_ms=args.frame_ms, speed=args.speed, final_timeout_s=args.final_timeout,
                       max_reconnects=args.max_reconnects)
    th = Thresholds(args.max_p95_final_ms, args.max_error_rate, args.max_wer_delta)
    steps = [int(s) for s in args.steps.split(",") if s.strip()]
    result: dict = {"mode": args.mode, "pool_size": len(pool), "pool_audio_s": round(sum(p.seconds for p in pool), 1),
                    "config": {k: getattr(args, k) for k in ("model", "chunk_ms", "frame_ms", "encoding",
                                                             "sample_rate", "speed", "step_duration", "stagger_ms")}}
    solo: dict[str, str] = {}
    if args.mode in ("live", "mixed") and not args.no_solo:
        solo, recs = await solo_pass(pool, cfg)
        result["solo"] = {"n": len(recs), "ok": sum(r.ok for r in recs),
                          "finalize_to_final_ms": summarize(r.finalize_to_final_ms for r in recs if r.ok)}
        log(f"solo pass: {result['solo']['ok']}/{len(recs)} ok")
    if args.mode == "live":
        result["live"] = await live_ramp(pool, cfg, steps, duration_s=args.step_duration, stagger_ms=args.stagger_ms,
                                         cooldown_s=args.cooldown, th=th, url=args.url, api_key=args.api_key,
                                         solo=solo, log=log)
    elif args.mode == "batch":
        result["batch"] = await batch_throughput(pool, url=args.url, api_key=args.api_key, model=args.batch_model,
                                                 jobs=args.batch_jobs, concurrency=args.batch_concurrency,
                                                 mode=args.batch_mode)
    else:
        result.update(await mixed(pool, cfg, steps, args=args, th=th, solo=solo, log=log))
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = prepare_output_dir(args.out, prefix="capacity", allow_in_repo=args.allow_in_repo)
    result = asyncio.run(run(args))
    write_outputs(out, result)
    for phase in ("live", "live_only", "with_batch"):
        if result.get(phase):
            print(f"{phase}: measured capacity {result[phase]['measured_capacity_streams']} streams")
    print(f"results written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
