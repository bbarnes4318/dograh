"""Build a public, telephone-simulated proxy corpus from Google FLEURS.

FLEURS (https://huggingface.co/datasets/google/fleurs, CC-BY-4.0) is read
speech, not phone calls. Each clip is band-limited to 300-3400 Hz, resampled to
8 kHz and passed through a G.711 mu-law encode/decode, which approximates the
codec path of a PSTN call. Use it to compare engines on identical audio and to
measure latency/capacity before the private, authorised call corpus exists;
never present its numbers as call-audio accuracy.

    python -m bench.public_corpus --out /data/fleurs-tel --en 140 --es 60

Writes ``<out>/manifest.jsonl`` (bench manifest format, references verified by
the FLEURS authors) and ``<out>/audio/*.wav`` (PCM16 8 kHz mono). Items are
interleaved by language so ``--limit`` keeps the EN/ES mix.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from .safety import guard_path

BASE = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"
SETS = (("en_us", "en"), ("es_419", "es"))


def _download(url: str, dst: Path) -> None:
    if dst.exists() and dst.stat().st_size > 0:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    tmp.rename(dst)


def _telephone(raw: bytes, dst: Path) -> None:
    mu = dst.with_suffix(".mu.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", "pipe:0", "-af", "highpass=f=300,lowpass=f=3400",
             "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", "-f", "wav", str(mu)],
            input=raw, check=True, timeout=120,
        )
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mu), "-c:a", "pcm_s16le", str(dst)],
                       check=True, timeout=120)
    finally:
        mu.unlink(missing_ok=True)


def build(out: Path, counts: dict[str, int], cache: Path, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    (out / "audio").mkdir(parents=True, exist_ok=True)
    per_lang: dict[str, list[dict]] = {}
    for fleurs_code, lang in SETS:
        n = counts.get(lang, 0)
        if n <= 0:
            continue
        tsv, tar = cache / fleurs_code / "test.tsv", cache / fleurs_code / "test.tar.gz"
        _download(f"{BASE}/{fleurs_code}/test.tsv", tsv)
        _download(f"{BASE}/{fleurs_code}/audio/test.tar.gz", tar)
        rows, seen = [], set()
        with open(tsv, encoding="utf-8") as fh:
            for r in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                if r[0] not in seen:  # one recording per sentence
                    seen.add(r[0])
                    rows.append(r)
        pick = {r[1]: r for r in rng.sample(rows, min(n, len(rows)))}
        items = []
        with tarfile.open(tar) as tf:
            for member in tf:
                name = os.path.basename(member.name)
                if name not in pick:
                    continue
                iid = f"fl{lang}{len(items):03d}"
                _telephone(tf.extractfile(member).read(), out / "audio" / f"{iid}.wav")
                items.append({
                    "id": iid, "audio": f"audio/{iid}.wav", "language": lang,
                    "reference": pick[name][2], "reference_status": "verified",
                    "tags": ["read_speech", "telephone_sim", "public_fleurs"], "critical_fields": [],
                })
        per_lang[lang] = items
    # Interleave so any prefix keeps roughly the requested language mix.
    total = sum(len(v) for v in per_lang.values())
    queues = {k: list(v) for k, v in per_lang.items()}
    ordered: list[dict] = []
    taken = {k: 0 for k in queues}
    while len(ordered) < total:
        lang = min((k for k in queues if queues[k]),
                   key=lambda k: taken[k] / max(1, len(per_lang[k])))
        ordered.append(queues[lang].pop(0))
        taken[lang] += 1
    with open(out / "manifest.jsonl", "w", encoding="utf-8") as fh:
        for it in ordered:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    (out / "SOURCE.txt").write_text(
        "Google FLEURS test split (CC-BY-4.0, https://huggingface.co/datasets/google/fleurs), "
        "telephone-simulated (300-3400 Hz, 8 kHz, G.711 mu-law). Read speech: a proxy, "
        "not call audio.\n", encoding="utf-8")
    return ordered


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", help="download cache (default <out>/../fleurs-cache)")
    ap.add_argument("--en", type=int, default=140)
    ap.add_argument("--es", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--allow-in-repo", action="store_true")
    args = ap.parse_args(argv)
    out = guard_path(args.out, what="corpus", allow_in_repo=args.allow_in_repo)
    cache = Path(args.cache) if args.cache else out.parent / "fleurs-cache"
    items = build(out, {"en": args.en, "es": args.es}, cache, seed=args.seed)
    print(json.dumps({"items": len(items), "es": sum(i["language"] == "es" for i in items),
                      "manifest": str(out / "manifest.jsonl")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
