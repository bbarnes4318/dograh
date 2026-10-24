"""Sweep streaming parameters against one loaded model (model-side numbers).

Loads the Qwen backend once and re-runs the same phrases with different
first-chunk / steady-state emission and decode-window settings. For each
configuration it reports model-side TTFA (generation start -> first PCM),
prefill, RTF and the worst sample discontinuity, and writes WAVs so the
fastest settings that still sound clean can be picked by ear.

    python -m bench.sweep_stream --model /models/Qwen3-TTS-12Hz-0.6B-Base \
        --voices /voices/voices.json --voice alex_fec_v1 --out sweep-out
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import statistics
import time
from pathlib import Path

import numpy as np

DEFAULT_GRID = {
    "stream_first_chunk_emit_every": [2, 3, 5],
    "stream_first_chunk_decode_window": [24, 48],
    "stream_emit_every_frames": [6, 12],
    "stream_overlap_samples": [0, 256, 512],
}

PHRASES = [
    ("ack", "Got it."),
    (
        "open",
        "Hey, this is Alex at the Final Expense Center. We had called you earlier about funeral and burial coverage.",
    ),
    ("confirm", "I'm showing your age as seventy-two. Is that correct?"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--voices", required=True)
    parser.add_argument("--voice", default="alex_fec_v1")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--out", default="sweep-out")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--grid", help="JSON object overriding the default grid")
    parser.add_argument("--max-configs", type=int, default=0)
    args = parser.parse_args()

    from hopvoice.backends import QwenBackend
    from hopvoice.config import Settings
    from hopvoice.voices import VoiceRegistry
    from tools.voice_metrics import boundary_discontinuity

    import soundfile as sf

    base = dataclasses.replace(
        Settings.from_env(),
        backend="qwen",
        model_id=args.model,
        device=args.device,
        dtype=args.dtype,
        voices_file=args.voices,
        use_flash_attention=args.device.startswith("cuda"),
        stream_use_optimized_decode=args.device.startswith("cuda"),
        stream_compile=args.device.startswith("cuda"),
        stream_cuda_graphs=args.device.startswith("cuda"),
    )
    backend = QwenBackend(base, VoiceRegistry(args.voices))
    load_started = time.perf_counter()
    backend.load()
    load_s = time.perf_counter() - load_started

    # Cold: first utterance after load (prompt build, kernels, graph capture).
    cold: dict = {}
    t0 = time.perf_counter()
    list(backend.stream_synthesize(text="Okay.", voice_id=args.voice, timings=cold))
    cold_first = (cold["first_pcm"] - t0) * 1000.0

    grid = json.loads(args.grid) if args.grid else DEFAULT_GRID
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    if args.max_configs:
        combos = combos[: args.max_configs]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {
        "model": args.model,
        "device": args.device,
        "load_s": round(load_s, 2),
        "cold_first_pcm_ms": round(cold_first, 1),
        "device_stats": backend.device_stats(),
        "configs": [],
    }
    for combo in combos:
        overrides = dict(zip(keys, combo))
        backend.settings = dataclasses.replace(base, **overrides)
        name = "_".join(f"{k.replace('stream_', '')}{v}" for k, v in overrides.items())
        ttfa, prefill, rtf, jumps = [], [], [], []
        for rep in range(args.repeats):
            for pid, text in PHRASES:
                timings: dict = {}
                start = time.perf_counter()
                chunks = [
                    r.audio
                    for r in backend.stream_synthesize(
                        text=text, voice_id=args.voice, timings=timings
                    )
                ]
                elapsed = time.perf_counter() - start
                audio = np.concatenate(chunks)
                ttfa.append(
                    (timings["first_pcm"] - timings["generation_start"]) * 1000.0
                )
                prefill.append(
                    (timings["prefill_done"] - timings["generation_start"]) * 1000.0
                )
                rtf.append(elapsed / (len(audio) / 24000.0))
                jumps.append(boundary_discontinuity(audio))
                if rep == 0:
                    sf.write(out / f"{name}__{pid}.wav", audio, 24000)
        row = {
            **overrides,
            "ttfa_p50_ms": round(statistics.median(ttfa), 1),
            "ttfa_max_ms": round(max(ttfa), 1),
            "prefill_p50_ms": round(statistics.median(prefill), 1),
            "rtf_p50": round(statistics.median(rtf), 3),
            "max_jump_ratio": round(max(jumps), 2),
        }
        results["configs"].append(row)
        print(json.dumps(row), flush=True)
    results["device_stats_after"] = backend.device_stats()
    (out / "sweep.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
