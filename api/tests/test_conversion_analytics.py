"""Tests for the node funnel and conversion cohorts."""

from datetime import UTC, datetime

import pytest
from pipecat.utils.enums import RealtimeFeedbackType

from api.services.reports.conversion_analytics import (
    COHORT_DIMENSIONS,
    build_conversion_cohorts,
    build_node_funnel,
    cohort_key,
    node_path_for_run,
)


def _run(
    run_id: int,
    nodes: list[tuple[str, str]] | None = None,
    disposition: str = "NOANSWER",
    *,
    definition_id: int | None = 1,
    created_at: datetime | None = None,
    runtime: dict | None = None,
    p95_latency: float | None = None,
    call_type: str = "outbound",
    logs: dict | None = None,
):
    gathered: dict = {"mapped_call_disposition": disposition}
    if nodes is not None:
        gathered["node_path"] = [
            {"node_id": node_id, "node_name": name} for node_id, name in nodes
        ]
    usage: dict = {}
    if p95_latency is not None:
        usage["latency"] = {"p95_seconds": p95_latency}
    return {
        "id": run_id,
        "workflow_id": 7,
        "definition_id": definition_id,
        "created_at": created_at or datetime(2026, 6, 15, 14, tzinfo=UTC),
        "call_type": call_type,
        "gathered_context": gathered,
        "initial_context": {"runtime_configuration": runtime or {}},
        "usage_info": usage,
        "logs": logs or {},
    }


GREET = ("1", "Greet")
QUALIFY = ("2", "Qualify")
PITCH = ("3", "Pitch")
TRANSFER = ("4", "Transfer")


class TestNodePathForRun:
    def test_prefers_the_stored_path(self):
        run = _run(1, [GREET, QUALIFY])
        assert [e["node_id"] for e in node_path_for_run(run)] == ["1", "2"]

    def test_falls_back_to_the_event_log(self):
        run = _run(
            1,
            None,
            logs={
                "realtime_feedback_events": [
                    {
                        "type": RealtimeFeedbackType.NODE_TRANSITION.value,
                        "payload": {"node_id": "9", "node_name": "Legacy"},
                    }
                ]
            },
        )
        assert [e["node_id"] for e in node_path_for_run(run)] == ["9"]

    def test_empty_when_there_is_nothing_to_read(self):
        assert node_path_for_run(_run(1, None)) == []


class TestNodeFunnel:
    def test_counts_reach_and_drop_off_in_conversation_order(self):
        runs = [
            _run(1, [GREET, QUALIFY, PITCH, TRANSFER], "XFER"),
            _run(2, [GREET, QUALIFY, PITCH]),
            _run(3, [GREET, QUALIFY]),
            _run(4, [GREET]),
        ]

        funnel = build_node_funnel(runs)

        assert [step["node_name"] for step in funnel["steps"]] == [
            "Greet",
            "Qualify",
            "Pitch",
            "Transfer",
        ]
        assert [step["reached"] for step in funnel["steps"]] == [4, 3, 2, 1]
        assert funnel["steps"][1]["dropped_from_previous"] == 1
        assert funnel["steps"][1]["drop_off_pct_from_previous"] == 25.0
        assert funnel["steps"][0]["reached_pct_of_runs"] == 100.0

    def test_reports_conversion_of_the_runs_that_reached_each_node(self):
        runs = [
            _run(1, [GREET, QUALIFY], "XFER"),
            _run(2, [GREET, QUALIFY]),
            _run(3, [GREET]),
        ]

        funnel = build_node_funnel(runs)

        greet, qualify = funnel["steps"]
        assert greet["converted"] == 1
        assert greet["conversion_pct_of_reached"] == pytest.approx(33.33, abs=0.01)
        assert qualify["conversion_pct_of_reached"] == 50.0

    def test_a_node_revisited_in_one_call_is_counted_once(self):
        funnel = build_node_funnel([_run(1, [GREET, QUALIFY, GREET])])
        greet = next(s for s in funnel["steps"] if s["node_id"] == "1")
        assert greet["reached"] == 1

    def test_runs_without_a_path_are_excluded_but_still_counted(self):
        funnel = build_node_funnel([_run(1, [GREET]), _run(2, None)])
        assert funnel["total_runs"] == 2
        assert funnel["runs_with_node_path"] == 1
        assert funnel["steps"][0]["reached_pct_of_runs"] == 100.0

    def test_honours_custom_conversion_dispositions(self):
        runs = [_run(1, [GREET], "SALE"), _run(2, [GREET], "XFER")]

        funnel = build_node_funnel(runs, conversion_dispositions=["SALE"])

        assert funnel["steps"][0]["converted"] == 1
        assert funnel["conversion_dispositions"] == ["SALE"]

    def test_empty_input(self):
        funnel = build_node_funnel([])
        assert funnel["total_runs"] == 0
        assert funnel["steps"] == []

    def test_skips_path_entries_without_an_id(self):
        run = _run(1, [GREET])
        run["gathered_context"]["node_path"].append({"node_name": "Broken"})
        funnel = build_node_funnel([run])
        assert [step["node_id"] for step in funnel["steps"]] == ["1"]


class TestCohortKey:
    def test_definition_version(self):
        assert cohort_key(_run(1, definition_id=42), "definition_version") == (
            "definition 42"
        )

    def test_unknown_definition(self):
        assert cohort_key(_run(1, definition_id=None), "definition_version") == (
            "unknown"
        )

    def test_model_includes_the_provider(self):
        run = _run(1, runtime={"llm_provider": "openai", "llm_model": "gpt-4.1"})
        assert cohort_key(run, "llm_model") == "openai/gpt-4.1"

    def test_realtime_run_reports_its_realtime_model(self):
        run = _run(
            1,
            runtime={
                "realtime_provider": "openai_realtime",
                "realtime_model": "gpt-realtime",
                "llm_provider": "openai",
                "llm_model": "gpt-4.1",
            },
        )
        assert cohort_key(run, "llm_model") == "openai_realtime/gpt-realtime"

    def test_daypart(self):
        run = _run(1, created_at=datetime(2026, 6, 15, 10, tzinfo=UTC))
        assert cohort_key(run, "daypart") == "morning (09-12)"

    def test_weekday(self):
        run = _run(1, created_at=datetime(2026, 6, 15, 10, tzinfo=UTC))
        assert cohort_key(run, "weekday") == "Monday"

    def test_call_type_and_disposition(self):
        run = _run(1, disposition="XFER", call_type="inbound")
        assert cohort_key(run, "call_type") == "inbound"
        assert cohort_key(run, "disposition") == "XFER"

    def test_rejects_an_unknown_dimension(self):
        with pytest.raises(ValueError):
            cohort_key(_run(1), "phase_of_the_moon")

    def test_every_advertised_dimension_works(self):
        run = _run(1, runtime={"llm_model": "m", "tts_model": "t", "stt_model": "s"})
        for dimension in COHORT_DIMENSIONS:
            assert isinstance(cohort_key(run, dimension), str)


class TestConversionCohorts:
    def test_splits_and_ranks_by_conversion(self):
        runs = [
            _run(1, definition_id=1, disposition="XFER"),
            _run(2, definition_id=1, disposition="XFER"),
            _run(3, definition_id=2, disposition="NOANSWER"),
            _run(4, definition_id=2, disposition="XFER"),
        ]

        result = build_conversion_cohorts(runs, "definition_version")

        assert [c["cohort"] for c in result["cohorts"]] == [
            "definition 1",
            "definition 2",
        ]
        assert result["cohorts"][0]["conversion_pct"] == 100.0
        assert result["cohorts"][1]["conversion_pct"] == 50.0

    def test_averages_latency_where_it_was_measured(self):
        runs = [
            _run(1, definition_id=1, p95_latency=1.0),
            _run(2, definition_id=1, p95_latency=2.0),
            _run(3, definition_id=1),
        ]

        result = build_conversion_cohorts(runs, "definition_version")

        cohort = result["cohorts"][0]
        assert cohort["runs"] == 3
        assert cohort["runs_with_latency"] == 2
        assert cohort["avg_p95_latency_seconds"] == 1.5

    def test_latency_is_none_when_never_measured(self):
        result = build_conversion_cohorts([_run(1)], "definition_version")
        assert result["cohorts"][0]["avg_p95_latency_seconds"] is None

    def test_rejects_an_unknown_dimension(self):
        with pytest.raises(ValueError):
            build_conversion_cohorts([_run(1)], "phase_of_the_moon")

    def test_empty_input(self):
        result = build_conversion_cohorts([], "definition_version")
        assert result["cohorts"] == []
