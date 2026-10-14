"""Tests for MutedSpeechBufferProcessor.

The processor sits between STT and the user aggregator and holds caller
transcriptions that the aggregator would drop while the caller is muted,
replaying them as a context message once they're unmuted.

The fake mute state here mirrors the real one: the engine mutes when the bot
starts speaking on a no-interrupt node and unmutes when it stops.
"""

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    Frame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.utils.time import time_now_iso8601

from api.enums import MuteReason
from api.services.pipecat.muted_speech_buffer import (
    REPLAY_PREFIX,
    MutedSpeechBufferProcessor,
)


class _MuteState:
    """Stand-in for the aggregator's mute flag and the engine's mute reason.

    Driven by bot-speaking frames so it tracks state the way the real pipeline
    does, without needing a full aggregator in the test pipeline.
    """

    def __init__(self, reason=MuteReason.NO_INTERRUPT):
        self.muted = False
        self.reason = None
        self.transition_in_progress = False
        self._mute_reason_when_speaking = reason

    def observe(self, frame: Frame) -> None:
        if isinstance(frame, BotStartedSpeakingFrame):
            self.muted = True
            self.reason = self._mute_reason_when_speaking
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.muted = False
            self.reason = None

    def is_muted(self) -> bool:
        return self.muted

    def mute_reason(self):
        return self.reason

    def suppress_generation(self) -> bool:
        return self.transition_in_progress


class _MuteStateProcessor(MutedSpeechBufferProcessor):
    """Buffer processor that also drives the fake mute state from frames.

    Subclassing keeps the state update ordered exactly like production: the
    aggregator (here, ``_MuteState``) sees bot-speaking frames before the
    buffer decides whether to hold or replay.
    """

    def __init__(self, state: _MuteState, **kwargs):
        super().__init__(
            is_muted=state.is_muted,
            mute_reason=state.mute_reason,
            suppress_generation=state.suppress_generation,
            **kwargs,
        )
        self._state = state

    async def process_frame(self, frame, direction):
        self._state.observe(frame)
        await super().process_frame(frame, direction)


def _transcription(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text, "caller", time_now_iso8601())


def _in_order(*frames: Frame) -> list[Frame]:
    """Interleave sleeps so frames are *processed* in the order given.

    Bot-speaking frames are SystemFrames and jump the processor's input queue,
    while transcriptions are DataFrames that wait in it. On a live call the two
    are separated by real time; in a test they have to be spaced explicitly or
    every system frame lands before the first data frame.
    """
    spaced: list[Frame] = []
    for frame in frames:
        spaced.extend([frame, SleepFrame(0.05)])
    return spaced


def _appended(frames) -> list[LLMMessagesAppendFrame]:
    return [f for f in frames if isinstance(f, LLMMessagesAppendFrame)]


def _transcriptions(frames) -> list[TranscriptionFrame]:
    return [f for f in frames if isinstance(f, TranscriptionFrame)]


class TestMutedSpeechBuffer:
    async def test_on_capture_fires_once_per_muted_transcription(self):
        state = _MuteState()
        calls: list[None] = []

        async def on_capture() -> None:
            calls.append(None)

        processor = _MuteStateProcessor(state, on_capture=on_capture)

        await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("Yeah. I hear you."),
                BotStoppedSpeakingFrame(),
                _transcription("hello"),
            ),
        )

        assert len(calls) == 1

    async def test_on_capture_failure_does_not_break_replay(self):
        state = _MuteState()

        async def on_capture() -> None:
            raise RuntimeError("boom")

        processor = _MuteStateProcessor(state, on_capture=on_capture)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("okay"),
                BotStoppedSpeakingFrame(),
            ),
        )

        assert len(_appended(down)) == 1

    async def test_passes_transcriptions_through_when_unmuted(self):
        state = _MuteState()
        processor = _MuteStateProcessor(state)

        down, _ = await run_test(
            processor, frames_to_send=_in_order(_transcription("hello there"))
        )

        assert [f.text for f in _transcriptions(down)] == ["hello there"]
        assert _appended(down) == []

    async def test_buffers_while_muted_and_replays_on_unmute(self):
        state = _MuteState()
        replayed: list[str] = []

        async def on_replay(text: str) -> None:
            replayed.append(text)

        processor = _MuteStateProcessor(state, on_replay=on_replay)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("I'm not interested"),
                BotStoppedSpeakingFrame(),
            ),
        )

        # Swallowed on the way in.
        assert _transcriptions(down) == []

        appended = _appended(down)
        assert len(appended) == 1
        message = appended[0].messages[0]
        assert message["role"] == "user"
        assert message["content"] == f"{REPLAY_PREFIX}I'm not interested"
        assert appended[0].run_llm is True
        assert replayed == ["I'm not interested"]

    async def test_joins_multiple_muted_utterances(self):
        state = _MuteState()
        processor = _MuteStateProcessor(state)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("wait"),
                _transcription("stop calling me"),
                BotStoppedSpeakingFrame(),
            ),
        )

        assert (
            _appended(down)[0].messages[0]["content"]
            == f"{REPLAY_PREFIX}wait stop calling me"
        )

    async def test_does_not_buffer_during_shutdown(self):
        state = _MuteState(reason=MuteReason.SHUTDOWN)
        processor = _MuteStateProcessor(state)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("goodbye"),
                BotStoppedSpeakingFrame(),
            ),
        )

        # Forwarded as usual — the aggregator will drop it, and there is
        # nothing left to replay into.
        assert [f.text for f in _transcriptions(down)] == ["goodbye"]
        assert _appended(down) == []

    async def test_replay_rides_a_pending_transition_generation(self):
        state = _MuteState(reason=MuteReason.QUEUED_SPEECH)
        state.transition_in_progress = True
        processor = _MuteStateProcessor(state)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("actually no"),
                BotStoppedSpeakingFrame(),
            ),
        )

        appended = _appended(down)
        assert len(appended) == 1
        assert appended[0].run_llm is False

    async def test_keeps_the_tail_when_over_the_char_cap(self):
        state = _MuteState()
        processor = _MuteStateProcessor(state, max_buffered_chars=10)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("abcdefghijklmnop"),
                BotStoppedSpeakingFrame(),
            ),
        )

        assert _appended(down)[0].messages[0]["content"] == f"{REPLAY_PREFIX}ghijklmnop"

    async def test_drops_stale_buffered_speech(self):
        state = _MuteState()
        processor = _MuteStateProcessor(state, max_buffer_age_seconds=0.0)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("ancient history"),
                BotStoppedSpeakingFrame(),
            ),
        )

        assert _appended(down) == []

    async def test_discards_buffer_on_terminal_frame(self):
        state = _MuteState()
        processor = _MuteStateProcessor(state)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("too late"),
                CancelFrame(),
            ),
            send_end_frame=False,
        )

        assert _appended(down) == []

    @pytest.mark.parametrize("text", ["", "   "])
    async def test_ignores_empty_transcriptions(self, text):
        state = _MuteState()
        processor = _MuteStateProcessor(state)

        down, _ = await run_test(
            processor,
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription(text),
                BotStoppedSpeakingFrame(),
            ),
        )

        assert _appended(down) == []


class _Generations:
    def __init__(self):
        self.count = 0

    def __call__(self) -> int:
        return self.count


def _forced_runs(frames) -> list[LLMMessagesAppendFrame]:
    return [f for f in _appended(frames) if not f.messages and f.run_llm]


class TestReplayGenerationSafetyNet:
    """A replay that never leads to a generation must not leave the caller in silence."""

    def _processor(self, state, generations, timeout=0.3):
        return _MuteStateProcessor(
            state,
            generation_count=generations,
            replay_generation_timeout_seconds=timeout,
        )

    async def test_forces_run_when_no_generation_starts(self):
        state, generations = _MuteState(), _Generations()
        down, _ = await run_test(
            self._processor(state, generations),
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("Hello?"),
                BotStoppedSpeakingFrame(),
                SleepFrame(0.6),
            ),
        )

        assert len(_forced_runs(down)) == 1
        # The original replay is still there, ahead of the forced run.
        assert len(_appended(down)) == 2

    async def test_no_forced_run_when_generation_starts_in_time(self):
        import asyncio

        state, generations = _MuteState(), _Generations()

        class _StartsGeneration(_MuteStateProcessor):
            async def process_frame(self, frame, direction):
                if isinstance(frame, BotStoppedSpeakingFrame):
                    # The engine starts a generation 0.1 s after the unmute.
                    asyncio.get_running_loop().call_later(0.1, self._bump)
                await super().process_frame(frame, direction)

            def _bump(self):
                generations.count += 1

        down, _ = await run_test(
            _StartsGeneration(
                state,
                generation_count=generations,
                replay_generation_timeout_seconds=0.3,
            ),
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("Hello?"),
                BotStoppedSpeakingFrame(),
                SleepFrame(0.6),
            ),
        )

        assert len(_appended(down)) == 1
        assert _forced_runs(down) == []

    async def test_end_frame_cancels_pending_check(self):
        state, generations = _MuteState(), _Generations()
        down, _ = await run_test(
            self._processor(state, generations, timeout=0.5),
            frames_to_send=_in_order(
                BotStartedSpeakingFrame(),
                _transcription("Hello?"),
                BotStoppedSpeakingFrame(),
            ),
        )

        # run_test ends the pipeline right away, well before the 0.5 s check.
        assert _forced_runs(down) == []
