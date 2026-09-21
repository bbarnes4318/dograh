"""Tests for per-call response metrics and node-path extraction."""

import pytest
from pipecat.utils.enums import RealtimeFeedbackType

from api.services.pipecat.call_metrics import (
    compute_node_path,
    compute_response_metrics,
    summarize_samples,
)


def _latency(seconds: float) -> dict:
    return {
        "type": RealtimeFeedbackType.LATENCY_MEASURED.value,
        "payload": {"latency_seconds": seconds},
    }


def _ttfb(seconds: float) -> dict:
    return {
        "type": RealtimeFeedbackType.TTFB_METRIC.value,
        "payload": {"ttfb_seconds": seconds, "processor": "tts", "model": "m"},
    }


def _transition(node_id: str, node_name: str) -> dict:
    return {
        "type": RealtimeFeedbackType.NODE_TRANSITION.value,
        "payload": {"node_id": node_id, "node_name": node_name},
    }


def _user_turn(turn: int) -> dict:
    return {
        "type": RealtimeFeedbackType.USER_TRANSCRIPTION.value,
        "payload": {"text": "hi", "final": True},
        "turn": turn,
    }


class TestSummarizeSamples:
    def test_returns_none_without_samples(self):
        assert summarize_samples([]) is None
        assert summarize_samples([None, None]) is None

    def test_single_sample_is_every_percentile(self):
        summary = summarize_samples([1.5])
        assert summary == {
            "samples": 1,
            "avg_seconds": 1.5,
            "p50_seconds": 1.5,
            "p90_seconds": 1.5,
            "p95_seconds": 1.5,
            "max_seconds": 1.5,
        }

    def test_percentiles_are_real_measurements(self):
        summary = summarize_samples([float(v) for v in range(1, 11)])
        assert summary["samples"] == 10
        assert summary["avg_seconds"] == 5.5
        assert summary["p50_seconds"] == 5.0
        assert summary["p90_seconds"] == 9.0
        assert summary["p95_seconds"] == 10.0
        assert summary["max_seconds"] == 10.0

    @pytest.mark.parametrize(
        "fraction_key", ["p50_seconds", "p90_seconds", "p95_seconds"]
    )
    def test_percentiles_never_exceed_max(self, fraction_key):
        summary = summarize_samples([0.4, 0.9, 1.3, 2.7])
        assert summary[fraction_key] <= summary["max_seconds"]


class TestComputeResponseMetrics:
    def test_empty_log_reports_no_turns_and_no_blocks(self):
        assert compute_response_metrics([]) == {"num_turns": 0}

    def test_omits_blocks_that_were_never_measured(self):
        metrics = compute_response_metrics([_latency(0.8)])
        assert "latency" in metrics
        assert "ttfb" not in metrics

    def test_summarizes_latency_and_ttfb(self):
        metrics = compute_response_metrics(
            [_latency(0.5), _latency(1.5), _ttfb(0.2), _user_turn(1), _user_turn(2)]
        )
        assert metrics["num_turns"] == 2
        assert metrics["latency"]["samples"] == 2
        assert metrics["latency"]["avg_seconds"] == 1.0
        assert metrics["latency"]["max_seconds"] == 1.5
        assert metrics["ttfb"]["samples"] == 1

    def test_ignores_malformed_events(self):
        metrics = compute_response_metrics(
            [
                {"type": RealtimeFeedbackType.LATENCY_MEASURED.value},
                {"type": RealtimeFeedbackType.LATENCY_MEASURED.value, "payload": {}},
                {"type": RealtimeFeedbackType.LATENCY_MEASURED.value, "payload": None},
                _latency(0.3),
            ]
        )
        assert metrics["latency"]["samples"] == 1


class TestComputeNodePath:
    def test_empty_without_transitions(self):
        assert compute_node_path([_latency(1.0)]) == []

    def test_records_nodes_in_order(self):
        path = compute_node_path(
            [_transition("1", "Greet"), _latency(0.5), _transition("2", "Qualify")]
        )
        assert path == [
            {"node_id": "1", "node_name": "Greet"},
            {"node_id": "2", "node_name": "Qualify"},
        ]

    def test_collapses_consecutive_repeats_but_keeps_returns(self):
        path = compute_node_path(
            [
                _transition("1", "Greet"),
                _transition("1", "Greet"),
                _transition("2", "Qualify"),
                _transition("1", "Greet"),
            ]
        )
        assert [entry["node_id"] for entry in path] == ["1", "2", "1"]

    def test_skips_transitions_without_a_node_id(self):
        path = compute_node_path(
            [
                {
                    "type": RealtimeFeedbackType.NODE_TRANSITION.value,
                    "payload": {"node_id": None, "node_name": "?"},
                },
                _transition("2", "Qualify"),
            ]
        )
        assert [entry["node_id"] for entry in path] == ["2"]
