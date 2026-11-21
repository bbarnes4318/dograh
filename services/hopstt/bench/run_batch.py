"""Batch accuracy runner: send every item to HopSTT batch transcription.

    python -m bench.run_batch --url http://gpu-host:8788 --corpus /secure/hopstt-corpus \
        --models parakeet-tdt-0.6b-v3,nemotron-3.5-streaming --out /secure/hopstt-runs/batch

For each model a run directory ``batch_<model>/`` is written (run.json +
private items.jsonl with hypotheses). Each item is cut/split client-side to a
mono WAV at ``--sample-rate`` (default 8 kHz, i.e. what the phone network
delivers) and uploaded with ``channels=mix``.

Recorded per item: hypothesis, client wall time (upload + queue + decode +
inference + response), server ``timings`` (decode_ms, queue_ms, inference_ms),
RTF = server inference_ms / audio duration, errors.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

from .audio import wav_bytes
from .client import transcribe
from .metrics import summarize
from .runs import add_corpus_args, add_server_args, item_samples, safe_label, select_items, write_run
from .safety import prepare_output_dir


async def run_model(args, corpus, items, model: str | None) -> tuple[dict, list[dict]]:
    sem = asyncio.Semaphore(args.concurrency)
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    rows: list[dict] = []

    async with httpx.AsyncClient(headers=headers) as client:
        async def one(item: dict) -> None:
            samples = item_samples(corpus, item, args.sample_rate)
            audio_s = len(samples) / args.sample_rate
            async with sem:
                res = await transcribe(client, args.url, wav_bytes(samples, args.sample_rate),
                                       filename=f"{item['id']}.wav", model=model,
                                       language=item.get("language", "en"), mode=args.mode,
                                       priority=args.priority, timeout_s=args.timeout)
            timings = res.get("timings") or {}
            inf = timings.get("inference_ms")
            rows.append({
                "id": item["id"], "language": item.get("language"), "audio_s": round(audio_s, 3),
                "hypothesis": res.get("text"), "error": res.get("error"), "http_status": res.get("status"),
                "client_ms": res.get("client_ms"), "server_timings": timings or None,
                "rtf": round(inf / 1000 / audio_s, 4) if inf is not None and audio_s > 0 else None,
                "queue_wait_ms_client": res.get("queue_wait_ms_client"),
                "attempts_429": res.get("attempts_429"),
            })

        t0 = time.perf_counter()
        await asyncio.gather(*(one(it) for it in items))
        wall = time.perf_counter() - t0

    order = {it["id"]: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: order[r["id"]])
    audio_total = sum(r["audio_s"] for r in rows)
    meta = {
        "kind": "batch", "engine": f"batch:{model or 'default'}", "model": model, "mode": args.mode,
        "sample_rate": args.sample_rate, "concurrency": args.concurrency, "priority": args.priority,
        "wall_s": round(wall, 3), "audio_s": round(audio_total, 3),
        "throughput_audio_s_per_s": round(audio_total / wall, 3) if wall > 0 else None,
        "latency": {
            "client_ms": summarize(r["client_ms"] for r in rows if not r["error"]),
            "server_inference_ms": summarize((r["server_timings"] or {}).get("inference_ms") for r in rows),
            "server_queue_ms": summarize((r["server_timings"] or {}).get("queue_ms") for r in rows),
            "rtf": summarize(r["rtf"] for r in rows),
        },
    }
    return meta, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.run_batch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_server_args(ap)
    add_corpus_args(ap)
    ap.add_argument("--models", default="", help="comma-separated model ids (empty = server default)")
    ap.add_argument("--mode", choices=["sync", "jobs"], default="sync",
                    help="sync = POST /v1/transcriptions; jobs = POST /v1/jobs + poll")
    ap.add_argument("--priority", choices=["normal", "low"], default="normal")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel requests (1 for accuracy runs; use capacity.py for throughput)")
    ap.add_argument("--sample-rate", type=int, default=8000)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", help="output directory (default $HOPSTT_BENCH_OUT/batch-<ts>)")
    args = ap.parse_args(argv)

    corpus, items = select_items(args)
    if not items:
        print("no items selected", file=sys.stderr)
        return 1
    out = prepare_output_dir(args.out, prefix="batch", allow_in_repo=args.allow_in_repo)
    models = [m.strip() for m in args.models.split(",") if m.strip()] or [None]
    for model in models:
        meta, rows = asyncio.run(run_model(args, corpus, items, model))
        run_dir = out / safe_label(f"batch_{model or 'default'}")
        meta = write_run(run_dir, meta, rows)
        lat = meta["latency"]
        print(f"{meta['engine']}: {meta['n_items']} items, {meta['n_errors']} errors, "
              f"client p50 {lat['client_ms']['p50']} ms, RTF p50 {lat['rtf']['p50']} -> {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
