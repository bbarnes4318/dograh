"""Tests for the escalating idle-nudge ladder and the tool-call thinking cue."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import (
    LLMMessagesAppendFrame,
    TTSSpeakFrame,
    UserIdleTimeoutUpdateFrame,
)
from pipecat.utils.enums import EndTaskReason

from api.schemas.workflow_configurations import (
    IdleBehaviorConfiguration,
    IdleNudgeConfiguration,
    ToolFillerConfiguration,
    default_idle_nudges,
)
from api.services.pipecat.thinking_cue import ThinkingCue
from api.services.workflow.pipecat_engine_callbacks import (
    DEFAULT_LLM_IDLE_INSTRUCTION,
    create_user_idle_handler,
)


@pytest.fixture
def engine():
    engine = MagicMock()
    engine.task = MagicMock()
    engine.task.queue_frame = AsyncMock()
    engine.end_call_with_reason = AsyncMock()
    return engine


@pytest.fixture
def aggregator():
    aggregator = MagicMock()
    aggregator.push_frame = AsyncMock()
    return aggregator


def _spoken(engine) -> list[TTSSpeakFrame]:
    return [
        call.args[0]
        for call in engine.task.queue_frame.call_args_list
        if isinstance(call.args[0], TTSSpeakFrame)
    ]


def _timeout_updates(engine) -> list[float]:
    return [
        call.args[0].timeout
        for call in engine.task.queue_frame.call_args_list
        if isinstance(call.args[0], UserIdleTimeoutUpdateFrame)
    ]


def _llm_instructions(aggregator) -> list[str]:
    return [
        call.args[0].messages[0]["content"]
        for call in aggregator.push_frame.call_args_list
        if isinstance(call.args[0], LLMMessagesAppendFrame)
    ]


class TestIdleNudgeLadder:
    async def test_first_default_nudge_is_canned_and_skips_the_llm(
        self, engine, aggregator
    ):
        handler = create_user_idle_handler(engine, nudges=default_idle_nudges())

        await handler.handle_idle(aggregator)

        assert len(_spoken(engine)) == 1
        assert _llm_instructions(aggregator) == []
        engine.end_call_with_reason.assert_not_called()

    async def test_default_ladder_ends_the_call_on_the_third_nudge(
        self, engine, aggregator
    ):
        handler = create_user_idle_handler(engine, nudges=default_idle_nudges())

        for _ in range(3):
            await handler.handle_idle(aggregator)

        # One canned line, then two LLM-generated ones.
        assert len(_spoken(engine)) == 1
        assert len(_llm_instructions(aggregator)) == 2
        engine.end_call_with_reason.assert_awaited_once_with(
            EndTaskReason.USER_IDLE_MAX_DURATION_EXCEEDED.value
        )

    async def test_gives_the_caller_more_room_than_it_used_to(self, engine, aggregator):
        """The old ladder hung up on the second idle; the default now waits."""
        handler = create_user_idle_handler(engine, nudges=default_idle_nudges())

        await handler.handle_idle(aggregator)
        await handler.handle_idle(aggregator)

        engine.end_call_with_reason.assert_not_called()

    async def test_arms_the_next_nudges_timeout(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine,
            nudges=[
                IdleNudgeConfiguration(message="still there?"),
                IdleNudgeConfiguration(after_seconds=20.0, message="hello?"),
            ],
        )

        await handler.handle_idle(aggregator)

        assert _timeout_updates(engine) == [20.0]

    async def test_does_not_arm_a_timeout_the_next_nudge_does_not_set(
        self, engine, aggregator
    ):
        handler = create_user_idle_handler(
            engine,
            nudges=[
                IdleNudgeConfiguration(message="still there?"),
                IdleNudgeConfiguration(message="hello?"),
            ],
        )

        await handler.handle_idle(aggregator)

        assert _timeout_updates(engine) == []

    async def test_llm_nudge_uses_its_own_instruction(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine,
            nudges=[IdleNudgeConfiguration(llm_instruction="ask again in Spanish")],
        )

        await handler.handle_idle(aggregator)

        assert _llm_instructions(aggregator) == ["ask again in Spanish"]

    async def test_llm_nudge_falls_back_to_the_default_instruction(
        self, engine, aggregator
    ):
        handler = create_user_idle_handler(engine, nudges=[IdleNudgeConfiguration()])

        await handler.handle_idle(aggregator)

        assert _llm_instructions(aggregator) == [DEFAULT_LLM_IDLE_INSTRUCTION]

    async def test_running_past_the_ladder_ends_the_call(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine, nudges=[IdleNudgeConfiguration(message="only one")]
        )

        await handler.handle_idle(aggregator)
        await handler.handle_idle(aggregator)

        engine.end_call_with_reason.assert_awaited_once()

    async def test_disabled_does_nothing(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine, nudges=default_idle_nudges(), enabled=False
        )

        await handler.handle_idle(aggregator)

        assert _spoken(engine) == []
        assert _llm_instructions(aggregator) == []
        engine.end_call_with_reason.assert_not_called()

    async def test_reset_restarts_the_ladder(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine,
            nudges=[
                IdleNudgeConfiguration(message="first"),
                IdleNudgeConfiguration(message="second"),
            ],
        )

        await handler.handle_idle(aggregator)
        await handler.reset()
        await handler.handle_idle(aggregator)

        assert [frame.text for frame in _spoken(engine)] == ["first", "first"]

    async def test_canned_nudge_is_appended_to_context(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine, nudges=[IdleNudgeConfiguration(message="still there?")]
        )

        await handler.handle_idle(aggregator)

        # So the model knows it already prompted and doesn't repeat itself.
        assert _spoken(engine)[0].append_to_context is True


class TestIdleBehaviorConfiguration:
    def test_defaults_to_the_three_step_ladder(self):
        config = IdleBehaviorConfiguration()
        assert config.enabled is True
        assert len(config.nudges) == 3
        assert config.nudges[0].message
        assert config.nudges[-1].end_call is True

    def test_accepts_a_custom_ladder(self):
        config = IdleBehaviorConfiguration.model_validate(
            {"nudges": [{"message": "hey", "after_seconds": 5}]}
        )
        assert config.nudges[0].message == "hey"
        assert config.nudges[0].after_seconds == 5


class TestThinkingCue:
    async def test_stays_quiet_when_the_call_beats_the_deadline(self):
        queue_frame = AsyncMock()
        cue = ThinkingCue(
            queue_frame=queue_frame, phrases=["one moment"], delay_seconds=5.0
        )

        async with cue:
            pass

        queue_frame.assert_not_awaited()
        assert cue.spoken is None

    async def test_speaks_when_the_call_runs_long(self):
        queue_frame = AsyncMock()
        cue = ThinkingCue(
            queue_frame=queue_frame, phrases=["one moment"], delay_seconds=0.01
        )

        async with cue:
            await asyncio.sleep(0.08)

        queue_frame.assert_awaited_once()
        frame = queue_frame.call_args.args[0]
        assert isinstance(frame, TTSSpeakFrame)
        assert frame.text == "one moment"
        # Filler is padding, not content the model should reason over.
        assert frame.append_to_context is False
        assert cue.spoken == "one moment"

    async def test_disabled_never_speaks(self):
        queue_frame = AsyncMock()
        cue = ThinkingCue(
            queue_frame=queue_frame,
            phrases=["one moment"],
            delay_seconds=0.01,
            enabled=False,
        )

        async with cue:
            await asyncio.sleep(0.05)

        queue_frame.assert_not_awaited()

    async def test_no_phrases_configured_never_speaks(self):
        queue_frame = AsyncMock()
        cue = ThinkingCue(queue_frame=queue_frame, phrases=[], delay_seconds=0.01)

        async with cue:
            await asyncio.sleep(0.05)

        queue_frame.assert_not_awaited()

    async def test_avoids_repeating_the_previous_phrase(self):
        queue_frame = AsyncMock()
        cue = ThinkingCue(
            queue_frame=queue_frame,
            phrases=["a", "b"],
            delay_seconds=0.01,
            last_phrase="a",
        )

        async with cue:
            await asyncio.sleep(0.08)

        assert cue.spoken == "b"

    async def test_reports_what_it_spoke(self):
        queue_frame = AsyncMock()
        seen: list[str] = []
        cue = ThinkingCue(
            queue_frame=queue_frame,
            phrases=["only"],
            delay_seconds=0.01,
            on_spoken=seen.append,
        )

        async with cue:
            await asyncio.sleep(0.08)

        assert seen == ["only"]

    async def test_survives_a_failing_queue(self):
        queue_frame = AsyncMock(side_effect=RuntimeError("pipeline gone"))
        cue = ThinkingCue(
            queue_frame=queue_frame, phrases=["one moment"], delay_seconds=0.01
        )

        async with cue:
            await asyncio.sleep(0.08)

        # Swallowed: a filler failing must never take down a tool call.
        queue_frame.assert_awaited_once()

    async def test_exception_in_the_body_still_cancels_the_cue(self):
        queue_frame = AsyncMock()

        with pytest.raises(ValueError):
            async with ThinkingCue(
                queue_frame=queue_frame, phrases=["hold on"], delay_seconds=5.0
            ):
                raise ValueError("tool blew up")

        queue_frame.assert_not_awaited()


class TestToolFillerConfiguration:
    def test_sensible_defaults(self):
        config = ToolFillerConfiguration()
        assert config.enabled is True
        assert config.delay_seconds == 1.2
        assert config.phrases

    @pytest.mark.parametrize("delay", [0, -1, 11])
    def test_rejects_an_out_of_range_delay(self, delay):
        with pytest.raises(Exception):
            ToolFillerConfiguration.model_validate({"delay_seconds": delay})
