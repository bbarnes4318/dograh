"""Call metrics computation from raw event logs."""

from api.services.pipecat.call_metrics import (
    count_turns,
    extract_latency_samples,
    extract_ttfb_samples,
    summarize_samples,
)


def compute_call_metrics(
    logs: list[dict], call_duration_seconds: float | None = None
) -> dict:
    """Pre-compute quantitative metrics from raw call logs.

    Shares its extraction with ``api.services.pipecat.call_metrics`` so the
    numbers a QA judge is shown are the same ones persisted on the run. The
    flat ``avg_*``/``max_*`` keys are kept because user-authored QA prompts
    interpolate them by name; the percentile keys are additive.
    """
    latencies = extract_latency_samples(logs)
    ttfb_values = extract_ttfb_samples(logs)

    latency_summary = summarize_samples(latencies)
    ttfb_summary = summarize_samples(ttfb_values)

    return {
        "call_duration_seconds": call_duration_seconds,
        "num_turns": count_turns(logs),
        "avg_latency_seconds": (
            latency_summary["avg_seconds"] if latency_summary else None
        ),
        "avg_ttfb_seconds": (ttfb_summary["avg_seconds"] if ttfb_summary else None),
        "max_latency_seconds": (
            latency_summary["max_seconds"] if latency_summary else None
        ),
        "p95_latency_seconds": (
            latency_summary["p95_seconds"] if latency_summary else None
        ),
        "p95_ttfb_seconds": (ttfb_summary["p95_seconds"] if ttfb_summary else None),
    }
