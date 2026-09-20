"""Roll up per-call quality metrics from the realtime feedback event log.

Latency and TTFB are measured on every call and streamed to the live transcript,
but until now they were only ever written into the run's raw ``logs`` blob.
Nothing aggregated them, so "did p95 response time regress this week?" — the
question that predicts conversion better than almost anything else on a voice
call — was unanswerable without re-parsing every run.

These helpers turn the event log into a compact summary that gets persisted on
``WorkflowRunModel.usage_info``, next to token and duration counts, where
reports can group by it.

The node path serves the same purpose for the conversation shape: the ordered
list of nodes a call actually reached, stored on ``gathered_context`` so a
funnel can be aggregated in SQL instead of by scanning JSON logs.
"""

import math
from typing import Any, Iterable, Optional

from pipecat.utils.enums import RealtimeFeedbackType


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list.

    Nearest-rank (rather than interpolated) keeps every reported number a real
    measurement that appears in the call, which matters when a summary is only
    a handful of turns long.
    """
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    rank = math.ceil(fraction * len(sorted_values))
    rank = max(1, min(len(sorted_values), rank))
    return sorted_values[rank - 1]


def summarize_samples(values: Iterable[float]) -> Optional[dict[str, Any]]:
    """Summarize a series of seconds-valued samples, or None if there are none."""
    samples = sorted(float(v) for v in values if v is not None)
    if not samples:
        return None
    return {
        "samples": len(samples),
        "avg_seconds": round(sum(samples) / len(samples), 3),
        "p50_seconds": round(_percentile(samples, 0.50), 3),
        "p90_seconds": round(_percentile(samples, 0.90), 3),
        "p95_seconds": round(_percentile(samples, 0.95), 3),
        "max_seconds": round(samples[-1], 3),
    }


def extract_latency_samples(events: Iterable[dict[str, Any]]) -> list[float]:
    """User-speech-end to bot-speech-start latencies, in seconds."""
    return [
        event["payload"]["latency_seconds"]
        for event in events
        if event.get("type") == RealtimeFeedbackType.LATENCY_MEASURED.value
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("latency_seconds") is not None
    ]


def extract_ttfb_samples(events: Iterable[dict[str, Any]]) -> list[float]:
    """Time-to-first-byte samples reported by pipeline services, in seconds."""
    return [
        event["payload"]["ttfb_seconds"]
        for event in events
        if event.get("type") == RealtimeFeedbackType.TTFB_METRIC.value
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("ttfb_seconds") is not None
    ]


def count_turns(events: Iterable[dict[str, Any]]) -> int:
    """Number of distinct conversation turns the call produced."""
    speech_types = {
        RealtimeFeedbackType.USER_TRANSCRIPTION.value,
        RealtimeFeedbackType.BOT_TEXT.value,
    }
    return len(
        {
            event.get("turn", 0)
            for event in events
            if event.get("type") in speech_types
        }
    )


def compute_response_metrics(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the ``usage_info`` response-quality block for a call.

    Keys are omitted rather than set to None when a call produced no samples,
    so downstream aggregation can distinguish "not measured" from "measured as
    zero".
    """
    events = list(events)
    metrics: dict[str, Any] = {"num_turns": count_turns(events)}

    latency = summarize_samples(extract_latency_samples(events))
    if latency:
        metrics["latency"] = latency

    ttfb = summarize_samples(extract_ttfb_samples(events))
    if ttfb:
        metrics["ttfb"] = ttfb

    return metrics


def compute_node_path(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ordered, de-duplicated list of nodes the call actually reached.

    Consecutive repeats collapse (a node re-entered after a loop still counts
    once in a row), but a genuine return to an earlier node later in the call is
    kept, so the path reflects what happened rather than a set.
    """
    path: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != RealtimeFeedbackType.NODE_TRANSITION.value:
            continue
        payload = event.get("payload") or {}
        node_id = payload.get("node_id")
        if not node_id:
            continue
        if path and path[-1]["node_id"] == node_id:
            continue
        path.append({"node_id": node_id, "node_name": payload.get("node_name")})
    return path
