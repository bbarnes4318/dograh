from api.services.workflow.call_duration import (
    apply_telephony_duration,
    carry_over_telephony_duration,
    parse_duration_seconds,
    telephony_duration_from_callbacks,
)


def test_parse_duration_seconds():
    assert parse_duration_seconds("42") == 42
    assert parse_duration_seconds(42.5) == 42.5
    assert parse_duration_seconds(None) == 0
    assert parse_duration_seconds("") == 0
    assert parse_duration_seconds("abc") == 0
    assert parse_duration_seconds("-3") == 0


def test_apply_fills_missing_or_zero_pipeline_duration():
    assert apply_telephony_duration({}, "37") == {
        "telephony_duration_seconds": 37,
        "call_duration_seconds": 37,
    }
    assert apply_telephony_duration({"call_duration_seconds": 0, "llm": {}}, 37) == {
        "call_duration_seconds": 37,
        "telephony_duration_seconds": 37,
        "llm": {},
    }


def test_apply_keeps_pipeline_duration_when_measured():
    assert apply_telephony_duration({"call_duration_seconds": 35}, 37) == {
        "call_duration_seconds": 35,
        "telephony_duration_seconds": 37,
    }


def test_apply_ignores_zero_carrier_duration():
    assert apply_telephony_duration({"call_duration_seconds": 0}, "0") == {
        "call_duration_seconds": 0
    }


def test_pipeline_write_keeps_earlier_carrier_duration():
    existing = {"telephony_duration_seconds": 51, "call_duration_seconds": 51}
    assert carry_over_telephony_duration(
        existing, {"call_duration_seconds": 0, "llm": {"x": 1}}
    ) == {
        "call_duration_seconds": 51,
        "telephony_duration_seconds": 51,
        "llm": {"x": 1},
    }
    assert carry_over_telephony_duration(existing, {"call_duration_seconds": 49}) == {
        "call_duration_seconds": 49,
        "telephony_duration_seconds": 51,
    }


def test_pipeline_write_without_carrier_duration_is_unchanged():
    assert carry_over_telephony_duration({}, {"call_duration_seconds": 12}) == {
        "call_duration_seconds": 12
    }


def test_duration_from_reported_callback_duration():
    callbacks = [
        {"status": "ringing", "duration": None},
        {"status": "in-progress", "duration": "0"},
        {"status": "completed", "duration": "63"},
    ]
    assert telephony_duration_from_callbacks(callbacks) == 63


def test_duration_from_answer_and_hangup_timestamps():
    callbacks = [
        {"status": "initiated", "timestamp": "2026-09-26T15:00:00+00:00"},
        {"status": "in-progress", "timestamp": "2026-09-26T15:00:10+00:00"},
        {
            "status": "completed",
            "timestamp": "2026-09-26T15:01:25+00:00",
            "duration": None,
        },
    ]
    assert telephony_duration_from_callbacks(callbacks) == 75


def test_unanswered_call_has_no_duration():
    callbacks = [
        {"status": "initiated", "timestamp": "2026-09-26T15:00:00+00:00"},
        {"status": "completed", "timestamp": "2026-09-26T15:00:30+00:00"},
    ]
    assert telephony_duration_from_callbacks(callbacks) == 0
    assert telephony_duration_from_callbacks(None) == 0
