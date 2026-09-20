"""Callback factory helpers for :pyclass:`~api.services.workflow.pipecat_engine.PipecatEngine`.

Each helper takes a :class:`PipecatEngine` instance and returns an async
callback function suitable for passing to the various pipeline processors.
Separating these helpers into their own module keeps
``pipecat_engine.py`` focused on high-level engine orchestration logic while
encapsulating the callback implementations here for easier maintenance and
unit-testing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from loguru import logger
from pipecat.frames.frames import (
    LLMMessagesAppendFrame,
    TTSSpeakFrame,
    UserIdleTimeoutUpdateFrame,
)
from pipecat.utils.enums import EndTaskReason

from api.schemas.workflow_configurations import (
    IdleNudgeConfiguration,
    default_idle_nudges,
)

if TYPE_CHECKING:
    from api.services.workflow.pipecat_engine import PipecatEngine


# ---------------------------------------------------------------------------
# User-idle handling
# ---------------------------------------------------------------------------


DEFAULT_LLM_IDLE_INSTRUCTION = (
    "The user has been quiet. Politely and briefly ask if they're still there "
    "in the language that the user has been speaking so far."
)


class UserIdleHandler:
    """Escalate through configured nudges when the caller stops responding.

    Each nudge either speaks a canned line straight to TTS — instant, no model
    round trip, which is what you want at the moment attention is slipping — or
    asks the LLM to generate one, which is what a non-English workflow wants so
    the nudge lands in the caller's language. A nudge marked ``end_call`` ends
    the call after speaking.

    Nudges can carry their own ``after_seconds``, pushed to the aggregator's
    idle controller as the previous nudge fires, so the agent can wait longer
    after each unanswered prompt instead of hanging up on a fixed clock.
    """

    def __init__(
        self,
        engine: "PipecatEngine",
        nudges: Optional[list["IdleNudgeConfiguration"]] = None,
        enabled: bool = True,
    ):
        self._engine = engine
        self._enabled = enabled
        self._nudges = list(nudges) if nudges else default_idle_nudges()
        self._retry_count = 0

    def reset(self):
        """Reset the retry count when user becomes active."""
        self._retry_count = 0

    async def handle_idle(self, aggregator):
        """Handle a user-idle event by firing the next nudge in the ladder."""
        if not self._enabled:
            return

        index = self._retry_count
        self._retry_count += 1
        logger.debug(f"Handling user_idle, attempt: {self._retry_count}")

        if index >= len(self._nudges):
            # Ran past the configured ladder — treat it as the end.
            await self._engine.end_call_with_reason(
                EndTaskReason.USER_IDLE_MAX_DURATION_EXCEEDED.value
            )
            return

        nudge = self._nudges[index]

        if nudge.message:
            await self._speak_canned(nudge.message)
        else:
            await self._ask_llm(
                aggregator, nudge.llm_instruction or DEFAULT_LLM_IDLE_INSTRUCTION
            )

        if nudge.end_call:
            await self._engine.end_call_with_reason(
                EndTaskReason.USER_IDLE_MAX_DURATION_EXCEEDED.value
            )
            return

        await self._arm_next_timeout(index + 1)

    async def _speak_canned(self, message: str) -> None:
        """Speak a fixed line without going through the LLM."""
        if self._engine.task is None:
            logger.warning("No pipeline task available to speak idle nudge")
            return
        # append_to_context so the model knows it already prompted the caller
        # and doesn't repeat the question on its next turn.
        await self._engine.task.queue_frame(
            TTSSpeakFrame(message, append_to_context=True, persist_to_logs=True)
        )

    async def _ask_llm(self, aggregator, instruction: str) -> None:
        await aggregator.push_frame(
            LLMMessagesAppendFrame(
                [{"role": "user", "content": instruction}], run_llm=True
            )
        )

    async def _arm_next_timeout(self, next_index: int) -> None:
        """Apply the next nudge's own idle timeout, if it sets one."""
        if next_index >= len(self._nudges):
            return
        after_seconds = self._nudges[next_index].after_seconds
        if not after_seconds or self._engine.task is None:
            return
        await self._engine.task.queue_frame(
            UserIdleTimeoutUpdateFrame(timeout=after_seconds)
        )


def create_user_idle_handler(
    engine: "PipecatEngine",
    nudges: Optional[list["IdleNudgeConfiguration"]] = None,
    enabled: bool = True,
) -> UserIdleHandler:
    """Return a UserIdleHandler that manages user-idle timeouts with state."""
    return UserIdleHandler(engine, nudges=nudges, enabled=enabled)


# ---------------------------------------------------------------------------
# Max-duration handling
# ---------------------------------------------------------------------------


def create_max_duration_callback(engine: "PipecatEngine"):
    """Return a callback that cancels the task when the hard call limit is exceeded."""

    async def handle_max_duration():
        logger.debug("Max call duration exceeded. Terminating call")
        await engine.end_call_with_reason(
            EndTaskReason.CALL_DURATION_EXCEEDED.value,
            abort_immediately=True,
        )

    return handle_max_duration


# ---------------------------------------------------------------------------
# Generation-started handling
# ---------------------------------------------------------------------------


def create_generation_started_callback(engine: "PipecatEngine"):
    """Return a callback that resets flags at the start of each LLM generation."""

    async def handle_generation_started():
        logger.debug("LLM generation started in callback processor")
        # Clear reference text from previous generation
        engine._current_llm_generation_reference_text = ""
        # A generation is now under way, so a pending node transition is no
        # longer "about to queue one".
        engine._transition_in_progress = False

    return handle_generation_started


def create_aggregation_correction_callback(engine: "PipecatEngine"):
    """Create a callback that uses engine's reference text to correct corrupted aggregation."""

    def correct_corrupted_aggregation(ref: str, corrupted: str) -> str:
        """Correct corrupted text by aligning it with reference text.

        This is a pure function that doesn't depend on engine instance.
        """
        # 1) Safety check: if ref (minus spaces) is shorter than corrupted, bail out
        # also if corrupted is less than 10 characters, lets also return that since most likely
        # Elevenlabs returned the right alignment
        alnum_corr = "".join(ch for ch in corrupted if ch.isalnum())
        alnum_ref = "".join(ch for ch in ref if ch.isalnum())

        if corrupted in ref or len(alnum_ref) < len(alnum_corr) or len(alnum_corr) < 10:
            return corrupted

        logger.debug(
            f"In correct_corrupted_aggregation: ref: {ref} corrupted: {corrupted}"
        )

        # 2) Find where in `ref` we should start aligning.
        #    We take the first N (N=10) characters of `corrupted`
        #    and look for all their occurrences in `ref`.
        #    We pick the *last* one
        prefix = corrupted[:10]

        # find all start‐indices of that prefix in ref
        starts = [m.start() for m in re.finditer(re.escape(prefix), ref)]
        start_idx = starts[-1] if starts else 0

        # 3) Now run the same two‑pointer scan from start_idx
        i, j = start_idx, 0
        out_chars = []
        while i < len(ref) and j < len(corrupted):
            r_ch, c_ch = ref[i], corrupted[j]
            if r_ch == c_ch:
                out_chars.append(r_ch)
                i += 1
                j += 1

            elif c_ch == " ":
                # extra space in corrupted → skip it
                j += 1

            elif r_ch == " " or r_ch in ".,;:!?":
                # missing structural char in corrupted → emit from ref
                out_chars.append(r_ch)
                i += 1

            else:
                # letter mismatch → best‑effort copy from ref
                out_chars.append(r_ch)
                i += 1
                j += 1

        # 4) A final check - the final created output should be exactly
        # as corrupted sentence sans whitespace.
        alnum_out = "".join([ch for ch in out_chars if ch.isalnum()])
        if alnum_out != alnum_corr:
            return corrupted

        # 5) Join and return exactly what we built
        return "".join(out_chars)

    def correct_aggregation(corrupted: str) -> str:
        reference = engine._current_llm_generation_reference_text

        if not reference:
            return corrupted

        # Apply the correction algorithm
        corrected = correct_corrupted_aggregation(reference, corrupted)
        return corrected

    return correct_aggregation
