"""Streaming accuracy + latency runner over the live WebSocket protocol.

    # accuracy + latency at one chunk size
    python -m bench.run_stream --url http://gpu-host:8788 --corpus /secure/hopstt-corpus \
        --models nemotron-3.5-streaming --chunk-ms 320 --out /secure/hopstt-runs/stream

    # chunk-size sweep -> sweep.csv / sweep.md (accuracy vs latency)
    python -m bench.run_stream ... --chunk-ms 80,160,320,560,1120

    # recovery test: abort the socket half-way through every item and reconnect
    python -m bench.run_stream ... --kill-fraction 0.5

Each item is streamed at real-time pace (``--speed 1``; ``0`` = as fast as
possible) in ``--frame-ms`` frames, encoded as ``--encoding`` at
``--sample-rate``; ``finalize`` is sent at the end of the item audio, or at
``speech_end + --vad-stop-ms`` with ``--finalize-at speech_end`` (simulated VAD
stop). See bench/client.py ``StreamRecord`` for exactly what each latency
includes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from .client import StreamConfig, stream_once
from .dataset import is_verified, load_manifest, manifest_path
from .metrics import aggregate, score_engine, summarize
from .runs import (add_corpus_args, add_server_args, hypotheses_from_rows, item_samples, safe_label,
                   select_items, write_run)
from .safety import prepare_output_dir

LATENCY_KEYS = ["connect_ms", "ttfp_ms", "finalize_to_final_ms", "speech_end_to_final_ms",
                "first_partial_to_final_ms", "last_partial_to_final_ms"]


def latency_summary(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    out = {k: summarize(r.get(k) for r in ok) for k in LATENCY_KEYS}
    out["revision_rate"] = summarize(r.get("revision_rate") for r in ok)
    out["partial_edit_mean"] = summarize(r.get("partial_edit_mean") for r in ok)
    out["final_vs_last_partial_edit"] = summarize(r.get("final_vs_last_partial_edit") for r in ok)
    out["wall_rtf"] = summarize(r.get("wall_rtf") for r in ok)
    return out


def failure_summary(rows: list[dict]) -> dict:
    n = len(rows)
    failed = [r for r in rows if not r.get("ok")]
    codes: dict[str, int] = {}
    for r in rows:
        for c in r.get("errors") or []:
            codes[c] = codes.get(c, 0) + 1
    killed = [r for r in rows if r.get("killed")]
    finals = sum(r.get("n_finals", 0) for r in rows)
    dups = sum(r.get("duplicate_finals", 0) for r in rows)
    return {
        "n_streams": n, "failed": len(failed), "failure_rate": len(failed) / n if n else None,
        "dropped": sum(1 for r in rows if r.get("dropped")),
        "no_final": sum(1 for r in rows if r.get("no_final")),
        "error_codes": codes,
        "recovery": {
            "n_killed": len(killed),
            "recovered": sum(1 for r in killed if r.get("recovered")),
            "success_rate": (sum(1 for r in killed if r.get("recovered")) / len(killed)) if killed else None,
            "recovery_ms": summarize(r.get("recovery_ms") for r in killed),
            "duplicate_finals": dups,
            "duplicate_final_rate": dups / finals if finals else None,
            "stale_dropped": sum(r.get("stale_dropped", 0) for r in rows),
        },
    }


async def run_config(args, corpus: Path, items: list[dict], model: str | None, chunk_ms: int | None) -> tuple[dict, list[dict]]:
    cfg = StreamConfig(
        url=args.url, api_key=args.api_key, ws_url=args.ws_url, model=model,
        sample_rate=args.sample_rate, encoding=args.encoding, chunk_ms=chunk_ms,
        frame_ms=args.frame_ms if args.frame_ms > 0 else (chunk_ms or 20), speed=args.speed,
        interim_results=not args.no_interims, finalize_at=args.finalize_at, vad_stop_ms=args.vad_stop_ms,
        final_timeout_s=args.final_timeout, max_reconnects=args.max_reconnects,
    )
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []

    async def one(idx: int, item: dict) -> None:
        samples = item_samples(corpus, item, args.sample_rate)
        dur = len(samples) / args.sample_rate
        kill_at = None
        if (args.kill_fraction is not None or args.kill_at_s is not None) and idx % args.kill_every == 0:
            kill_at = dur * args.kill_fraction if args.kill_fraction is not None else min(args.kill_at_s, dur * 0.9)
        item_cfg = StreamConfig(**{**cfg.__dict__, "language": item.get("language", "en")})
        async with sem:
            rec = await stream_once(item_cfg, samples, item_id=item["id"],
                                    speech_start_s=item.get("speech_start"),
                                    speech_end_s=item.get("speech_end"), kill_at_s=kill_at)
        row = rec.to_row()
        row["language"] = item.get("language")
        row["error"] = None if rec.ok else (rec.error_code or "failed")
        rows.append(row)

    await asyncio.gather(*(one(i, it) for i, it in enumerate(items)))
    order = {it["id"]: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: order[r["id"]])
    meta = {
        "kind": "stream", "engine": f"stream:{model or 'default'}@{chunk_ms or 'default'}ms",
        "model": model, "chunk_ms": chunk_ms, "frame_ms": cfg.frame_ms, "encoding": cfg.encoding,
        "sample_rate": cfg.sample_rate, "speed": cfg.speed, "finalize_at": cfg.finalize_at,
        "vad_stop_ms": cfg.vad_stop_ms, "concurrency": args.concurrency,
        "latency": latency_summary(rows), "failures": failure_summary(rows),
    }
    return meta, rows


def sweep_rows(manifest_items: list[dict], runs: list[tuple[dict, list[dict]]]) -> list[dict]:
    """Accuracy-vs-latency table (aggregates only, no text)."""
    by_id = {it["id"]: it for it in manifest_items if is_verified(it)}  # accuracy: verified only
    table = []
    for meta, rows in runs:
        items = [by_id[r["id"]] for r in rows if r["id"] in by_id]
        agg = aggregate(score_engine(items, hypotheses_from_rows(rows)))
        lat = meta["latency"]
        table.append({
            "engine": meta["engine"], "model": meta["model"], "chunk_ms": meta["chunk_ms"],
            "n_items": len(items), "wer": agg["overall"]["wer"],
            "wer_en": (agg["by_language"].get("en") or {}).get("wer"),
            "wer_es": (agg["by_language"].get("es") or {}).get("wer"),
            "field_accuracy": agg["fields"]["accuracy"],
            "negative_flip_rate": agg["negation"]["negative_flip_rate"],
            "ttfp_p50_ms": lat["ttfp_ms"]["p50"], "ttfp_p95_ms": lat["ttfp_ms"]["p95"],
            "finalize_final_p50_ms": lat["finalize_to_final_ms"]["p50"],
            "finalize_final_p95_ms": lat["finalize_to_final_ms"]["p95"],
            "finalize_final_p99_ms": lat["finalize_to_final_ms"]["p99"],
            "revision_rate_mean": lat["revision_rate"]["mean"],
            "failure_rate": meta["failures"]["failure_rate"],
        })
    return table


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}" if v < 10 else f"{v:.0f}"
    return str(v)


def write_sweep(out: Path, table: list[dict]) -> None:
    if not table:
        return
    with (out / "sweep.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    cols = ["engine", "n_items", "wer", "wer_en", "wer_es", "field_accuracy", "negative_flip_rate",
            "ttfp_p95_ms", "finalize_final_p50_ms", "finalize_final_p95_ms", "revision_rate_mean", "failure_rate"]
    lines = ["# Chunk-size sweep (accuracy vs latency)", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_fmt(r[c]) for c in cols) + " |" for r in table]
    (out / "sweep.md").write_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bench.run_stream", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_server_args(ap)
    add_corpus_args(ap)
    ap.add_argument("--models", default="", help="comma-separated streaming model ids (empty = server default)")
    ap.add_argument("--chunk-ms", default="320", help="comma-separated server chunk sizes; >1 value = sweep")
    ap.add_argument("--frame-ms", type=int, default=20,
                    help="client send frame size in ms (Dograh sends ~20 ms); 0 = same as chunk-ms")
    ap.add_argument("--encoding", choices=["pcm_s16le", "mulaw", "alaw"], default="pcm_s16le")
    ap.add_argument("--sample-rate", type=int, choices=[8000, 16000], default=8000)
    ap.add_argument("--speed", type=float, default=1.0, help="1.0 = real time, 0 = as fast as possible")
    ap.add_argument("--finalize-at", choices=["end", "speech_end"], default="end")
    ap.add_argument("--vad-stop-ms", type=int, default=200)
    ap.add_argument("--no-interims", action="store_true")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel streams (1 for clean latency; use capacity.py for load)")
    ap.add_argument("--final-timeout", type=float, default=5.0)
    ap.add_argument("--max-reconnects", type=int, default=3)
    ap.add_argument("--kill-fraction", type=float, help="recovery test: abort socket at this fraction of each item")
    ap.add_argument("--kill-at-s", type=float, help="recovery test: abort socket at this second of each item")
    ap.add_argument("--kill-every", type=int, default=1, help="recovery test: only every Nth item")
    ap.add_argument("--out", help="output directory (default $HOPSTT_BENCH_OUT/stream-<ts>)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    corpus, items = select_items(args)
    if not items:
        print("no items selected", file=sys.stderr)
        return 1
    out = prepare_output_dir(args.out, prefix="stream", allow_in_repo=args.allow_in_repo)
    models = [m.strip() for m in args.models.split(",") if m.strip()] or [None]
    chunks = [int(c) for c in args.chunk_ms.split(",") if c.strip()] or [None]
    runs = []
    for model in models:
        for chunk in chunks:
            meta, rows = asyncio.run(run_config(args, corpus, items, model, chunk))
            run_dir = out / safe_label(f"stream_{model or 'default'}@{chunk}ms")
            write_run(run_dir, meta, rows)
            lat, fail = meta["latency"], meta["failures"]
            print(f"{meta['engine']}: {len(rows)} streams, {fail['failed']} failed, "
                  f"TTFP p95 {lat['ttfp_ms']['p95']} ms, finalize->final p95 "
                  f"{lat['finalize_to_final_ms']['p95']} ms -> {run_dir}")
            runs.append((meta, rows))
    manifest_items = load_manifest(manifest_path(corpus, args.manifest))
    table = sweep_rows(manifest_items, runs)
    write_sweep(out, table)
    (out / "sweep.json").write_text(json.dumps(table, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
