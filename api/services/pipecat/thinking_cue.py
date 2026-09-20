"""Speak a short holding phrase when a tool call outruns its deadline.

A tool call is the longest silence in a voice conversation that isn't the
caller's own thinking: a CRM lookup or availability check takes a second or
two, and for that whole time the line is dead. Callers read dead air as a
dropped call and start saying "hello? hello?".

Tools can already be configured with a message spoken *before* they run, but
that fires unconditionally — the agent announces "one moment" and then answers
instantly, which reads as stalling, while an unconfigured tool still produces
silence. This fires only once a call has actually taken too long, and it wraps
every tool rather than only the configured ones.
"""

import asyncio
import random
from typing import Callable, Optional, Sequence

from loguru import logger

from pipecat.frames.frames import TTSSpeakFrame


class ThinkingCue:
    """Async context manager that speaks a filler if the body runs long.

    Usage::

        async with ThinkingCue(queue_frame=task.queue_frame, phrases=[...]):
            result = await slow_operation()

    The cue task is cancelled on exit, so a fast operation says nothing.
    There is a small unavoidable race — an operation that finishes in the
    moment between the deadline elapsing and the frame being queued will still
    hear the filler — bounded by how long queueing takes.
    """

    def __init__(
        self,
        *,
        queue_frame,
        phrases: Sequence[str],
        delay_seconds: float,
        enabled: bool = True,
        last_phrase: Optional[str] = None,
        on_spoken: Optional[Callable[[str], None]] = None,
    ):
        self._queue_frame = queue_frame
        self._phrases = [p for p in (phrases or []) if p and p.strip()]
        self._delay_seconds = delay_seconds
        self._enabled = enabled and bool(self._phrases) and queue_frame is not None
        self._last_phrase = last_phrase
        self._on_spoken = on_spoken
        self._task: Optional[asyncio.Task] = None
        self.spoken: Optional[str] = None

    def _choose(self) -> str:
        """Pick a phrase, avoiding an immediate repeat of the previous one."""
        candidates = [p for p in self._phrases if p != self._last_phrase]
        return random.choice(candidates or self._phrases)

    async def _speak_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._delay_seconds)
            phrase = self._choose()
            self.spoken = phrase
            if self._on_spoken is not None:
                self._on_spoken(phrase)
            logger.debug(f"Tool call outran {self._delay_seconds}s, speaking: {phrase}")
            # append_to_context=False: a filler is conversational padding, not
            # content the model should reason over on the next turn.
            await self._queue_frame(
                TTSSpeakFrame(phrase, append_to_context=False, persist_to_logs=True)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Could not speak thinking cue: {e}")

    async def __aenter__(self) -> "ThinkingCue":
        if self._enabled:
            self._task = asyncio.create_task(self._speak_after_delay())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        return False
