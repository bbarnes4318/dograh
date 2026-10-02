"""Application-owned gate for AI-generated conversational output.

Media playback (recordings, songs) and terminal tool chains
(``play audio -> send SMS -> hang up``) are owned by workflow state, not by the
voice model. While the gate is *suppressed*:

- realtime / GPT-Live services must not forward model audio or text and must
  not start new spoken responses (they text-only their tool-calling turns);
- the chained-pipeline gate processor drops LLM text before it can reach TTS.

Frames the application itself queues (``play_audio`` through
``transport.output().queue_frame``, explicit ``TTSSpeakFrame`` speech actions)
never pass through the gate, so deterministic media and explicit speech
actions keep working. This is enforced in code, not in the prompt.
"""

from typing import Callable

from loguru import logger

from pipecat.frames.frames import Frame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

REASON_TERMINAL_CHAIN = "terminal_chain"


class ConversationOutputGate:
    """Shared on/off state: ``conversation_output_enabled``."""

    def __init__(self) -> None:
        self._enabled = True
        self._reason: str | None = None
        self._suppressed_count = 0
        self._listeners: list[Callable[[bool], None]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def suppressed_count(self) -> int:
        """How many AI output frames/events were dropped while suppressed."""
        return self._suppressed_count

    def suppress(self, reason: str = REASON_TERMINAL_CHAIN) -> None:
        if self._enabled:
            logger.info(f"Conversation output suppressed (reason={reason})")
        self._enabled = False
        self._reason = reason
        self._notify()

    def resume(self) -> None:
        if not self._enabled:
            logger.info("Conversation output resumed")
        self._enabled = True
        self._reason = None
        self._notify()

    def record_suppressed(self, count: int = 1) -> None:
        self._suppressed_count += count

    def add_listener(self, listener: Callable[[bool], None]) -> None:
        """Register a callback invoked with the new ``enabled`` value on changes."""
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self._enabled)
            except Exception as e:  # noqa: BLE001 - a bad listener must not break the gate
                logger.warning(f"Output gate listener failed: {e}")


class ConversationOutputGateProcessor(FrameProcessor):
    """Drops model-generated text bound for TTS while the gate is suppressed.

    Place directly after the LLM in a chained pipeline. Lifecycle frames
    (``LLMFullResponseStart/End``) still pass so aggregators stay balanced.
    """

    def __init__(self, gate: ConversationOutputGate, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and not self._gate.enabled
            and isinstance(frame, LLMTextFrame)
        ):
            self._gate.record_suppressed()
            return
        await self.push_frame(frame, direction)
