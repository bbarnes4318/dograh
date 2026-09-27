"""Tests for the call hygiene guards.

The utterance fixtures are verbatim production transcripts from 9/25 —
voicemail greetings, post-recording carrier menus, call screeners and the real
people they must not be confused with.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    FunctionCallFromLLM,
    FunctionCallsStartedFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.enums import EndTaskReason

from api.schemas.workflow_configurations import CallHygieneConfiguration
from api.services.pipecat.call_hygiene import (
    AssistantTurnGuard,
    CallHygieneState,
    MachineAnswerGuard,
    build_tts_text_filter,
    classify_utterance,
    matches_closing_line,
)
from api.services.workflow.pipecat_engine_callbacks import create_user_idle_handler
from api.utils.telephony_address import is_dialable_pstn

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


@pytest.fixture
def engine():
    engine = MagicMock()
    engine.task = MagicMock()
    engine.task.queue_frame = AsyncMock()
    engine.end_call_with_reason = AsyncMock()
    engine._gathered_context = {}
    engine.call_hygiene = CallHygieneState()
    disposed = {"value": False}

    async def _end_for_hygiene(**kwargs):
        disposed["value"] = True

    async def _end_with_reason(*args, **kwargs):
        disposed["value"] = True

    engine.end_call_for_hygiene = AsyncMock(side_effect=_end_for_hygiene)
    engine.end_call_with_reason = AsyncMock(side_effect=_end_with_reason)
    engine.is_call_disposed = MagicMock(side_effect=lambda: disposed["value"])
    return engine


def _final(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="caller", timestamp="now")


def _interim(text: str) -> InterimTranscriptionFrame:
    return InterimTranscriptionFrame(text=text, user_id="caller", timestamp="now")


def _guard(engine, **config) -> MachineAnswerGuard:
    guard = MachineAnswerGuard(engine, CallHygieneConfiguration(**config))
    guard.push_frame = AsyncMock()
    return guard


def _pushed(processor) -> list:
    return [call.args[0] for call in processor.push_frame.call_args_list]


# ---------------------------------------------------------------------------
# classify_utterance
# ---------------------------------------------------------------------------

MACHINE_UTTERANCES = [
    "Your call has been forwarded to an automated voice messaging system. is not available. At the tone, please record your message. When you've finished recording, you may hang up or press one for more options.",
    "call has been forwarded to voice mail. The person you're trying to reach is not available. At the tone, please record your message.",
    "Sorry. Mailbox is full. To send an SMS notification, press five.",
    "To review, rerecord, or add to your message, press one. To mark your message urgent, press two.",
    "I couldn't hear you. Please try again.",
    "Are you still there? Your message has been sent. Goodbye.",
    "You have reached the maximum time permitted for recording your message.",
    "Eight one two five eight zero zero zero zero. This mailbox is currently unavailable.",
    "Hey. This is Bonnie Jean. Sorry I missed your call. Leave me a name, a number, and a detailed message, and I'll get back to you as soon as I can.",
    "reach Sterling Green. Sorry. I can't get to the phone right now. But if you leave your name, number, and a brief message, I will return your call.",
    "one four three one six nine two two zero is not available.",
    "Thanks. Please stay on the line. I'm sorry. This person is not available. If you would like to leave an additional message, please reply after the tone.",
    "After you have finished your message, just hang up. Or to hear more options, please press one.",
]

SCREENER_UTTERANCES = [
    "Thanks. Please stay on the line.",
    "I'm a call assistant recording this call for the person you're trying to reach. Please say who you are and why you're calling.",
    "Hi. You've reached Sharon. Can I please get your name?",
    "What is this regarding, Alex?",
]

HUMAN_UTTERANCES = [
    "I'm on a do not call list. Please don't call me anymore.",
    "Yeah. And I don't need it. Thank you very much. But, hey, hey, can you give me a favor?",
    "Can you take me off your list, please?",
    "Hello? Say that again?",
    "I'm in a vehicle. I can't hear you. You'll have to call back later.",
    "Will you help? Yeah.",
    "Well, no. No. No. No. No.",
    "the middle of not wanting to talk to you. Quit calling me. Lose my number. Do not ever call this number again, or I will report. Let me talk to your supervisor. right now.",
    "Okay. You... stop. Stop. Stop. I'm just gonna get out of the hospital. Please take my name off your list.",
    "Who is this?",
    "Whom are you looking for?",
    "Hello?",
]


@pytest.mark.parametrize("text", MACHINE_UTTERANCES)
def test_machine_utterances(text):
    assert classify_utterance(text, in_screener_hold=False) == "machine"


@pytest.mark.parametrize("text", SCREENER_UTTERANCES)
def test_screener_utterances(text):
    assert classify_utterance(text, in_screener_hold=False) == "screener"


@pytest.mark.parametrize("text", HUMAN_UTTERANCES)
def test_human_utterances(text):
    assert classify_utterance(text, in_screener_hold=False) == "other"


def test_thank_you_is_screener_only_while_in_hold():
    assert classify_utterance("Thank you.", in_screener_hold=True) == "screener"
    assert classify_utterance("Thank you.", in_screener_hold=False) == "other"


# ---------------------------------------------------------------------------
# TTS scrub
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("How old are you<|think_end|> <|think_end|>", "How old are you"),
        ("<tool_code> print(end_call()) </tool_code> </tool_code>", ""),
        ("Perfect. I'm here. <tool_call> </tool_call>", "Perfect. I'm here."),
        ("(Silence) <|think_end|>", ""),
        ("(No response)", ""),
        (
            'Option 2: "Just wanted to check, are you still on the line?" (Better) * Option 3: "No worries if you\'re busy, just check',
            "",
        ),
        (
            "* *Option 2:* Just checking if you're still there, I know sometimes lines go dead. * *Option 3 (Warm & brief):",
            "",
        ),
        (
            '<tool_code> print("responded") </tool_code> <|think_end|> Got it, thanks. I\'m here.',
            "Got it, thanks. I'm here.",
        ),
        ("[End of conversation] <tool_code> print(end_call()) </tool_code>", ""),
        (
            "<function <function <p arameter=to> done </parameter> </function> <|tool_call_end|>",
            "",
        ),
        (
            "I <function=end_call> </function> <|tool_call_end|> <|tool_call_end|>",
            "I",
        ),
        ("The call has been disconnected due to a voicemail. <|think_end|>", ""),
    ],
)
async def test_tts_scrub(raw, expected):
    assert await build_tts_text_filter().filter(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Hi, this is Alex with Family Life. I'm calling about the coverage that pays for funeral costs. Did I catch you in the middle of something?",
        "A licensed agent can walk you through some simple options that start under a buck a day (no pressure).",
        "Option one is a whole life plan.",
    ],
)
async def test_tts_scrub_leaves_normal_speech_alone(text):
    assert await build_tts_text_filter().filter(text) == text


# ---------------------------------------------------------------------------
# Closing lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Oh, gotcha, no problem. Take care.",
        "Alright, I'll hang up now and leave the line open for you.",
        "I understand. I will stop the call now.",
        "Understood. I'll end the call here.",
        "I'll let them call back if needed. Thanks.",
        "Absolutely, I'm going to take you off our list right now. You won't get another call from us.",
        "I'll make sure you're taken off the list right away. Have a good one.",
    ],
)
def test_closing_line_matches(text):
    assert matches_closing_line(text)


@pytest.mark.parametrize(
    "text",
    [
        "Got it, thanks. So are you living in your own home these days?",
        "Would you be open to speaking with a licensed agent about this?",
        "Yes, I'm right here with you.",
    ],
)
def test_closing_line_does_not_match_normal_turns(text):
    assert not matches_closing_line(text)


# ---------------------------------------------------------------------------
# MachineAnswerGuard
# ---------------------------------------------------------------------------


class TestMachineAnswerGuard:
    async def test_interim_voicemail_ends_the_call_immediately(self, engine):
        guard = _guard(engine)

        await guard.process_frame(
            _interim("your call has been forwarded to an automated voice"), DOWN
        )

        engine.end_call_for_hygiene.assert_awaited_once_with(
            disposition="voicemail_detected",
            tag="vm_keyword_guard",
            abort_immediately=True,
        )
        assert engine.call_hygiene.machine_detected is True
        assert _pushed(guard) == []

    async def test_interim_screener_phrase_does_not_act(self, engine):
        guard = _guard(engine)
        frame = _interim("thanks please stay on the line")

        await guard.process_frame(frame, DOWN)

        engine.end_call_for_hygiene.assert_not_called()
        assert engine.call_hygiene.screener_hold is False
        assert _pushed(guard) == [frame]

    async def test_human_final_is_forwarded(self, engine):
        guard = _guard(engine)
        frame = _final("Who is this?")

        await guard.process_frame(frame, DOWN)

        assert _pushed(guard) == [frame]
        assert engine.call_hygiene.user_has_spoken is True
        engine.end_call_for_hygiene.assert_not_called()

    async def test_screener_speaks_once_then_pickup_clears_hold(self, engine):
        guard = _guard(
            engine,
            screener_response="This is Alex with Family Life about final expense coverage.",
        )

        await guard.process_frame(_final("Thanks. Please stay on the line."), DOWN)
        await guard.process_frame(
            _final(
                "I'm a call assistant recording this call for the person you're "
                "trying to reach. Please say who you are and why you're calling."
            ),
            DOWN,
        )

        spoken = [
            call.args[0]
            for call in engine.task.queue_frame.call_args_list
            if isinstance(call.args[0], TTSSpeakFrame)
        ]
        assert [f.text for f in spoken] == [
            "This is Alex with Family Life about final expense coverage."
        ]
        assert engine.call_hygiene.screener_hold is True
        assert _pushed(guard) == []

        hello = _final("Hello?")
        await guard.process_frame(hello, DOWN)

        assert engine.call_hygiene.screener_hold is False
        pushed = _pushed(guard)
        assert isinstance(pushed[0], LLMMessagesAppendFrame)
        assert pushed[0].run_llm is False
        assert pushed[1] is hello
        engine.end_call_for_hygiene.assert_not_called()
        await guard.cleanup()

    async def test_screener_without_response_asks_the_llm(self, engine):
        guard = _guard(engine)

        await guard.process_frame(_final("What is this regarding, Alex?"), DOWN)

        pushed = _pushed(guard)
        assert len(pushed) == 1
        assert isinstance(pushed[0], LLMMessagesAppendFrame)
        assert pushed[0].run_llm is True
        await guard.cleanup()

    async def test_screener_without_pickup_ends_the_call(self, engine):
        guard = _guard(
            engine,
            screener_response="This is Alex.",
            screener_pickup_timeout_seconds=0.05,
        )

        await guard.process_frame(_final("Thanks. Please stay on the line."), DOWN)
        await asyncio.sleep(0.15)

        engine.end_call_for_hygiene.assert_awaited_once_with(
            disposition="voicemail_detected",
            tag="call_screener_no_pickup",
            abort_immediately=True,
        )

    async def test_voicemail_after_screener_is_tagged(self, engine):
        guard = _guard(engine, screener_response="This is Alex.")

        await guard.process_frame(_final("Thanks. Please stay on the line."), DOWN)
        await guard.process_frame(
            _final("This person is not available. Please leave a message."), DOWN
        )

        engine.end_call_for_hygiene.assert_awaited_once_with(
            disposition="voicemail_detected",
            tag="vm_after_screener",
            abort_immediately=True,
        )
        await guard.cleanup()

    async def test_answering_bot_sequence_ends_the_call(self, engine):
        guard = _guard(engine)

        for text in [
            "I hope this isn't a telemarketing call. Are you still there?",
            "Go on.",
            "Are you still there?",
            "Uh-huh. Yep.",
            "I'm listening.",
        ]:
            await guard.process_frame(_final(text), DOWN)

        engine.end_call_for_hygiene.assert_awaited_once_with(
            disposition="answering_bot",
            tag="answering_bot",
            abort_immediately=True,
        )
        assert engine.call_hygiene.answering_bot is True

    async def test_context_messages_bypass_the_pipeline_when_injected(self, engine):
        injected = []

        async def _inject(frame):
            injected.append(frame)

        guard = MachineAnswerGuard(
            engine, CallHygieneConfiguration(), inject_context_frame=_inject
        )
        guard.push_frame = AsyncMock()

        await guard.process_frame(_final("What is this regarding, Alex?"), DOWN)
        hello = _final("Hello?")
        await guard.process_frame(hello, DOWN)

        assert [f.run_llm for f in injected] == [True, False]
        assert _pushed(guard) == [hello]
        await guard.cleanup()

    async def test_dropped_speech_is_logged(self, engine):
        logged = []

        async def _log(text):
            logged.append(text)

        guard = MachineAnswerGuard(
            engine,
            CallHygieneConfiguration(screener_response="This is Alex."),
            on_dropped_transcription=_log,
        )
        guard.push_frame = AsyncMock()

        await guard.process_frame(_final("Thanks. Please stay on the line."), DOWN)

        assert logged == ["Thanks. Please stay on the line."]
        await guard.cleanup()

    async def test_everything_passes_once_disposed(self, engine):
        engine.is_call_disposed = MagicMock(return_value=True)
        guard = _guard(engine)
        frame = _final("Please leave a message after the tone.")

        await guard.process_frame(frame, DOWN)

        assert _pushed(guard) == [frame]
        engine.end_call_for_hygiene.assert_not_called()


# ---------------------------------------------------------------------------
# AssistantTurnGuard
# ---------------------------------------------------------------------------


def _turn_guard(engine, **config) -> AssistantTurnGuard:
    config.setdefault("closing_line_grace_seconds", 0.01)
    guard = AssistantTurnGuard(engine, CallHygieneConfiguration(**config))
    guard.push_frame = AsyncMock()
    return guard


async def _generation(guard, text: str, *, function_call: bool = False) -> None:
    await guard.process_frame(LLMFullResponseStartFrame(), DOWN)
    await guard.process_frame(LLMTextFrame(text=text), DOWN)
    if function_call:
        await guard.process_frame(
            FunctionCallsStartedFrame(
                function_calls=[
                    FunctionCallFromLLM(
                        function_name="end_call",
                        tool_call_id="1",
                        arguments={},
                        context=None,
                    )
                ]
            ),
            DOWN,
        )
    await guard.process_frame(LLMFullResponseEndFrame(), DOWN)


class TestAssistantTurnGuard:
    async def test_closing_line_hangs_up_after_bot_stops(self, engine):
        guard = _turn_guard(engine)

        await _generation(guard, "Oh, gotcha, no problem. Take care.")
        await asyncio.sleep(0.05)
        engine.end_call_with_reason.assert_not_called()

        await guard.process_frame(BotStoppedSpeakingFrame(), UP)
        await asyncio.sleep(0.05)

        engine.end_call_with_reason.assert_awaited_once_with(
            EndTaskReason.END_CALL_TOOL_REASON.value, abort_immediately=False
        )
        assert "closing_line_watchdog" in engine._gathered_context["call_tags"]

    async def test_end_call_written_as_text_hangs_up(self, engine):
        guard = _turn_guard(engine)

        await _generation(guard, "<tool_code> print(end_call()) </tool_code>")
        # Nothing speakable, so no BotStoppedSpeakingFrame will arrive.
        await asyncio.sleep(0.05)

        engine.end_call_with_reason.assert_awaited_once_with(
            EndTaskReason.END_CALL_TOOL_REASON.value, abort_immediately=False
        )
        assert "llm_markup_leak" in engine._gathered_context["call_tags"]

    async def test_end_call_text_with_real_tool_call_is_left_to_the_tool(self, engine):
        guard = _turn_guard(engine)

        await _generation(guard, "print(end_call())", function_call=True)
        await guard.process_frame(BotStoppedSpeakingFrame(), UP)
        await asyncio.sleep(0.05)

        engine.end_call_with_reason.assert_not_called()

    async def test_normal_question_does_not_hang_up(self, engine):
        guard = _turn_guard(engine)

        await _generation(
            guard, "Would you be open to speaking with a licensed agent about this?"
        )
        await guard.process_frame(BotStoppedSpeakingFrame(), UP)
        await asyncio.sleep(0.05)

        engine.end_call_with_reason.assert_not_called()
        assert guard.is_armed is False

    async def test_new_generation_cancels_pending_hangup(self, engine):
        guard = _turn_guard(engine)

        await _generation(guard, "Take care.")
        await _generation(guard, "Actually, one more question for you.")
        await guard.process_frame(BotStoppedSpeakingFrame(), UP)
        await asyncio.sleep(0.05)

        engine.end_call_with_reason.assert_not_called()
        await guard.cleanup()


# ---------------------------------------------------------------------------
# UserIdleHandler first-response ladder
# ---------------------------------------------------------------------------


class TestFirstResponseIdle:
    @pytest.fixture
    def aggregator(self):
        aggregator = MagicMock()
        aggregator.push_frame = AsyncMock()
        return aggregator

    async def test_never_spoke_nudges_then_ends(self, engine, aggregator):
        handler = create_user_idle_handler(
            engine, base_timeout=10.0, call_hygiene=CallHygieneConfiguration()
        )

        await handler.handle_idle(aggregator)
        spoken = [
            call.args[0].text
            for call in engine.task.queue_frame.call_args_list
            if isinstance(call.args[0], TTSSpeakFrame)
        ]
        assert spoken == ["Hello? Can you hear me okay?"]
        engine.end_call_for_hygiene.assert_not_called()

        await handler.handle_idle(aggregator)
        engine.end_call_for_hygiene.assert_awaited_once()
        assert (
            engine.end_call_for_hygiene.call_args.kwargs["disposition"] == "no_speech"
        )
        assert (
            engine.end_call_for_hygiene.call_args.kwargs["tag"] == "no_speech_dead_air"
        )

    async def test_first_turn_restores_base_timeout_and_normal_ladder(
        self, engine, aggregator
    ):
        handler = create_user_idle_handler(
            engine, base_timeout=10.0, call_hygiene=CallHygieneConfiguration()
        )

        await handler.reset()

        timeouts = [
            call.args[0].timeout
            for call in engine.task.queue_frame.call_args_list
            if hasattr(call.args[0], "timeout")
        ]
        assert timeouts == [10.0]

        await handler.handle_idle(aggregator)
        engine.end_call_for_hygiene.assert_not_called()

    async def test_screener_hold_suppresses_idle(self, engine, aggregator):
        engine.call_hygiene.screener_hold = True
        handler = create_user_idle_handler(
            engine, base_timeout=10.0, call_hygiene=CallHygieneConfiguration()
        )

        await handler.handle_idle(aggregator)

        engine.task.queue_frame.assert_not_called()
        aggregator.push_frame.assert_not_called()
        engine.end_call_for_hygiene.assert_not_called()
        engine.end_call_with_reason.assert_not_called()


# ---------------------------------------------------------------------------
# is_dialable_pstn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "number",
    [
        "10000000000",
        "+10000000000",
        "+11234567890",
        "+12120550000",
        "+19112345678",
        "+12222222222",
    ],
)
def test_undialable_numbers(number):
    assert is_dialable_pstn(number) is False


@pytest.mark.parametrize(
    "number",
    ["+18653173943", "8653173943", "+447911123456", "sip:1000000000@pbx.example"],
)
def test_dialable_numbers(number):
    assert is_dialable_pstn(number) is True


# ---------------------------------------------------------------------------
# Transfer gate
# ---------------------------------------------------------------------------


async def test_transfer_is_blocked_when_machine_answered(engine):
    from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

    engine.call_hygiene.machine_detected = True
    manager = CustomToolManager(engine)
    tool = SimpleNamespace(definition={"config": {"destination": "+18653173943"}})
    handler = manager._create_transfer_call_handler(tool, "transfer_call")

    params = MagicMock()
    params.arguments = {}
    params.result_callback = AsyncMock()

    await handler(params)

    result = params.result_callback.call_args.args[0]
    assert result["action"] == "transfer_blocked"
    assert result["reason"] == "machine_answered"
    engine.end_call_for_hygiene.assert_awaited_once_with(
        disposition="voicemail_detected",
        tag="transfer_blocked_machine",
        abort_immediately=True,
    )
