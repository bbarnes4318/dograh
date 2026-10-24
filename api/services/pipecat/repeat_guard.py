"""Stop the agent saying the same thing twice on one call.

Models loop: they re-ask a question the caller already answered, restate an
explanation word for word, or emit the same sentence twice in one turn. Prompt
rules reduce it but never remove it, and every repeat tells the caller they
are talking to automation.

:class:`RepeatGuard` sits directly after the LLM, ahead of TTS, and holds each
generation's text one sentence at a time (TTS aggregates by sentence anyway,
so this adds no audible latency). A sentence the agent has already said on
this call — exactly, or near enough after dropping leading acknowledgments
like "okay" or "so" — is not spoken. The one exception is a caller who asks to
hear it again ("what?", "say that again"): then a sentence may be said one
more time, never more.

What the agent "has already said" comes from the assistant messages in the
LLM context, which hold only what was actually spoken (an interrupted line is
truncated there), plus engine-spoken lines that bypass the context such as
transition speech.

When dropping leaves the caller with nothing to answer — the whole generation
was a repeat, or the question it ended on was — the guard appends a note
telling the model what it was about to repeat and asks it once to say
something new instead.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from loguru import logger

from api.services.pipecat.call_hygiene import matches_closing_line, normalize
from api.services.pipecat.muted_speech_buffer import REPLAY_PREFIX
from api.services.workflow.pipecat_engine_context_composer import (
    RECORDING_MARKER,
    TTS_MARKER,
)
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

if TYPE_CHECKING:
    from api.services.workflow.pipecat_engine import PipecatEngine


# A sentence this short ("Okay.", "Got it.", "Take care.") is conversational
# glue, not content; repeating it is normal speech.
MIN_GUARDED_WORDS = 3
# Near-duplicates ("Are you between forty and eighty five?" vs "So are you
# between forty and eighty-five, then?") need enough words for a similarity
# score to mean anything.
MIN_FUZZY_WORDS = 5
FUZZY_MATCH_RATIO = 0.88
# Regenerations the guard may ask for over a whole call, as a cost ceiling.
MAX_REGENERATIONS_PER_CALL = 6

# Leading words that don't change what a sentence says.
_LEADING_FILLER_RE = re.compile(
    r"^((okay|ok|alright|all right|so|and|but|well|great|sure|gotcha|got it"
    r"|sorry|um|uh|oh|right|yes|yeah|now|just|perfect|then)\s+)+"
)
# A sentence ends at . ? or ! followed by whitespace.
_SENTENCE_END_RE = re.compile(r"(?<=[.?!])\s+")

# Caller asking to hear the last thing again. Matched against the normalized
# text of the caller's latest message.
REPEAT_REQUEST_PATTERN = re.compile(
    r"^(what|huh|hm+|sorry|pardon|excuse me|come again|hello|say what|what now)$"
    r"|\b(say (that|it|this) again|repeat (that|it|this|the question|yourself)"
    r"|(didnt|did not|couldnt|could not|cant|can not|cannot) (hear|understand|catch|get) "
    r"(that|you|what you said|the question|it)?"
    r"|what (was|did) (that|you say|you said)|what did you (just )?say"
    r"|one more time|say again|i missed (that|it|what you said)"
    r"|(you|youre) (broke|breaking|cutting) (up|out)|bad (connection|line)"
    r"|pardon me|come again)\b"
)

REGENERATE_INSTRUCTION = (
    "[Note: you were about to say this again, but the caller already heard it "
    'on this call: "{repeated}". Do not say it again, not even reworded. If the '
    "caller has already answered that question, use their answer and move to "
    "the next step. Otherwise respond to what the caller said last with "
    "something new — one short clarifying question, the next step, or a polite "
    "close if the conversation is finished.]"
)


def sentence_key(text: str) -> str:
    """Comparison key for one sentence: normalized, leading filler stripped."""
    norm = normalize(text)
    return _LEADING_FILLER_RE.sub("", norm).strip()


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping each sentence's trailing whitespace."""
    parts: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        parts.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        parts.append(text[start:])
    return parts


def is_repeat_request(caller_text: str) -> bool:
    """Whether the caller is asking to hear the last thing again."""
    norm = normalize(caller_text)
    return bool(norm) and REPEAT_REQUEST_PATTERN.search(norm) is not None


_CALLER_QUESTION_START_RE = re.compile(
    r"^(who|what|whats|why|how|where|when|which|is|are|do|does|did|can|could"
    r"|would|will|wait)\b"
)


def is_caller_question(caller_text: str) -> bool:
    """Whether the caller answered with a question of their own.

    A question the caller has not answered yet may then be asked once more
    ("Who is this?" -> "It's Alex with Family Life. Do you usually handle
    your own insurance decisions?").
    """
    if (caller_text or "").rstrip().endswith("?"):
        return True
    return _CALLER_QUESTION_START_RE.match(normalize(caller_text)) is not None


def allows_one_more_time(caller_text: str) -> bool:
    """Whether the caller's last turn licenses saying something once more."""
    return is_repeat_request(caller_text) or is_caller_question(caller_text)


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


class RepeatGuard(FrameProcessor):
    """Drop sentences the agent already said on this call.

    Args:
        engine: The call's PipecatEngine; its LLM context is the record of what
            the agent has said and what the caller said last.
        inject_context_frame: Async callable delivering an
            ``LLMMessagesAppendFrame`` to the user aggregator, used to ask for
            a regeneration. Without it the guard only drops.
    """

    def __init__(
        self,
        engine: PipecatEngine,
        *,
        inject_context_frame: Callable[[Frame], Awaitable[None]] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._engine = engine
        self._inject_context_frame = inject_context_frame

        # How many times each sentence key has been said on this call. Kept
        # across generations so context compaction can't make the agent
        # forget it already said something.
        self._said_counts: dict[str, int] = {}

        self._in_generation = False
        self._passthrough = False
        self._pending = ""
        self._generation_counts: dict[str, int] = {}
        self._repeat_allowed = False
        self._dropped: list[str] = []
        self._dropped_question = False
        self._forwarded_text = False
        self._forwarded_question = False
        self._function_call_started = False

        # Sentences from finished generations that may still be playing. They
        # count as said right away (the context only gets them once spoken),
        # and are taken back if the caller cuts the bot off before it is done.
        self._unconfirmed_counts: dict[str, int] = {}

        self._regenerations = 0
        # User-message count right after the last regeneration note, so one
        # caller turn gets at most one regeneration.
        self._regenerated_at_user_count: int | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStoppedSpeakingFrame):
            # Everything queued so far has been heard in full.
            self._unconfirmed_counts = {}
            await self.push_frame(frame, direction)
            return

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._start_generation()
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMTextFrame) and self._in_generation:
            await self._handle_text(frame)
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._function_call_started = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMFullResponseEndFrame) and self._in_generation:
            await self._flush(final=True)
            await self.push_frame(frame, direction)
            await self._end_generation()
        elif isinstance(frame, TTSSpeakFrame):
            # Engine-spoken lines (transition speech, nudges, tool messages)
            # always play, but they count as said.
            self._record_said(frame.text)
            await self.push_frame(frame, direction)
        elif isinstance(frame, InterruptionFrame):
            # Whatever was held or still queued is not going to be heard; the
            # context keeps the part that was.
            self._reset_generation()
            for key, count in self._unconfirmed_counts.items():
                self._said_counts[key] = max(0, self._said_counts.get(key, 0) - count)
            self._unconfirmed_counts = {}
            await self.push_frame(frame, direction)
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._reset_generation()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Generation lifecycle
    # ------------------------------------------------------------------

    def _start_generation(self) -> None:
        # FunctionCallsStartedFrame is a system frame and can overtake this
        # response's start frame, so a tool call seen already belongs to it.
        function_call_started = self._function_call_started
        self._reset_generation()
        self._function_call_started = function_call_started
        self._in_generation = True
        self._sync_said_from_context()
        self._repeat_allowed = allows_one_more_time(self._last_caller_text())

    def _reset_generation(self) -> None:
        self._in_generation = False
        self._passthrough = False
        self._pending = ""
        self._generation_counts = {}
        self._repeat_allowed = False
        self._dropped = []
        self._dropped_question = False
        self._forwarded_text = False
        self._forwarded_question = False
        self._function_call_started = False

    async def _handle_text(self, frame: LLMTextFrame) -> None:
        if self._passthrough:
            await self.push_frame(frame)
            return
        if RECORDING_MARKER in (frame.text or "") or RECORDING_MARKER in self._pending:
            # A pre-recorded clip id: the recording router owns this text.
            self._passthrough = True
            pending, self._pending = self._pending, ""
            if pending:
                await self.push_frame(LLMTextFrame(pending))
            await self.push_frame(frame)
            return
        self._pending += frame.text or ""
        await self._flush(final=False)

    async def _flush(self, *, final: bool) -> None:
        if not self._pending:
            return
        sentences = split_sentences(self._pending)
        # The last piece is still being written unless it ended a sentence.
        if not final and sentences and not sentences[-1][-1:].isspace():
            self._pending = sentences.pop()
        else:
            self._pending = ""
        for sentence in sentences:
            await self._emit_sentence(sentence)

    async def _emit_sentence(self, sentence: str) -> None:
        key = sentence_key(sentence)
        if self._is_repeat(sentence, key):
            logger.info(f"Repeat guard: not repeating {sentence.strip()!r}")
            self._dropped.append(sentence.strip())
            if sentence.rstrip().endswith("?"):
                self._dropped_question = True
            # Keep a leading speech-mode marker so the recording router
            # still sees this response's mode.
            if sentence.lstrip().startswith(TTS_MARKER):
                await self.push_frame(LLMTextFrame(f"{TTS_MARKER} "))
            return

        if key:
            self._generation_counts[key] = self._generation_counts.get(key, 0) + 1
            if sentence.rstrip().endswith("?"):
                self._forwarded_question = True
        # "Okay." alone doesn't move the call forward; only content counts.
        if len(key.split()) >= MIN_GUARDED_WORDS:
            self._forwarded_text = True
        await self.push_frame(LLMTextFrame(sentence))

    async def _end_generation(self) -> None:
        for key, count in self._generation_counts.items():
            if len(key.split()) < MIN_GUARDED_WORDS:
                continue
            self._said_counts[key] = self._said_counts.get(key, 0) + count
            self._unconfirmed_counts[key] = self._unconfirmed_counts.get(key, 0) + count
        dropped = self._dropped
        needs_new_line = (
            bool(dropped)
            and not self._function_call_started
            and (
                not self._forwarded_text
                or (self._dropped_question and not self._forwarded_question)
            )
        )
        self._reset_generation()
        if needs_new_line:
            await self._request_regeneration(dropped)

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def _is_repeat(self, sentence: str, key: str) -> bool:
        words = key.split()
        if len(words) < MIN_GUARDED_WORDS:
            return False
        # A goodbye ends the call; dropping it would leave the call open.
        if matches_closing_line(sentence):
            return False

        if self._generation_counts.get(key, 0):
            # The same sentence twice in one breath is never right.
            return True
        said = self._said_counts.get(key, 0)
        if not said and len(words) >= MIN_FUZZY_WORDS:
            said = max(
                (
                    count
                    for previous, count in self._said_counts.items()
                    if len(previous.split()) >= MIN_FUZZY_WORDS
                    and SequenceMatcher(None, key, previous).ratio()
                    >= FUZZY_MATCH_RATIO
                ),
                default=0,
            )
        if not said:
            return False
        # The caller asked to hear it again, or asked something back instead
        # of answering: once more, never twice.
        return not (self._repeat_allowed and said < 2)

    async def _request_regeneration(self, dropped: list[str]) -> None:
        if self._inject_context_frame is None:
            return
        if self._engine.is_call_disposed():
            return
        if not getattr(self._engine, "conversation_output_enabled", True):
            return
        if self._regenerations >= MAX_REGENERATIONS_PER_CALL:
            logger.warning("Repeat guard: regeneration budget spent for this call")
            return
        user_count = self._user_message_count()
        if self._regenerated_at_user_count == user_count:
            # Already asked once since the caller last spoke; asking again
            # would just loop.
            logger.info("Repeat guard: model repeated itself again; staying quiet")
            return

        self._regenerations += 1
        self._regenerated_at_user_count = user_count + 1
        repeated = " ".join(dropped)
        logger.info(f"Repeat guard: asking the model for a new line ({repeated!r})")
        await self._inject_context_frame(
            LLMMessagesAppendFrame(
                [
                    {
                        "role": "user",
                        "content": REGENERATE_INSTRUCTION.format(repeated=repeated),
                    }
                ],
                run_llm=True,
            )
        )

    # ------------------------------------------------------------------
    # What has been said
    # ------------------------------------------------------------------

    def _record_said(self, text: str) -> None:
        for sentence in split_sentences(text or ""):
            key = sentence_key(sentence)
            if len(key.split()) >= MIN_GUARDED_WORDS:
                self._said_counts[key] = self._said_counts.get(key, 0) + 1

    def _messages(self) -> list:
        context = getattr(self._engine, "context", None)
        if context is None:
            return []
        try:
            return list(context.get_messages())
        except Exception as exc:  # never let bookkeeping break the pipeline
            logger.warning(f"Repeat guard could not read the LLM context: {exc}")
            return []

    def _sync_said_from_context(self) -> None:
        counts: dict[str, int] = {}
        for message in self._messages():
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for sentence in split_sentences(_message_text(message.get("content"))):
                key = sentence_key(sentence)
                if len(key.split()) >= MIN_GUARDED_WORDS:
                    counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            if count > self._said_counts.get(key, 0):
                self._said_counts[key] = count

    def _last_caller_text(self) -> str:
        """The caller's turn this generation answers, or "" if there is none."""
        for message in reversed(self._messages()):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                # The agent already replied to whatever the caller said last.
                return ""
            if role != "user":
                continue
            text = _message_text(message.get("content")).strip()
            text = text.removeprefix(REPLAY_PREFIX.strip())
            return text
        return ""

    def _user_message_count(self) -> int:
        return sum(
            1
            for message in self._messages()
            if isinstance(message, dict) and message.get("role") == "user"
        )
