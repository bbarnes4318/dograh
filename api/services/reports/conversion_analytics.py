"""Where calls stop, and which cohorts convert.

Two questions the platform collected the data for but never answered.

**The funnel.** Every call records which nodes it reached. Nothing aggregated
that, so "73% reach Qualify, 31% reach Pitch, 9% reach Transfer" — the single
most actionable artifact for a scripted voice agent, because it says *where*
the script loses people — was unavailable.

**Cohorts.** Reporting was total runs, transfer count and top dispositions,
with no way to split by workflow version, model, daypart or list source. So
even when conversion moved, nothing said what moved it.

Both read the summaries written onto the run when the call ends
(``gathered_context["node_path"]``, ``usage_info``), falling back to the raw
event log for runs that predate them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from api.services.pipecat.call_metrics import compute_node_path

# Dispositions that count as a conversion when the caller doesn't say otherwise.
# XFER is what the daily report has always treated as the success outcome.
DEFAULT_CONVERSION_DISPOSITIONS = ("XFER",)

# Cohort dimensions callers can group by.
COHORT_DIMENSIONS = (
    "definition_version",
    "llm_model",
    "tts_model",
    "stt_model",
    "daypart",
    "weekday",
    "call_type",
    "disposition",
)

_DAYPARTS = (
    (0, 6, "night (00-06)"),
    (6, 9, "early morning (06-09)"),
    (9, 12, "morning (09-12)"),
    (12, 15, "early afternoon (12-15)"),
    (15, 18, "late afternoon (15-18)"),
    (18, 21, "evening (18-21)"),
    (21, 24, "late evening (21-24)"),
)


def _as_mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def node_path_for_run(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ordered nodes a run reached.

    Reads the summary written onto ``gathered_context`` when the call ends.

    Runs that finished before that summary existed have no node path and are
    reported as such: the funnel query deliberately does not select ``logs``
    (the per-call event blob is the largest column on the table), so there is
    nothing here to recompute a path from. ``build_node_funnel`` counts those
    runs separately rather than silently dropping them from the denominator —
    a funnel that quietly ignored every pre-deploy run would read as a cliff
    that isn't there.

    A caller that *does* have ``logs`` in hand (a single-run drill-down, say)
    can pass it and get the path recomputed.
    """
    gathered = _as_mapping(run.get("gathered_context"))
    path = gathered.get("node_path")
    if isinstance(path, list) and path:
        return [entry for entry in path if isinstance(entry, Mapping)]

    logs = _as_mapping(run.get("logs"))
    events = logs.get("realtime_feedback_events")
    if isinstance(events, list) and events:
        return compute_node_path(events)
    return []


def disposition_for_run(run: Mapping[str, Any]) -> str:
    gathered = _as_mapping(run.get("gathered_context"))
    return str(gathered.get("mapped_call_disposition") or "UNKNOWN")


def _converted(run: Mapping[str, Any], conversion_dispositions: Sequence[str]) -> bool:
    return disposition_for_run(run) in conversion_dispositions


def _percent(part: int, whole: int) -> float:
    return round(part / whole * 100, 2) if whole else 0.0


def build_node_funnel(
    runs: Iterable[Mapping[str, Any]],
    conversion_dispositions: Sequence[str] = DEFAULT_CONVERSION_DISPOSITIONS,
) -> dict[str, Any]:
    """Reach, drop-off and conversion per node.

    Nodes are ordered by where they typically appear in a call rather than by
    the graph, so a funnel spanning several workflow versions still reads in
    conversation order.
    """
    runs = list(runs)
    total_runs = len(runs)

    reached: dict[str, set[Any]] = defaultdict(set)
    converted: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    position_total: dict[str, int] = defaultdict(int)
    position_count: dict[str, int] = defaultdict(int)
    runs_with_path = 0

    for run in runs:
        path = node_path_for_run(run)
        if not path:
            continue
        runs_with_path += 1
        run_converted = _converted(run, conversion_dispositions)
        seen: set[str] = set()
        for position, entry in enumerate(path):
            node_id = str(entry.get("node_id") or "")
            if not node_id:
                continue
            names.setdefault(node_id, str(entry.get("node_name") or node_id))
            position_total[node_id] += position
            position_count[node_id] += 1
            if node_id in seen:
                continue
            seen.add(node_id)
            reached[node_id].add(run.get("id"))
            if run_converted:
                converted[node_id] += 1

    ordered = sorted(
        reached.keys(),
        key=lambda node_id: (
            position_total[node_id] / position_count[node_id],
            -len(reached[node_id]),
        ),
    )

    steps: list[dict[str, Any]] = []
    previous_count: Optional[int] = None
    for node_id in ordered:
        count = len(reached[node_id])
        steps.append(
            {
                "node_id": node_id,
                "node_name": names.get(node_id, node_id),
                "reached": count,
                "reached_pct_of_runs": _percent(count, runs_with_path),
                "dropped_from_previous": (
                    max(0, previous_count - count) if previous_count is not None else 0
                ),
                "drop_off_pct_from_previous": (
                    _percent(max(0, previous_count - count), previous_count)
                    if previous_count
                    else 0.0
                ),
                "converted": converted[node_id],
                "conversion_pct_of_reached": _percent(converted[node_id], count),
            }
        )
        previous_count = count

    return {
        "total_runs": total_runs,
        "runs_with_node_path": runs_with_path,
        "conversion_dispositions": list(conversion_dispositions),
        "steps": steps,
    }


def _daypart(moment: datetime | None) -> str:
    if not isinstance(moment, datetime):
        return "unknown"
    hour = moment.hour
    for start, end, label in _DAYPARTS:
        if start <= hour < end:
            return label
    return "unknown"


def _runtime_value(run: Mapping[str, Any], key: str) -> str:
    initial = _as_mapping(run.get("initial_context"))
    runtime = _as_mapping(initial.get("runtime_configuration"))
    provider = runtime.get(key.replace("_model", "_provider"))
    model = runtime.get(key)
    if provider and model:
        return f"{provider}/{model}"
    return str(model or provider or "unknown")


def cohort_key(run: Mapping[str, Any], dimension: str) -> str:
    """The cohort a run belongs to along one dimension."""
    if dimension == "definition_version":
        value = run.get("definition_id")
        return f"definition {value}" if value is not None else "unknown"
    if dimension in ("llm_model", "tts_model", "stt_model"):
        # A realtime run has no separate TTS/STT; it reports its realtime model.
        initial = _as_mapping(run.get("initial_context"))
        runtime = _as_mapping(initial.get("runtime_configuration"))
        if dimension == "llm_model" and runtime.get("realtime_model"):
            return _runtime_value(run, "realtime_model")
        return _runtime_value(run, dimension)
    if dimension == "daypart":
        return _daypart(run.get("created_at"))
    if dimension == "weekday":
        created = run.get("created_at")
        return created.strftime("%A") if isinstance(created, datetime) else "unknown"
    if dimension == "call_type":
        return str(run.get("call_type") or "unknown")
    if dimension == "disposition":
        return disposition_for_run(run)
    raise ValueError(f"Unknown cohort dimension: {dimension}")


def _latency_p95(run: Mapping[str, Any]) -> Optional[float]:
    usage = _as_mapping(run.get("usage_info"))
    latency = _as_mapping(usage.get("latency"))
    value = latency.get("p95_seconds")
    return float(value) if isinstance(value, (int, float)) else None


def build_conversion_cohorts(
    runs: Iterable[Mapping[str, Any]],
    dimension: str,
    conversion_dispositions: Sequence[str] = DEFAULT_CONVERSION_DISPOSITIONS,
) -> dict[str, Any]:
    """Conversion rate and response latency per cohort, best first.

    Latency rides along because it is the variable most likely to explain a
    difference between two otherwise identical cohorts.
    """
    if dimension not in COHORT_DIMENSIONS:
        raise ValueError(f"Unknown cohort dimension: {dimension}")

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "converted": 0, "latencies": []}
    )
    for run in runs:
        bucket = buckets[cohort_key(run, dimension)]
        bucket["runs"] += 1
        if _converted(run, conversion_dispositions):
            bucket["converted"] += 1
        latency = _latency_p95(run)
        if latency is not None:
            bucket["latencies"].append(latency)

    cohorts = []
    for name, bucket in buckets.items():
        latencies = bucket["latencies"]
        cohorts.append(
            {
                "cohort": name,
                "runs": bucket["runs"],
                "converted": bucket["converted"],
                "conversion_pct": _percent(bucket["converted"], bucket["runs"]),
                "avg_p95_latency_seconds": (
                    round(sum(latencies) / len(latencies), 3) if latencies else None
                ),
                "runs_with_latency": len(latencies),
            }
        )

    cohorts.sort(key=lambda c: (-c["conversion_pct"], -c["runs"], c["cohort"]))
    return {
        "dimension": dimension,
        "conversion_dispositions": list(conversion_dispositions),
        "cohorts": cohorts,
    }
