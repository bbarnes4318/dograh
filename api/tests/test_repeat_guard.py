"""Tests for RepeatGuard: the agent never says the same thing twice on a call.

The guard sits after the LLM and holds each generation's text a sentence at a
time. These tests drive it with LLM frames and a fake engine whose context
holds what was said before.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    FunctionCallFromLLM,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import SleepFrame, run_test

from api.services.pipecat.muted_speech_buffer import REPLAY_PREFIX
from api.services.pipecat.repeat_guard import (
    RepeatGuard,
    allows_one_more_time,
    is_repeat_request,
    sentence_key,
    split_sentences,
)
from api.services.workflow.pipecat_engine_context_composer import (
    NO_REPEAT_RULES,
    TTS_MARKER,
    compose_system_prompt_for_node,
)

AGE_QUESTION = "Are you between forty and eighty-five?"
DECISION_QUESTION = "Do you usually handle your own insurance decisions?"


class _Context:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self):
        return self.messages


def _engine(messages=None):
    return SimpleNamespace(
        context=_Context(list(messages or [])),
        is_call_disposed=lambda: False,
        conversation_output_enabled=True,
    )


def _generation(*tokens, function_call=False):
    frames = [LLMFullResponseStartFrame()]
    frames += [LLMTextFrame(token) for token in tokens]
    if function_call:
        frames.append(
            FunctionCallsStartedFrame(
                function_calls=[
                    FunctionCallFromLLM(
                        function_name="transfer_call",
                        tool_call_id="call-1",
                        arguments={},
                        context=None,
                    )
                ]
            )
        )
    frames.append(LLMFullResponseEndFrame())
    return frames


async def _spoken(guard, frames):
    down, _ = await run_test(guard, frames_to_send=frames)
    return "".join(f.text for f in down if isinstance(f, LLMTextFrame))


def _regeneration_notes(inject: AsyncMock) -> list[str]:
    return [
        call.args[0].messages[0]["content"]
        for call in inject.call_args_list
        if isinstance(call.args[0], LLMMessagesAppendFrame)
    ]


class TestHelpers:
    def test_sentence_key_ignores_case_punctuation_and_leading_filler(self):
        assert sentence_key("Okay, so are you between forty and eighty-five?") == (
            sentence_key(AGE_QUESTION)
        )
        assert sentence_key("And " + AGE_QUESTION) == sentence_key(AGE_QUESTION)

    def test_split_sentences_keeps_the_unfinished_tail(self):
        assert split_sentences("Okay. Are you") == ["Okay. ", "Are you"]
        assert split_sentences("Okay. Are you? ") == ["Okay. ", "Are you? "]

    @pytest.mark.parametrize(
        "text",
        ["What?", "Huh?", "Sorry, say that again?", "I didn't hear you", "Hello?"],
    )
    def test_repeat_requests(self, text):
        assert is_repeat_request(text)

    @pytest.mark.parametrize("text", ["Yes I do.", "I'm 62.", "At home with my son."])
    def test_answers_are_not_repeat_requests(self, text):
        assert not allows_one_more_time(text)

    def test_a_question_back_allows_one_more_time(self):
        assert allows_one_more_time("Who is this?")
        assert allows_one_more_time("what is this about")


class TestRepeatGuard:
    async def test_new_text_passes_through_unchanged(self):
        guard = RepeatGuard(_engine())

        spoken = await _spoken(guard, _generation("Okay. ", AGE_QUESTION))

        assert spoken == "Okay. " + AGE_QUESTION

    async def test_drops_a_question_already_asked_and_answered(self):
        inject = AsyncMock()
        guard = RepeatGuard(
            _engine(
                [
                    {"role": "assistant", "content": AGE_QUESTION},
                    {"role": "user", "content": "Yes, I'm 62."},
                    {"role": "assistant", "content": "And are you living at home?"},
                    {"role": "user", "content": "Yes."},
                ]
            ),
            inject_context_frame=inject,
        )

        spoken = await _spoken(guard, _generation("Gotcha. ", "So " + AGE_QUESTION))

        assert spoken == "Gotcha. "
        # Nothing left for the caller to answer, so the model gets one note
        # asking for something new.
        notes = _regeneration_notes(inject)
        assert len(notes) == 1
        assert AGE_QUESTION in notes[0]

    async def test_drops_a_sentence_said_twice_in_one_generation(self):
        guard = RepeatGuard(_engine())

        spoken = await _spoken(
            guard,
            _generation("It's about final expense coverage. ")[:-1]
            + [
                LLMTextFrame("It's about final expense coverage. "),
                LLMTextFrame(AGE_QUESTION),
            ]
            + [LLMFullResponseEndFrame()],
        )

        assert spoken == "It's about final expense coverage. " + AGE_QUESTION

    async def test_drops_near_duplicates(self):
        guard = RepeatGuard(
            _engine(
                [
                    {
                        "role": "assistant",
                        "content": "It's about final expense coverage for funeral and burial costs.",
                    },
                    {"role": "user", "content": "Okay."},
                ]
            )
        )

        spoken = await _spoken(
            guard,
            _generation(
                "Well, it's about final expense coverage for funeral and burial cost. ",
                AGE_QUESTION,
            ),
        )

        assert spoken == AGE_QUESTION

    async def test_caller_asking_again_gets_it_once_more_but_never_twice(self):
        history = [
            {"role": "assistant", "content": DECISION_QUESTION},
            {"role": "user", "content": "What?"},
        ]
        guard = RepeatGuard(_engine(history))

        spoken = await _spoken(guard, _generation(DECISION_QUESTION))
        assert spoken == DECISION_QUESTION

        history += [
            {"role": "assistant", "content": DECISION_QUESTION},
            {"role": "user", "content": "Huh?"},
        ]
        spoken = await _spoken(guard, _generation(DECISION_QUESTION))
        assert spoken == ""

    async def test_caller_question_back_allows_one_re_ask(self):
        guard = RepeatGuard(
            _engine(
                [
                    {
                        "role": "assistant",
                        "content": "Hi, this is Alex. " + DECISION_QUESTION,
                    },
                    {"role": "user", "content": "Who is this?"},
                ]
            )
        )

        spoken = await _spoken(
            guard, _generation("It's Alex with Family Life. ", DECISION_QUESTION)
        )

        assert spoken == "It's Alex with Family Life. " + DECISION_QUESTION

    async def test_answer_does_not_license_a_re_ask(self):
        guard = RepeatGuard(
            _engine(
                [
                    {"role": "assistant", "content": DECISION_QUESTION},
                    {"role": "user", "content": "Yeah, I do."},
                ]
            )
        )

        spoken = await _spoken(guard, _generation(DECISION_QUESTION))

        assert spoken == ""

    async def test_replayed_muted_speech_is_read_as_the_callers_turn(self):
        guard = RepeatGuard(
            _engine(
                [
                    {"role": "assistant", "content": DECISION_QUESTION},
                    {"role": "user", "content": f"{REPLAY_PREFIX}what?"},
                ]
            )
        )

        spoken = await _spoken(guard, _generation(DECISION_QUESTION))

        assert spoken == DECISION_QUESTION

    async def test_short_acknowledgments_and_goodbyes_always_pass(self):
        guard = RepeatGuard(
            _engine(
                [
                    {"role": "assistant", "content": "Okay. Have a good day."},
                    {"role": "user", "content": "Bye."},
                ]
            )
        )

        spoken = await _spoken(guard, _generation("Okay. ", "Have a good day."))

        assert spoken == "Okay. Have a good day."

    async def test_engine_spoken_lines_count_as_said(self):
        guard = RepeatGuard(_engine([{"role": "user", "content": "Okay."}]))
        line = "A licensed agent can call you back within forty-eight hours."

        spoken = await _spoken(
            guard,
            [TTSSpeakFrame(line, append_to_context=False)] + _generation(line),
        )

        assert spoken == ""

    async def test_previous_generation_still_playing_counts_as_said(self):
        guard = RepeatGuard(_engine([{"role": "user", "content": "Yes."}]))

        spoken = await _spoken(
            guard, _generation(AGE_QUESTION) + _generation(AGE_QUESTION)
        )

        assert spoken == AGE_QUESTION

    async def test_interrupted_line_may_be_said_again(self):
        guard = RepeatGuard(_engine([{"role": "user", "content": "Yes."}]))

        spoken = await _spoken(
            guard,
            _generation(AGE_QUESTION)
            + [SleepFrame(0.05), InterruptionFrame()]
            + _generation(AGE_QUESTION),
        )

        assert spoken == AGE_QUESTION + AGE_QUESTION

    async def test_line_confirmed_spoken_is_not_taken_back_by_a_later_interruption(
        self,
    ):
        guard = RepeatGuard(_engine([{"role": "user", "content": "Yes."}]))

        spoken = await _spoken(
            guard,
            _generation(AGE_QUESTION)
            + [SleepFrame(0.05), BotStoppedSpeakingFrame(), InterruptionFrame()]
            + _generation(AGE_QUESTION),
        )

        assert spoken == AGE_QUESTION

    async def test_no_regeneration_when_a_tool_call_takes_over(self):
        inject = AsyncMock()
        guard = RepeatGuard(
            _engine(
                [
                    {"role": "assistant", "content": AGE_QUESTION},
                    {"role": "user", "content": "Yes."},
                ]
            ),
            inject_context_frame=inject,
        )

        await _spoken(guard, _generation(AGE_QUESTION, function_call=True))

        assert _regeneration_notes(inject) == []

    async def test_regenerates_at_most_once_per_caller_turn(self):
        inject = AsyncMock()
        messages = [
            {"role": "assistant", "content": AGE_QUESTION},
            {"role": "user", "content": "Yes."},
        ]
        engine = _engine(messages)

        async def deliver(frame):
            engine.context.messages.extend(frame.messages)

        inject.side_effect = deliver
        guard = RepeatGuard(engine, inject_context_frame=inject)

        await _spoken(guard, _generation(AGE_QUESTION) + _generation(AGE_QUESTION))

        assert len(_regeneration_notes(inject)) == 1

    async def test_keeps_the_speech_mode_marker_when_dropping(self):
        guard = RepeatGuard(
            _engine(
                [
                    {"role": "assistant", "content": AGE_QUESTION},
                    {"role": "user", "content": "Yes."},
                ]
            )
        )

        spoken = await _spoken(guard, _generation(f"{TTS_MARKER} {AGE_QUESTION}"))

        assert spoken == f"{TTS_MARKER} "

    async def test_upstream_frames_pass_untouched(self):
        guard = RepeatGuard(_engine())

        _, up = await run_test(
            guard,
            frames_to_send=[BotStoppedSpeakingFrame()],
            frames_to_send_direction=FrameDirection.UPSTREAM,
        )

        assert any(isinstance(f, BotStoppedSpeakingFrame) for f in up)


class TestNoRepeatPromptRules:
    def _workflow_and_node(self):
        node = SimpleNamespace(prompt="Ask their age.", add_global_prompt=False)
        workflow = SimpleNamespace(global_node_id=None, nodes={})
        return workflow, node

    def test_rules_are_appended_when_enabled(self):
        workflow, node = self._workflow_and_node()

        prompt = compose_system_prompt_for_node(
            node=node,
            workflow=workflow,
            format_prompt=lambda p: p,
            has_recordings=False,
            include_no_repeat_rules=True,
        )

        assert prompt == f"Ask their age.\n\n{NO_REPEAT_RULES}"

    def test_rules_are_absent_by_default(self):
        workflow, node = self._workflow_and_node()

        prompt = compose_system_prompt_for_node(
            node=node,
            workflow=workflow,
            format_prompt=lambda p: p,
            has_recordings=False,
        )

        assert prompt == "Ask their age."
