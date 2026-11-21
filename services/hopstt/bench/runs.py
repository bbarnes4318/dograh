"""Shared helpers for runners: item selection, audio loading, run directories.

A run directory holds:
  run.json      metadata (engine label, model, parameters, counts) -- no text
  items.jsonl   one row per item: id, hypothesis (PRIVATE TEXT), timings, errors
Run directories are as sensitive as the corpus (they contain transcripts) and
are written owner-only, outside the repo.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np

from .audio import load_audio
from .dataset import is_verified, load_manifest, manifest_path
from .safety import add_safety_args, resolve_corpus, write_jsonl_private


def add_corpus_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--corpus", help="private corpus directory (or $HOPSTT_EVAL_CORPUS)")
    ap.add_argument("--manifest", help="manifest path (default <corpus>/manifest.jsonl)")
    ap.add_argument("--all-items", action="store_true",
                    help="include unverified items (default: verified only; unverified never count toward accuracy)")
    ap.add_argument("--language", choices=["en", "es"], help="only this language")
    ap.add_argument("--tags", help="comma-separated: only items with any of these tags")
    ap.add_argument("--limit", type=int, help="first N items after filtering")
    add_safety_args(ap)


def add_server_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--url", default=os.environ.get("HOPSTT_URL", "http://127.0.0.1:8788"),
                    help="HopSTT base URL (or $HOPSTT_URL)")
    ap.add_argument("--ws-url", help="override the WebSocket URL (default <url>/v1/stt/stream)")
    ap.add_argument("--api-key", default=os.environ.get("HOPSTT_API_KEY"),
                    help="bearer key (default $HOPSTT_API_KEY)")


def select_items(args) -> tuple[Path, list[dict]]:
    corpus = resolve_corpus(args.corpus, allow_in_repo=args.allow_in_repo)
    items = load_manifest(manifest_path(corpus, args.manifest))
    if not getattr(args, "all_items", False):
        items = [it for it in items if is_verified(it)]
    if getattr(args, "language", None):
        items = [it for it in items if it.get("language") == args.language]
    if getattr(args, "tags", None):
        wanted = {t.strip() for t in args.tags.split(",") if t.strip()}
        items = [it for it in items if wanted & set(it.get("tags") or [])]
    if getattr(args, "limit", None):
        items = items[: args.limit]
    return corpus, items


def item_samples(corpus: Path, item: dict, sample_rate: int) -> np.ndarray:
    return load_audio(corpus / item["audio"], sample_rate=sample_rate, channel=item.get("channel"),
                      start=item.get("start"), end=item.get("end"))


def write_run(run_dir: Path, meta: dict, rows: list[dict]) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    meta = dict(meta)
    meta.setdefault("written_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    meta["n_items"] = len(rows)
    meta["n_errors"] = sum(1 for r in rows if r.get("error") or r.get("error_code"))
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    write_jsonl_private(run_dir / "items.jsonl", rows)
    return meta


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run_dir / "run.json").read_text())
    rows = [json.loads(x) for x in (run_dir / "items.jsonl").read_text().splitlines() if x.strip()]
    return meta, rows


def find_runs(paths: list[str]) -> list[Path]:
    """Accept run dirs or parents containing run dirs (one level deep)."""
    out = []
    for p in map(Path, paths):
        if (p / "run.json").is_file():
            out.append(p)
        elif p.is_dir():
            out += sorted(d for d in p.iterdir() if (d / "run.json").is_file())
    return out


def hypotheses_from_rows(rows: list[dict]) -> dict[str, str | None]:
    """id -> hypothesis; failed items map to None (scored as empty output)."""
    out: dict[str, str | None] = {}
    for r in rows:
        failed = bool(r.get("error")) or r.get("ok") is False
        text = r.get("hypothesis")
        out[r["id"]] = None if failed and not text else (text or "")
    return out


def safe_label(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_.@" else "_" for c in label)
