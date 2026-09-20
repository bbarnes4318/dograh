"""Tests for DTMF capture and sending."""

from unittest.mock import AsyncMock

import pytest
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    Frame,
    InputDTMFFrame,
    LLMMessagesAppendFrame,
    OutputDTMFFrame,
    TranscriptionFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.utils.time import time_now_iso8601

from api.schemas.workflow_configurations import DTMFConfiguration
from api.services.pipecat.dtmf import (
    DTMFCaptureProcessor,
    parse_dtmf_digits,
    send_dtmf_digits,
)


def _press(digit: str) -> InputDTMFFrame:
    return InputDTMFFrame(button=KeypadEntry(digit))


def _appended(frames) -> list[str]:
    return [
        f.messages[0]["content"]
        for f in frames
        if isinstance(f, LLMMessagesAppendFrame)
    ]


def _in_order(*frames: Frame, gap: float = 0.05) -> list[Frame]:
    """Space frames so they're processed in the order given."""
    spaced: list[Frame] = []
    for frame in frames:
        spaced.extend([frame, SleepFrame(gap)])
    return spaced


class TestParseDtmfDigits:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("123", "123"),
            ("1234#", "1234#"),
            ("*9", "*9"),
            # Models like to write things like this.
            ("press 2", "2"),
            ("1-800-555-0100", "18005550100"),
        ],
    )
    def test_keeps_only_dialable_characters(self, value, expected):
        assert "".join(e.value for e in parse_dtmf_digits(value)) == expected

    @pytest.mark.parametrize("value", [None, "", "abc", 42, ["1"]])
    def test_returns_nothing_for_unusable_values(self, value):
        assert parse_dtmf_digits(value) == []

    def test_caps_the_length(self):
        assert len(parse_dtmf_digits("1" * 100, max_digits=5)) == 5


class TestSendDtmfDigits:
    async def test_queues_the_keypresses(self):
        queue_frame = AsyncMock()

        result = await send_dtmf_digits(queue_frame, "12#")

        assert result == {"status": "ok", "pressed": "12#"}
        frame = queue_frame.call_args.args[0]
        assert isinstance(frame, OutputDTMFFrame)
        assert [b.value for b in frame.buttons] == ["1", "2", "#"]

    async def test_reports_an_unusable_value_back_to_the_model(self):
        queue_frame = AsyncMock()

        result = await send_dtmf_digits(queue_frame, "hello")

        assert result["status"] == "error"
        queue_frame.assert_not_awaited()

    async def test_reports_when_the_call_cannot_send(self):
        result = await send_dtmf_digits(None, "1")
        assert result["status"] == "error"


class TestDTMFCapture:
    async def test_pound_ends_the_entry(self):
        captured: list[str] = []

        async def on_entry(entry: str) -> None:
            captured.append(entry)

        processor = DTMFCaptureProcessor(on_entry=on_entry, interdigit_timeout=30)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                _press("1"), _press("2"), _press("3"), _press("#")
            ),
        )

        assert _appended(down) == ["[The caller pressed these keys: 123]"]
        assert captured == ["123"]

    async def test_quiet_ends_the_entry(self):
        processor = DTMFCaptureProcessor(interdigit_timeout=0.05)

        down, _ = await run_test(
            processor,
            frames_to_send=[_press("4"), _press("2"), SleepFrame(0.3)],
        )

        assert _appended(down) == ["[The caller pressed these keys: 42]"]

    async def test_individual_keypresses_do_not_reach_downstream(self):
        processor = DTMFCaptureProcessor(interdigit_timeout=30)

        down, _ = await run_test(
            processor, frames_to_send=_in_order(_press("7"), _press("#"))
        )

        assert not [f for f in down if isinstance(f, InputDTMFFrame)]

    async def test_other_frames_pass_through(self):
        processor = DTMFCaptureProcessor(interdigit_timeout=30)
        transcription = TranscriptionFrame("hello", "caller", time_now_iso8601())

        down, _ = await run_test(processor, frames_to_send=[transcription])

        assert [f.text for f in down if isinstance(f, TranscriptionFrame)] == ["hello"]

    async def test_caps_the_entry_length(self):
        processor = DTMFCaptureProcessor(interdigit_timeout=30, max_digits=3)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                _press("1"), _press("2"), _press("3"), _press("4")
            ),
        )

        assert _appended(down)[0] == "[The caller pressed these keys: 123]"

    async def test_a_lone_pound_produces_nothing(self):
        processor = DTMFCaptureProcessor(interdigit_timeout=30)

        down, _ = await run_test(processor, frames_to_send=_in_order(_press("#")))

        assert _appended(down) == []

    async def test_a_failing_callback_does_not_stop_the_turn(self):
        async def on_entry(entry: str) -> None:
            raise RuntimeError("context gone")

        processor = DTMFCaptureProcessor(on_entry=on_entry, interdigit_timeout=30)

        down, _ = await run_test(
            processor, frames_to_send=_in_order(_press("5"), _press("#"))
        )

        assert _appended(down) == ["[The caller pressed these keys: 5]"]


class TestDTMFConfiguration:
    def test_capture_on_send_off_by_default(self):
        config = DTMFConfiguration()
        # Capture costs nothing; sending puts a tool on every node.
        assert config.capture_enabled is True
        assert config.send_enabled is False

    @pytest.mark.parametrize("timeout", [0, -1, 31])
    def test_rejects_an_out_of_range_timeout(self, timeout):
        with pytest.raises(Exception):
            DTMFConfiguration.model_validate({"interdigit_timeout_seconds": timeout})
