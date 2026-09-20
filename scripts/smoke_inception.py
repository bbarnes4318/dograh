"""Measure Inception Mercury time-to-first-token from this machine.

Sends ten sequential streaming chat completions to Inception's
OpenAI-compatible endpoint and reports the time from request send to the first
streamed *content* token — the same thing the pipeline's ``[llm_ttft]`` log
line measures, but without a workflow or a phone call in the way.

Run from the repo root:

    INCEPTION_API_KEY=sk_... python -m scripts.smoke_inception

Exits non-zero if p95 TTFT exceeds the budget (default 800 ms), so it can gate
a rollout. Use --runs / --budget-ms / --model / --reasoning-effort / --base-url
to point it somewhere else.
"""

import argparse
import json
import math
import os
import sys
import time

import httpx

DEFAULT_BASE_URL = "https://api.inceptionlabs.ai/v1"
DEFAULT_MODEL = "mercury-2.5"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_RUNS = 10
DEFAULT_BUDGET_MS = 800.0
REQUEST_TIMEOUT_S = 30.0

# ~40 tokens: close to the standing instructions a node carries into a turn.
SYSTEM_PROMPT = (
    "You are a phone agent for a home services company. Keep every reply to "
    "one short spoken sentence. If the caller asks about pricing, give a range "
    "and offer to book a visit."
)

USER_TURN = "Hi, my kitchen sink has been backing up since yesterday."


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list.

    Ten samples is too few for interpolation to mean much, so this takes the
    lowest-ranked sample at or above the requested percentile (p95 of ten
    samples is the slowest one).
    """
    if not sorted_values:
        raise ValueError("no samples")
    rank = math.ceil(fraction * len(sorted_values))
    rank = min(max(rank, 1), len(sorted_values))
    return sorted_values[rank - 1]


def measure_one(
    client: httpx.Client,
    *,
    base_url: str,
    api_key: str,
    model: str,
    reasoning_effort: str,
) -> float:
    """Return ms from request send to the first streamed content token."""
    payload = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TURN},
        ],
    }

    started = time.perf_counter()
    with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=REQUEST_TIMEOUT_S,
    ) as response:
        if response.status_code != 200:
            body = response.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {response.status_code} from Inception: {body}")

        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            # Role-only and empty-string deltas are not usable output; wait for
            # the first chunk that actually carries text.
            if delta.get("content"):
                return (time.perf_counter() - started) * 1000.0

    raise RuntimeError("stream ended without any content token")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--budget-ms", type=float, default=DEFAULT_BUDGET_MS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument(
        "--base-url", default=os.environ.get("INCEPTION_BASE_URL", DEFAULT_BASE_URL)
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    api_key = os.environ.get("INCEPTION_API_KEY", "").strip()
    if not api_key:
        print(
            "INCEPTION_API_KEY is not set. Export the Inception API key for the "
            "org whose latency you want to measure, then re-run:\n"
            "    INCEPTION_API_KEY=sk_... python -m scripts.smoke_inception",
            file=sys.stderr,
        )
        return 2

    print(
        f"{args.runs} sequential streaming completions -> {args.base_url} "
        f"(model={args.model}, reasoning_effort={args.reasoning_effort})"
    )

    samples: list[float] = []
    failures = 0
    with httpx.Client() as client:
        for run in range(1, args.runs + 1):
            try:
                ttft_ms = measure_one(
                    client,
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                )
            except Exception as exc:  # noqa: BLE001 — report and keep going
                failures += 1
                print(f"run {run:2d}: FAILED — {type(exc).__name__}: {exc}")
                continue
            samples.append(ttft_ms)
            print(f"run {run:2d}: ttft {ttft_ms:7.1f} ms")

    if not samples:
        print(f"\nAll {args.runs} runs failed; no TTFT to report.", file=sys.stderr)
        return 1

    ordered = sorted(samples)
    p50 = percentile(ordered, 0.50)
    p95 = percentile(ordered, 0.95)
    print(
        f"\n{len(samples)}/{args.runs} runs succeeded — "
        f"p50 {p50:.1f} ms, p95 {p95:.1f} ms "
        f"(min {ordered[0]:.1f}, max {ordered[-1]:.1f})"
    )

    if failures:
        print(f"{failures} run(s) failed.", file=sys.stderr)
        return 1
    if p95 > args.budget_ms:
        print(
            f"FAIL: p95 {p95:.1f} ms exceeds the {args.budget_ms:.0f} ms budget.",
            file=sys.stderr,
        )
        return 1

    print(f"PASS: p95 {p95:.1f} ms is within the {args.budget_ms:.0f} ms budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
