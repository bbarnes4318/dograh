"""Guards that stop the agent sitting on calls nobody is going to answer.

Voicemail greetings, post-recording carrier menus, call screeners and
answering bots all arrive as ordinary caller speech, so the idle ladder never
ends them and the LLM voicemail detector — which classifies only the first
caller turn — locks a screener-then-voicemail call as a conversation. On the
assistant side, the model sometimes says goodbye (or writes ``end_call`` as
text) without ever calling the end_call tool, and leaks raw markup that TTS
reads aloud.

Two processors live here, both non-realtime only and both running alongside
the existing VoicemailDetector rather than replacing it:

- :class:`MachineAnswerGuard` sits directly after STT and classifies each
  caller utterance with keyword patterns validated against production
  transcripts: machine (end now), call screener (answer once, wait for a
  pickup) or answering bot.
- :class:`AssistantTurnGuard` sits directly after the LLM and hangs up when
  a generation plainly ends the call but no end_call tool ran.

Every machine/voicemail end keeps ``call_disposition == "voicemail_detected"``:
campaign redial and QA skipping key off that exact string.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

from loguru import logger

from api.schemas.workflow_configurations import CallHygieneConfiguration
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallsStartedFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.enums import EndTaskReason
from pipecat.utils.text.xml_function_tag_filter import XMLFunctionTagFilter

if TYPE_CHECKING:
    from api.services.workflow.pipecat_engine import PipecatEngine


# ---------------------------------------------------------------------------
# Patterns. Validated against the 9/25 production transcripts — change them
# only with new transcript evidence.
# ---------------------------------------------------------------------------

_D = r"(zero|one|two|three|four|five|six|seven|eight|nine)"
MACHINE_TERMINAL_PATTERNS = [
    r"\bforwarded to (an )?(automated|automatic) voice",
    r"\b(forwarded|transferred) to (a )?voice ?mail\b",
    r"\bvoice messag(e|ing) system\b",
    r"\bat the tone\b",
    r"\bafter the (tone|beep)\b",
    r"\b(record|leave) (your|a) (message|name)\b",
    r"\bplease leave (your|a|me)\b",
    r"\bleave me (your|a)\b",
    r"\bleave (your|a) (phone number|number|brief message|detailed message)\b",
    r"\bmailbox\b",
    r"\bvoice ?mail\b",
    r"\b(cant|cannot|is unable to|unable to) take your call\b",
    r"\b(cant|cannot) (get to|come to|answer) the phone\b",
    r"\bnot able to answer the phone\b",
    r"\bre ?record\b",
    r"\bmark (your|this) message (urgent|private)\b",
    r"\bsend (your|the) message as is\b",
    r"\bmessage has been (sent|saved|delivered|deleted|cancell?ed)\b",
    r"\bmaximum (time|length) (permitted|allowed)\b",
    r"\bcouldnt hear you please try again\b",
    r"\bsend an sms notification\b",
    r"\bleave a callback number\b",
    r"\bto listen to your message\b",
    r"\bsatisfied with (the|your) message\b",
    r"\bafter you have finished\b",
    r"\bafter leaving a message\b",
    r"\bpress (one|two|three|four|five|six|seven|eight|nine|zero|pound|star|any key)\b",
    r"\b(not|no longer) in service\b",
    r"\bout of service\b",
    r"\bhas been disconnected\b",
    r"\ball circuits are busy\b",
    r"\bis not accepting (calls|messages)\b",
    r"\bcannot be reached\b",
    r"\b(this|the) person (is not|isnt) available\b",
    _D + r"( " + _D + r"){6,} (is )?not available\b",
]
SCREENER_PATTERNS = [
    r"\bplease stay on the line\b",
    r"\bcall assistant\b",
    r"\brecording this call for\b",
    r"\bsay who you are\b",
    r"\bwhy youre (calling|trying to reach)\b",
    r"\b(share|know|get|give me) your name\b",
    r"\bname (and|or) (the )?(reason|purpose|company)\b",
    r"\breason for (your|the) call\b",
    r"\bpurpose of your call\b",
    r"\bscreening service\b",
    r"\bhow do you know\b",
    r"\bhold while i connect you\b",
    r"\bwhat is this regarding\b",
    r"\bcompany youre (calling from|with)\b",
    r"\bget the person youre trying to reach\b",
]
# Only counts as screener while already in screener hold.
SCREENER_HOLD_ONLY_PATTERNS = [r"^(thanks|thank you)$"]
MACHINE_SOFT_PATTERNS = [
    r"\bthe (person|party|subscriber|customer) (youre|you are|you have) (trying to reach|calling|dialed)\b"
]
BOT_FILLER_PATTERN = r"^(go on|ok go on|im listening|uh huh( yep| yeah)?|mm hmm|yep|yeah|right|sure tell me|hold on|sorry what was that|are you still there|is anybody still on the line|is anyone (still )?there)$"
BOT_STILL_THERE_PATTERN = r"\b(are you still there|anybody still on the line|anyone (still )?there|is anybody there)\b"
CLOSING_LINE_PATTERN = r"\b(take care|have a (good|great|nice|wonderful|blessed) (day|one|evening|afternoon|night)|good ?bye|bye now|ill let you go|ill (hang up|end the call|disconnect)|i will (stop|end) the call|(end|stop) the call (here|now)|ill let them (call back|know)|this is a voicemail|(well|i will|ill) (take|remove) you off|wont (get|receive) another call|wont call (you|this number) again)\b"
# Raw (NOT normalized) LLM text, case-insensitive: the model tried to end the
# call in text instead of calling the tool.
END_INTENT_MARKUP_PATTERN = r"end_call|\[\s*end of (conversation|call)\s*\]|the call has been (disconnected|ended)"
# TTS scrub — order matters, applied in sequence, flags re.I | re.S.
LLM_MARKUP_PATTERNS = [
    r"(?<=>)\s*done\s*(?=</)",
    r"<\|[^|<>]{0,40}\|>",
    r"</?\s*(?:think|thinking|tool_call|tool_code|tool_response|function_call|function|p\s*arameter|parameter)\b[^>]*>?",
    r"\b\w*_code>",
    r"\bprint\(.*?\)\)?",
    r"\[\s*end of (?:conversation|call)\s*\]",
    r"\((?:silence|pause|no response|no answer|better|[^)]*warm[^)]*)\)",
    r"\*?\s*\bOption\s+\d+\b[^:]*:.*",
    r"\bthe call has been (?:disconnected|ended)\b[^.]*\.?",
    r"^[\s*_|]+$",
]

_MACHINE_TERMINAL_RE = [re.compile(p) for p in MACHINE_TERMINAL_PATTERNS]
_SCREENER_RE = [re.compile(p) for p in SCREENER_PATTERNS]
_SCREENER_HOLD_ONLY_RE = [re.compile(p) for p in SCREENER_HOLD_ONLY_PATTERNS]
_MACHINE_SOFT_RE = [re.compile(p) for p in MACHINE_SOFT_PATTERNS]
_BOT_FILLER_RE = re.compile(BOT_FILLER_PATTERN)
_BOT_STILL_THERE_RE = re.compile(BOT_STILL_THERE_PATTERN)
_CLOSING_LINE_RE = re.compile(CLOSING_LINE_PATTERN)
_END_INTENT_MARKUP_RE = re.compile(END_INTENT_MARKUP_PATTERN, re.I)
_LLM_MARKUP_RE = [re.compile(p, re.I | re.S) for p in LLM_MARKUP_PATTERNS]
_SENTENCE_SPLIT_RE = re.compile(r"[.?!]+")

SCREENER_LLM_INSTRUCTION = (
    "An automated call-screening assistant answered, not the person. In one "
    "short sentence give only your first name, your company and the reason for "
    "your call, exactly as introduced in your greeting. Ask no questions."
)
SCREENER_PICKUP_INSTRUCTION = (
    "(A call-screening assistant answered first; the person you called just "
    "picked up. Greet them and restate who you are and why you are calling, "
    "then continue.)"
)

# Hangup fires this long after arming even if the bot never reports it
# stopped speaking.
CLOSING_LINE_HARD_FALLBACK_SECONDS = 20.0

UtteranceClass = Literal["machine", "screener", "other"]


def normalize(text: str) -> str:
    """Normalize text before every pattern match."""
    text = (text or "").lower().replace("’", "'").replace("'", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _any(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def is_machine_terminal(text: str) -> bool:
    return _any(_MACHINE_TERMINAL_RE, normalize(text))


def classify_utterance(text: str, in_screener_hold: bool) -> UtteranceClass:
    """Classify one final caller utterance.

    Order is load-bearing: a terminal machine phrase wins over a screener
    phrase in the same utterance ("please stay on the line ... reply after
    the tone" is voicemail), and a screener phrase wins over the soft machine
    phrase ("a call assistant recording this call for the person you're
    trying to reach" is a screener).
    """
    norm = normalize(text)
    if _any(_MACHINE_TERMINAL_RE, norm):
        return "machine"
    if _any(_SCREENER_RE, norm):
        return "screener"
    if in_screener_hold and _any(_SCREENER_HOLD_ONLY_RE, norm):
        return "screener"
    if _any(_MACHINE_SOFT_RE, norm):
        return "machine"
    return "other"


def matches_closing_line(text: str) -> bool:
    return bool(_CLOSING_LINE_RE.search(normalize(text)))


def has_end_intent_markup(raw_text: str) -> bool:
    return bool(_END_INTENT_MARKUP_RE.search(raw_text or ""))


def has_llm_markup(raw_text: str) -> bool:
    return any(p.search(raw_text or "") for p in _LLM_MARKUP_RE)


def build_tts_text_filter() -> XMLFunctionTagFilter:
    """TTS text filter that also scrubs the model markup seen in production."""
    return XMLFunctionTagFilter(custom_patterns=LLM_MARKUP_PATTERNS)


@dataclass
class CallHygieneState:
    """Per-call state shared by the guards, the idle handler and transfers."""

    machine_detected: bool = False
    screener_hold: bool = False
    answering_bot: bool = False
    user_has_spoken: bool = False
    filler_count: int = 0
    still_there_count: int = 0


def _word_count(text: str) -> int:
    return len(normalize(text).split())


def _append_tag(engine: "PipecatEngine", tag: str) -> None:
    call_tags = engine._gathered_context.get("call_tags", [])
    if tag not in call_tags:
        call_tags.append(tag)
    engine._gathered_context["call_tags"] = call_tags


async def _cancel_task(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    if task is asyncio.current_task():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


class MachineAnswerGuard(FrameProcessor):
    """Classify caller transcriptions and end machine-answered calls.

    Sits directly after STT, above the voicemail detector, so it sees every
    transcription the classifier does — and keeps looking after the
    classifier has locked the call as a conversation.

    Args:
        engine: The call's PipecatEngine (state lives on ``engine.call_hygiene``).
        config: The workflow's call hygiene configuration.
        on_dropped_transcription: Optional async callback called with the text
            of every final utterance this guard swallows, so it still reaches
            the transcript (the user aggregator never sees it).
        inject_context_frame: Optional async callable that delivers an
            ``LLMMessagesAppendFrame`` straight to the user aggregator. Without
            it the frame is pushed downstream, which would also run it through
            the voicemail detector's classifier branch when that is enabled.
    """

    def __init__(
        self,
        engine: "PipecatEngine",
        config: CallHygieneConfiguration,
        *,
        on_dropped_transcription: Optional[Callable[[str], Awaitable[None]]] = None,
        inject_context_frame: Optional[Callable[[Frame], Awaitable[None]]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._engine = engine
        self._config = config
        self._on_dropped = on_dropped_transcription
        self._inject_context_frame = inject_context_frame
        self._pickup_timer: Optional[asyncio.Task] = None

    @property
    def state(self) -> CallHygieneState:
        return self._engine.call_hygiene

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            await _cancel_task(self._pickup_timer)
            await self.push_frame(frame, direction)
            return

        if (
            direction != FrameDirection.DOWNSTREAM
            or not isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame))
            or self._engine.is_call_disposed()
        ):
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterimTranscriptionFrame):
            if self._config.machine_keyword_hangup and is_machine_terminal(frame.text):
                await self._machine_detected(frame.text)
                return
            await self.push_frame(frame, direction)
            return

        await self._handle_final(frame, direction)

    async def _handle_final(self, frame: TranscriptionFrame, direction):
        text = frame.text or ""
        state = self.state
        kind = classify_utterance(text, in_screener_hold=state.screener_hold)

        if kind == "machine" and self._config.machine_keyword_hangup:
            await self._machine_detected(text)
            return

        if kind == "screener" and self._config.screener_handling:
            await self._screener_detected(text)
            return

        if kind != "other" or _word_count(text) < 1:
            await self.push_frame(frame, direction)
            return

        if state.screener_hold:
            await self._screener_pickup()
            state.user_has_spoken = True
            await self.push_frame(frame, direction)
            return

        state.user_has_spoken = True

        if self._config.answering_bot_detection and self._looks_like_answering_bot(
            text
        ):
            state.answering_bot = True
            logger.info(f"Call hygiene: answering bot detected ({text!r})")
            await self._log_dropped(text)
            await self._engine.end_call_for_hygiene(
                disposition="answering_bot",
                tag="answering_bot",
                abort_immediately=True,
            )
            return

        await self.push_frame(frame, direction)

    async def _machine_detected(self, text: str) -> None:
        state = self.state
        state.machine_detected = True
        tag = "vm_after_screener" if state.screener_hold else "vm_keyword_guard"
        logger.info(f"Call hygiene: machine answer detected [{tag}] ({text!r})")
        await _cancel_task(self._pickup_timer)
        await self._log_dropped(text)
        await self._engine.end_call_for_hygiene(
            disposition="voicemail_detected", tag=tag, abort_immediately=True
        )

    async def _screener_detected(self, text: str) -> None:
        state = self.state
        await self._log_dropped(text)
        if state.screener_hold:
            logger.debug(f"Call hygiene: screener still talking ({text!r})")
            return

        logger.info(f"Call hygiene: call screener answered ({text!r})")
        state.screener_hold = True
        if self._config.screener_response:
            if self._engine.task is not None:
                await self._engine.task.queue_frame(
                    TTSSpeakFrame(
                        self._config.screener_response,
                        append_to_context=True,
                        persist_to_logs=True,
                    )
                )
        else:
            await self._append_context(SCREENER_LLM_INSTRUCTION, run_llm=True)

        await _cancel_task(self._pickup_timer)
        self._pickup_timer = asyncio.create_task(self._pickup_timeout())

    async def _pickup_timeout(self) -> None:
        await asyncio.sleep(self._config.screener_pickup_timeout_seconds)
        if not self.state.screener_hold or self._engine.is_call_disposed():
            return
        logger.info("Call hygiene: nobody picked up after the call screener")
        await self._engine.end_call_for_hygiene(
            disposition="voicemail_detected",
            tag="call_screener_no_pickup",
            abort_immediately=True,
        )

    async def _screener_pickup(self) -> None:
        logger.info("Call hygiene: person picked up after the call screener")
        self.state.screener_hold = False
        await _cancel_task(self._pickup_timer)
        self._pickup_timer = None
        await self._append_context(SCREENER_PICKUP_INSTRUCTION, run_llm=False)

    async def _append_context(self, content: str, *, run_llm: bool) -> None:
        frame = LLMMessagesAppendFrame(
            [{"role": "user", "content": content}], run_llm=run_llm
        )
        if self._inject_context_frame is not None:
            await self._inject_context_frame(frame)
        else:
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)

    def _looks_like_answering_bot(self, text: str) -> bool:
        state = self.state
        for piece in _SENTENCE_SPLIT_RE.split(text):
            norm = normalize(piece)
            if not norm:
                continue
            if _BOT_FILLER_RE.fullmatch(norm):
                state.filler_count += 1
            if _BOT_STILL_THERE_RE.search(norm):
                state.still_there_count += 1
        return state.filler_count >= 4 and state.still_there_count >= 2

    async def _log_dropped(self, text: str) -> None:
        if self._on_dropped is None or not (text or "").strip():
            return
        try:
            await self._on_dropped(text)
        except Exception as exc:
            logger.error(f"Call hygiene: failed to log dropped transcription: {exc}")

    async def cleanup(self):
        await _cancel_task(self._pickup_timer)
        await super().cleanup()


class AssistantTurnGuard(FrameProcessor):
    """Hang up when the assistant ends the call in words but not with the tool.

    Sits directly after the LLM. Watches each generation's raw text; when it
    is a closing line (or ``end_call`` written as text, or the call is already
    known to be a machine), waits for the bot to finish speaking plus a short
    grace period and ends the call the same way the end_call tool would.
    """

    def __init__(
        self, engine: "PipecatEngine", config: CallHygieneConfiguration, **kwargs
    ):
        super().__init__(**kwargs)
        self._engine = engine
        self._config = config
        self._text_filter = build_tts_text_filter()

        self._in_generation = False
        self._raw_text = ""
        self._function_call_started = False
        self._markup_tagged = False

        self._armed = False
        self._grace_task: Optional[asyncio.Task] = None
        self._fallback_task: Optional[asyncio.Task] = None

    @property
    def is_armed(self) -> bool:
        return self._armed

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._disarm()
        elif direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                await self._disarm()
                self._in_generation = True
                self._raw_text = ""
                self._function_call_started = False
            elif isinstance(frame, LLMTextFrame) and self._in_generation:
                self._raw_text += frame.text or ""
            elif isinstance(frame, FunctionCallsStartedFrame):
                self._function_call_started = True
            elif isinstance(frame, LLMFullResponseEndFrame):
                # Forward first: the evaluation must not hold up TTS.
                await self.push_frame(frame, direction)
                await self._on_generation_end()
                return
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._armed and self._grace_task is None:
                self._grace_task = asyncio.create_task(self._fire_after_grace())

        await self.push_frame(frame, direction)

    async def _on_generation_end(self) -> None:
        raw = self._raw_text
        function_call_started = self._function_call_started
        self._in_generation = False
        self._raw_text = ""
        self._function_call_started = False

        if self._engine.is_call_disposed():
            return

        if not self._markup_tagged and raw.strip() and has_llm_markup(raw):
            self._markup_tagged = True
            _append_tag(self._engine, "llm_markup_leak")

        reason = None
        # A generation that also started a tool call hands control to that
        # tool (end_call disposes the call itself; a transfer must not be cut
        # off by a "have a great day" said while it connects).
        if (
            self._config.closing_line_hangup
            and not function_call_started
            and matches_closing_line(raw)
        ):
            reason = "closing_line"
        elif has_end_intent_markup(raw) and not function_call_started:
            reason = "end_intent_markup"
        elif self._engine.call_hygiene.machine_detected:
            reason = "machine_detected"

        if reason is None:
            return

        scrubbed = await self._text_filter.filter(raw)
        logger.info(
            f"Call hygiene: assistant ended the call in text ({reason}); "
            f"arming hangup ({raw!r})"
        )
        self._armed = True
        self._fallback_task = asyncio.create_task(
            self._fire_after(CLOSING_LINE_HARD_FALLBACK_SECONDS)
        )
        if not scrubbed.strip():
            # Nothing will be spoken, so no BotStoppedSpeakingFrame is coming.
            self._grace_task = asyncio.create_task(self._fire_after_grace())

    async def _fire_after_grace(self) -> None:
        await self._fire_after(self._config.closing_line_grace_seconds)

    async def _fire_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._fire()

    async def _fire(self) -> None:
        if not self._armed:
            return
        self._armed = False
        # Cancel whichever timer did not fire.
        for task in (self._grace_task, self._fallback_task):
            await _cancel_task(task)
        self._grace_task = None
        self._fallback_task = None

        if self._engine.is_call_disposed():
            return
        logger.info("Call hygiene: ending call after closing line (no end_call tool)")
        _append_tag(self._engine, "closing_line_watchdog")
        await self._engine.end_call_with_reason(
            EndTaskReason.END_CALL_TOOL_REASON.value, abort_immediately=False
        )

    async def _disarm(self) -> None:
        self._armed = False
        for task in (self._grace_task, self._fallback_task):
            await _cancel_task(task)
        self._grace_task = None
        self._fallback_task = None

    async def cleanup(self):
        await self._disarm()
        await super().cleanup()
