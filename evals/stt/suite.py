#!/usr/bin/env python3
"""Labeled STT comparison suite for phone voice agents.

Runs every clip in a manifest through each provider and reports, per clip and
per provider: WER (when the clip has a reference), end-of-turn events vs the
expected number (false / missed endpoints), time to first partial, time from
end of audio to the last final, errors, and estimated cost per minute.

    python -m evals.stt.suite suites/phone_agent.json --providers assemblyai deepgram-flux
    python -m evals.stt.suite suites/phone_agent.json --providers assemblyai --mode min_latency
    python -m evals.stt.suite suites/phone_agent.json --only short_answers --save

Clips whose audio file is missing are reported as SKIPPED with the reason, never
counted as passes. ``generate:silence:<seconds>`` creates silence locally.

Use synthetic or properly de-identified audio only. Real caller recordings may
be used only where their use for testing is authorized; results store
transcripts, so keep ``results/`` out of shared places for such runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from evals.stt.metrics import cost_per_minute, endpoint_errors, word_error_rate

# Approximate list prices per streaming hour, for the cost column only.
PRICE_PER_HOUR = {
    "assemblyai": 0.45,
    "deepgram": 0.46,
    "deepgram-flux": 0.46,
    "speechmatics": 0.72,
    "hopstt": 0.0,
}


def _provider(name: str):
    from evals.stt.benchmark import get_provider

    return get_provider(name)


def _resolve_audio(entry: dict, base: Path, tmp: Path) -> tuple[Path | None, str | None]:
    audio = entry.get("audio") or ""
    if audio.startswith("generate:silence:"):
        seconds = float(audio.rsplit(":", 1)[1])
        out = tmp / f"silence_{seconds:g}.wav"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
             "anullsrc=r=8000:cl=mono", "-t", str(seconds), str(out)],
            check=True,
        )
        return out, None
    path = Path(audio)
    if not path.is_absolute():
        path = base / path
    if not path.exists():
        return None, f"audio missing: {audio}"
    return path, None


async def run_suite(args) -> dict:
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = Path(__file__).parent / manifest_path
    manifest = json.loads(manifest_path.read_text())
    base = Path(__file__).parent
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for clip in manifest["clips"]:
            if args.only and clip.get("category") not in args.only:
                continue
            audio_path, skip = _resolve_audio(clip, base, tmp)
            for name in args.providers:
                row = {"clip": clip["id"], "category": clip.get("category"), "provider": name}
                if skip:
                    rows.append({**row, "status": "SKIPPED", "reason": skip})
                    continue
                try:
                    provider = _provider(name)
                    kwargs = dict(
                        keyterms=clip.get("keyterms"),
                        language=clip.get("language", "en"),
                        sample_rate=args.sample_rate,
                    )
                    if name == "assemblyai":
                        kwargs.update(mode=args.mode, agent_context=clip.get("agent_context"))
                        if args.no_voice_focus:
                            kwargs["voice_focus"] = None
                    result = await provider.transcribe(audio_path, **kwargs)
                except Exception as exc:
                    rows.append({**row, "status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"})
                    continue
                params = result.params or {}
                turns = params.get("end_of_turn_count")
                if turns is None:
                    turns = len((result.raw_response or {}).get("finals") or []) or None
                reference = clip.get("reference")
                rows.append(
                    {
                        **row,
                        "status": "OK" if not params.get("errors") else "PROVIDER_ERROR",
                        "transcript": result.transcript,
                        "reference": reference,
                        "wer": word_error_rate(reference, result.transcript)
                        if reference is not None
                        else None,
                        "expected_turns": clip.get("expected_turns"),
                        "observed_turns": turns,
                        **endpoint_errors(clip.get("expected_turns"), turns),
                        "time_to_first_partial_ms": params.get("time_to_first_partial_ms"),
                        "final_after_audio_end_ms": params.get("last_final_after_audio_end_ms")
                        or params.get("time_to_final_after_finalize_ms"),
                        "model_served": params.get("model_served"),
                        "errors": params.get("errors"),
                        "cost_per_minute_usd": cost_per_minute(PRICE_PER_HOUR.get(name, 0.0)),
                    }
                )
    return {"manifest": str(manifest_path), "mode": args.mode, "rows": rows}


def summarize(report: dict) -> None:
    print(f"\n{'clip':<28} {'provider':<14} {'status':<14} {'WER':>6} {'turns':>7} "
          f"{'1st partial':>11} {'final+':>8}")
    for r in report["rows"]:
        wer = "-" if r.get("wer") is None else f"{r['wer']:.2f}"
        turns = f"{r.get('observed_turns') or 0}/{r.get('expected_turns') or '-'}"
        fp = r.get("time_to_first_partial_ms")
        fa = r.get("final_after_audio_end_ms")
        print(f"{r['clip']:<28} {r['provider']:<14} {r['status']:<14} {wer:>6} {turns:>7} "
              f"{(f'{fp:.0f}' if fp else '-'):>11} {(f'{fa:.0f}' if fa else '-'):>8}")
        if r["status"] in ("SKIPPED", "ERROR"):
            print(f"    {r.get('reason')}")
    by_provider: dict[str, list[float]] = {}
    for r in report["rows"]:
        if r.get("wer") is not None:
            by_provider.setdefault(r["provider"], []).append(r["wer"])
    if by_provider:
        print("\nMean WER on labeled clips (lower is better; small samples prove little):")
        for name, values in by_provider.items():
            print(f"  {name:<14} {sum(values) / len(values):.3f} over {len(values)} clip(s)")
    skipped = sum(1 for r in report["rows"] if r["status"] == "SKIPPED")
    if skipped:
        print(f"\n{skipped} run(s) SKIPPED: supply the missing audio before drawing conclusions.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest")
    parser.add_argument("--providers", nargs="+", default=["assemblyai"])
    parser.add_argument("--only", nargs="+", help="categories to run")
    parser.add_argument("--mode", default="balanced",
                        choices=["min_latency", "balanced", "max_accuracy"])
    parser.add_argument("--no-voice-focus", action="store_true")
    parser.add_argument("--sample-rate", type=int, default=8000)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run_suite(args))
    summarize(report)
    if args.save:
        out = Path(__file__).parent / "results" / (
            f"suite-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{args.mode}.json"
        )
        out.write_text(json.dumps(report, indent=2))
        print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
