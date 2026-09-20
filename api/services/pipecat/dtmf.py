"""DTMF: pressing keys on an IVR, and capturing keys the caller presses.

Two capabilities the platform had neither half of. ``ToolCategory.NATIVE`` even
carried the note "future: dtmf_input".

**Sending** matters on outbound calls to businesses: an agent that can't press
"2 for sales" never reaches a human, and the call is wasted. The agent gets a
``send_dtmf`` function it can call with a digit string.

**Receiving** matters for anything the caller shouldn't say out loud — a card
number, an SSN, a date of birth — and for callers who would rather press a key
than talk. Digits are collected, terminated by ``#`` or a short inter-digit
pause, then handed to the model as a message and recorded on the call's
gathered context.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputDTMFFrame,
    LLMMessagesAppendFrame,
    OutputDTMFFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Seconds of quiet after a keypress before the entry is treated as finished.
DEFAULT_INTERDIGIT_TIMEOUT_SECONDS = 2.5

# Guard against a stuck key or a noisy line filling the context.
MAX_COLLECTED_DIGITS = 32

# Where captured entries land on the call's gathered context.
DTMF_CONTEXT_KEY = "dtmf_entries"

_VALID_DTMF = re.compile(r"[0-9*#]")


def parse_dtmf_digits(
    value: Any, max_digits: int = MAX_COLLECTED_DIGITS
) -> list[KeypadEntry]:
    """Turn a caller-supplied string into keypad entries.

    Everything that isn't a dialable character is dropped rather than
    rejected — models like to write "1-800" or "press 2".
    """
    if not isinstance(value, str):
        return []
    entries: list[KeypadEntry] = []
    for char in _VALID_DTMF.findall(value):
        try:
            entries.append(KeypadEntry(char))
        except ValueError:  # pragma: no cover - regex already constrains this
            continue
        if len(entries) >= max_digits:
            break
    return entries


class DTMFCaptureProcessor(FrameProcessor):
    """Collect caller keypresses and hand them to the model as a turn.

    Sits just below the transport input so it sees ``InputDTMFFrame`` before
    anything else. An entry ends on ``#`` or after ``interdigit_timeout``
    seconds of quiet, whichever comes first.

    Args:
        on_entry: Async callback given the completed digit string — used to
            record it on the call's gathered context.
        interdigit_timeout: Seconds of quiet that end an entry.
        max_digits: Hard cap per entry.
    """

    def __init__(
        self,
        *,
        on_entry: Optional[Callable[[str], Awaitable[None]]] = None,
        interdigit_timeout: float = DEFAULT_INTERDIGIT_TIMEOUT_SECONDS,
        max_digits: int = MAX_COLLECTED_DIGITS,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._on_entry = on_entry
        self._interdigit_timeout = interdigit_timeout
        self._max_digits = max_digits
        self._digits: list[str] = []
        self._flush_task: Optional[asyncio.Task] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._cancel_flush()
            self._digits.clear()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InputDTMFFrame):
            await self._collect(frame.button)
            # Swallowed: downstream sees the finished entry as a message, not
            # a stream of single keypresses.
            return

        await self.push_frame(frame, direction)

    async def _collect(self, button: KeypadEntry) -> None:
        await self._cancel_flush()
        digit = button.value

        if digit == "#":
            await self._flush()
            return

        self._digits.append(digit)
        logger.debug(f"Collected DTMF digit {digit!r} ({len(self._digits)} so far)")

        if len(self._digits) >= self._max_digits:
            await self._flush()
            return

        self._flush_task = self.create_task(self._flush_after_timeout())

    async def _flush_after_timeout(self) -> None:
        await asyncio.sleep(self._interdigit_timeout)
        await self._flush()

    async def _cancel_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            await self.cancel_task(self._flush_task)
        self._flush_task = None

    async def _flush(self) -> None:
        if not self._digits:
            return
        entry = "".join(self._digits)
        self._digits.clear()
        logger.info(f"Caller entered DTMF: {entry}")

        await self.push_frame(
            LLMMessagesAppendFrame(
                [
                    {
                        "role": "user",
                        "content": f"[The caller pressed these keys: {entry}]",
                    }
                ],
                run_llm=True,
            ),
            FrameDirection.DOWNSTREAM,
        )

        if self._on_entry is not None:
            try:
                await self._on_entry(entry)
            except Exception as e:
                logger.error(f"DTMF entry callback failed: {e}")


def get_send_dtmf_tool_schema():
    """Function schema for the agent-callable DTMF sender."""
    from api.services.workflow.pipecat_engine_custom_tools import get_function_schema

    return get_function_schema(
        "send_dtmf",
        (
            "Press keys on the phone keypad. Use this to navigate an automated "
            "phone menu — for example pressing 2 to reach a department, or an "
            "extension number. Only digits, * and # can be pressed."
        ),
        properties={
            "digits": {
                "type": "string",
                "description": (
                    "The keys to press, in order, e.g. '2' or '1234#'. Anything "
                    "that is not a digit, * or # is ignored."
                ),
            }
        },
        required=["digits"],
    )


async def send_dtmf_digits(queue_frame, digits: str) -> dict[str, Any]:
    """Queue a DTMF keypress sequence on the transport.

    Returns a result dict for the model so it can tell whether the press
    happened and react verbally if it didn't.
    """
    entries = parse_dtmf_digits(digits)
    if not entries:
        return {
            "status": "error",
            "error": "No dialable keys in that value; use digits, * or #.",
        }
    if queue_frame is None:
        return {"status": "error", "error": "This call cannot send keypresses."}

    await queue_frame(OutputDTMFFrame(buttons=list(entries)))
    pressed = "".join(entry.value for entry in entries)
    logger.info(f"Sent DTMF: {pressed}")
    return {"status": "ok", "pressed": pressed}
