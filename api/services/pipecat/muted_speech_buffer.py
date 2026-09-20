"""Keep what the caller says while their audio is muted, and replay it after.

The user context aggregator mutes the caller for several legitimate reasons —
the current node has ``allow_interrupt=False``, a transition line or tool
message is playing, a function call is running, the first bot turn hasn't
finished yet. While muted it *drops* every ``TranscriptionFrame`` (see
``LLMUserAggregator._maybe_mute_frame``), so anything the caller said during
that window is gone: the agent finishes its disclaimer and answers a question
nobody asked, while the caller's "I'm not interested" never reaches the model.

This processor sits between STT and the user aggregator. Frames travel:

    STT -> MutedSpeechBufferProcessor -> user aggregator      (downstream)
    user aggregator -> MutedSpeechBufferProcessor -> STT      (upstream)

so bot-speaking frames update the aggregator's (and the engine's) mute state
*before* they reach us, and transcriptions reach us *before* the aggregator
would drop them. While the caller is muted we swallow their transcriptions and
hold the text; once they're unmuted we append it to the LLM context as a user
message so the agent actually answers it.

Replay appends the message rather than re-emitting speech frames: the
aggregator's turn strategies own turn boundaries, and synthesising a turn from
outside would fight them.
"""

import time
from typing import Awaitable, Callable, Optional

from loguru import logger

from api.enums import MuteReason
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# How much muted speech to keep. A caller talking over a long disclaimer can
# produce a lot of text; only the tail of it is worth replaying.
DEFAULT_MAX_BUFFERED_CHARS = 600

# Muted speech older than this is stale — the conversation has moved on and
# replaying it would confuse more than it helps.
DEFAULT_MAX_BUFFER_AGE_SECONDS = 45.0

# Prefix that tells the model this text arrived while it was talking, so it can
# react to it rather than treat it as a fresh, well-timed turn.
REPLAY_PREFIX = "[The caller said this while you were still speaking] "


class MutedSpeechBufferProcessor(FrameProcessor):
    """Buffer caller transcriptions produced while muted and replay them later.

    Args:
        is_muted: Returns whether the user aggregator is currently muting the
            caller. Read straight off the aggregator so every mute strategy is
            covered, not just the engine's own callback.
        mute_reason: Returns the engine's reason for the current mute (see
            :class:`api.enums.MuteReason`), or ``None``. Used only to skip
            buffering during call teardown.
        suppress_generation: Returns True when a generation is already on its
            way (a node transition queues one), so the replay rides it instead
            of asking for a second one.
        on_replay: Optional async callback invoked with the replayed text, so
            callers can record it in the transcript the aggregator never saw.
        max_buffered_chars: Cap on retained text.
        max_buffer_age_seconds: Drop buffered text older than this.
    """

    def __init__(
        self,
        *,
        is_muted: Callable[[], bool],
        mute_reason: Callable[[], Optional[str]],
        suppress_generation: Optional[Callable[[], bool]] = None,
        on_replay: Optional[Callable[[str], Awaitable[None]]] = None,
        max_buffered_chars: int = DEFAULT_MAX_BUFFERED_CHARS,
        max_buffer_age_seconds: float = DEFAULT_MAX_BUFFER_AGE_SECONDS,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._is_muted = is_muted
        self._mute_reason = mute_reason
        self._suppress_generation = suppress_generation
        self._on_replay = on_replay
        self._max_buffered_chars = max_buffered_chars
        self._max_buffer_age_seconds = max_buffer_age_seconds

        self._buffered: list[str] = []
        self._buffered_at: Optional[float] = None
        self._ended = False

    @property
    def buffered_text(self) -> str:
        """The text currently held, joined and trimmed to the cap."""
        text = " ".join(part for part in self._buffered if part).strip()
        if len(text) > self._max_buffered_chars:
            # Keep the tail: the last thing said is the most actionable.
            text = text[-self._max_buffered_chars :].lstrip()
        return text

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            self._ended = True
            self._discard()
            await self.push_frame(frame, direction)
            return

        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TranscriptionFrame)
            and self._should_buffer()
        ):
            self._capture(frame)
            # Swallow it: the aggregator would drop it anyway, and letting it
            # through would only log a suppressed frame.
            return

        await self.push_frame(frame, direction)
        await self._maybe_replay()

    def _should_buffer(self) -> bool:
        if self._ended:
            return False
        try:
            if not self._is_muted():
                return False
            # Teardown: the call is ending, nothing to replay into.
            return self._mute_reason() != MuteReason.SHUTDOWN
        except Exception as exc:  # never let bookkeeping break the pipeline
            logger.warning(f"Muted-speech buffer could not read mute state: {exc}")
            return False

    def _capture(self, frame: TranscriptionFrame) -> None:
        text = (frame.text or "").strip()
        if not text:
            return
        now = time.monotonic()
        if self._buffered_at is None:
            self._buffered_at = now
        self._buffered.append(text)
        logger.debug(f"Buffered muted caller speech: {text!r}")

    def _discard(self) -> None:
        self._buffered.clear()
        self._buffered_at = None

    def _is_stale(self) -> bool:
        return (
            self._buffered_at is not None
            and (time.monotonic() - self._buffered_at) > self._max_buffer_age_seconds
        )

    async def _maybe_replay(self) -> None:
        if self._ended or not self._buffered:
            return
        try:
            if self._is_muted():
                return
        except Exception as exc:
            logger.warning(f"Muted-speech buffer could not read mute state: {exc}")
            return

        if self._is_stale():
            logger.debug("Dropping stale muted caller speech")
            self._discard()
            return

        text = self.buffered_text
        self._discard()
        if not text:
            return

        run_llm = True
        if self._suppress_generation is not None:
            try:
                run_llm = not self._suppress_generation()
            except Exception as exc:
                logger.warning(f"Muted-speech buffer could not read engine state: {exc}")

        logger.info(f"Replaying muted caller speech (run_llm={run_llm}): {text!r}")
        await self.push_frame(
            LLMMessagesAppendFrame(
                [{"role": "user", "content": f"{REPLAY_PREFIX}{text}"}],
                run_llm=run_llm,
            ),
            FrameDirection.DOWNSTREAM,
        )

        if self._on_replay is not None:
            try:
                await self._on_replay(text)
            except Exception as exc:
                logger.error(f"Muted-speech replay callback failed: {exc}")
