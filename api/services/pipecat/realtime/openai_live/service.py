"""Dograh's OpenAI GPT-Live (``gpt-live-1``) LLM service.

Why this exists instead of ``pipecat.services.openai.live``: the pinned Dograh
pipecat fork predates that module (and the worker/backend framework it relies
on for pure client delegation). This service speaks the Live WebSocket protocol
directly using vendored event models and Responses-style delegation, where the
backend model's function calls are **executed here, by the Dograh engine's
registered handlers** — the engine remains authoritative for node state, tools,
SMS, playback, transfers and hangup, and tool results always come from the real
handler, never from the model.

Layers
------
* *Frontend* (GPT-Live): listening, speaking, interruptions, timing. Its
  instructions are immutable per session and contain only voice/persona
  guidance (:func:`build_frontend_instructions`) — never workflow logic.
* *Backend* (Responses model, tool calls executed locally): the node system
  prompt (``settings.system_instruction``) and tool schemas from the shared
  ``LLMContext``; both are re-sent with ``session.update`` on node changes.

Audio path
----------
When the transport runs at 8 kHz (Asterisk/ARI) the session uses
``audio/pcmu`` @ 8 kHz in both directions: μ-law is only (de)companded, never
resampled. Other rates use 24 kHz PCM16 (one resample each way).

Output gate
-----------
While the :class:`ConversationOutputGate` is suppressed (media playback,
terminal tool chains) every model audio/transcript delta is dropped before it
reaches the transport. The same applies after a tool result flagged
``run_llm=False`` until the caller next speaks.
"""

import asyncio
import base64
import json
from typing import Any

from loguru import logger
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from api.services.configuration.options.openai import (
    CUSTOM_VOICE_UNAVAILABLE_MESSAGE,
    OPENAI_LIVE_DEFAULT_MODEL,
    OPENAI_LIVE_DEFAULT_VOICE,
    VOICE_TYPE_BUILTIN,
    VOICE_TYPE_CUSTOM,
    openai_voice_param,
)
from api.services.pipecat import voice_latency as vl
from api.services.pipecat.output_gate import ConversationOutputGate
from api.services.pipecat.realtime.openai_live import events
from api.services.pipecat.realtime.openai_live.adapter import (
    OpenAILiveLLMAdapter,
    OpenAILiveLLMInvocationParams,
)
from api.services.pipecat.realtime.static_greeting import format_static_greeting_prompt
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    AggregationType,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMServiceMetadataFrame,
    LLMSetToolsFrame,
    LLMTextFrame,
    SpeechOutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserMuteStartedFrame,
    UserMuteStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallFromLLM, LLMService
from pipecat.services.openai._constants import OPENAI_SAMPLE_RATE
from pipecat.services.settings import LLMSettings, assert_given
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.utils.time import time_now_iso8601

DEFAULT_BASE_URL = "wss://api.openai.com/v1/live/sessions"
DEFAULT_BACKEND_MODEL = "gpt-4.1"

# Quiet time that ends a speaker's turn (the API emits 200 ms transcript
# fragments, so this must clear ordinary gaps inside speech).
TURN_GAP_SECS = 0.8
# Graceful close waits for the server to drain; a hung-up call must not.
SESSION_CLOSE_TIMEOUT_SECS = 1.5
# Node-transition tool calls wait for the bot to finish a sentence, but never
# longer than this.
TRANSITION_DEFER_TIMEOUT_SECS = 6.0

AUDIO_FORMAT_AUTO = "auto"
AUDIO_FORMAT_PCM = "pcm"

_UNCORRELATED = "uncorrelated"


def build_frontend_instructions(voice_instructions: str | None) -> str:
    """Instructions for the GPT-Live frontend: voice and delegation behaviour only.

    Workflow/business logic lives in the backend prompt; voice delivery
    guidance is kept in its own labelled block.
    """
    parts = [
        "You are the voice of a phone agent. You handle listening, speaking, "
        "timing and being interrupted.",
        "Do not decide business logic yourself. For anything beyond brief "
        "acknowledgements, delegate to your backend: it knows the workflow, "
        "runs the tools, and tells you what to say. Speak what it provides "
        "faithfully and naturally, and do not claim an action happened until "
        "the backend confirms it.",
        "When asked to say something exactly, say exactly that.",
        "If the backend says to stay silent, say nothing.",
    ]
    if voice_instructions and voice_instructions.strip():
        parts.append("## Voice delivery\n" + voice_instructions.strip())
    return "\n\n".join(parts)


class _TurnGrouper:
    def __init__(self, gap_secs: float):
        self.gap_secs = gap_secs
        self.text = ""
        self.open = False
        self.timer: asyncio.Task | None = None
        self.lock = asyncio.Lock()


class _PendingResponse:
    def __init__(self):
        self.call_ids: set[str] = set()
        self.had_calls = False
        self.finished = False


class DograhOpenAILiveLLMService(LLMService[OpenAILiveLLMAdapter]):
    adapter_class = OpenAILiveLLMAdapter

    def __init__(
        self,
        *,
        api_key: str,
        model: str = OPENAI_LIVE_DEFAULT_MODEL,
        voice: str = OPENAI_LIVE_DEFAULT_VOICE,
        voice_type: str = VOICE_TYPE_BUILTIN,
        custom_voice_id: str | None = None,
        voice_instructions: str | None = None,
        backend_model: str = DEFAULT_BACKEND_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        audio_format: str = AUDIO_FORMAT_AUTO,
        output_gate: ConversationOutputGate | None = None,
        latency_tracker: vl.VoiceLatencyTracker | None = None,
        **kwargs,
    ):
        if voice_type == VOICE_TYPE_CUSTOM and not custom_voice_id:
            raise ValueError("custom_voice_id is required for a custom OpenAI voice")
        settings = LLMSettings(
            model=model,
            system_instruction=None,
            temperature=None,
            max_tokens=None,
            top_p=None,
            top_k=None,
            frequency_penalty=None,
            presence_penalty=None,
            seed=None,
            filter_incomplete_user_turns=False,
            user_turn_completion_config=None,
        )
        super().__init__(settings=settings, **kwargs)

        self.api_key = api_key
        self.base_url = base_url
        self._model = model
        self._voice = openai_voice_param(voice_type, voice, custom_voice_id)
        self._voice_type = voice_type
        self._custom_voice_id = custom_voice_id
        self._backend_model = backend_model
        self._frontend_instructions = build_frontend_instructions(voice_instructions)
        self._audio_format_mode = audio_format
        self._wire_format = events.AudioFormat(
            type="audio/pcm", rate=OPENAI_SAMPLE_RATE
        )
        self._output_gate = output_gate or ConversationOutputGate()
        self._output_gate.add_listener(self._on_output_gate_changed)
        self._latency = latency_tracker or vl.VoiceLatencyTracker(
            component="openai_live"
        )

        self._websocket = None
        self._receive_task: asyncio.Task | None = None
        self._disconnecting = False

        self._context: LLMContext | None = None
        self._session_config_sent = False
        self._session_started = False
        self._session_closed_event = asyncio.Event()
        self._sent_delegation_snapshot: str | None = None
        self._pending_opening_text: str | None = None

        self._in_resampler = create_stream_resampler()
        self._out_resampler = create_stream_resampler()
        self._pipeline_in_rate = 16000
        self._pipeline_out_rate = 16000

        self._user_is_muted = False
        self._bot_is_speaking = False
        self._silent_until_user_speaks = False
        self._first_audio_marked = False
        self._deferred_transition_calls: list[FunctionCallFromLLM] = []
        self._defer_timer: asyncio.Task | None = None

        self._user_turn = _TurnGrouper(TURN_GAP_SECS)
        self._assistant_turn = _TurnGrouper(TURN_GAP_SECS)

        self._open_function_calls: dict[str, str] = {}
        self._pending_responses: dict[str, _PendingResponse] = {}

        self._register_event_handler("on_session_started")

    # ------------------------------------------------------------------
    # metadata / lifecycle
    # ------------------------------------------------------------------

    def can_generate_metrics(self) -> bool:
        return True

    def service_metadata_frame(self) -> LLMServiceMetadataFrame:
        return LLMServiceMetadataFrame(
            service_name=self.name,
            is_realtime_service=True,
            user_turn_strategies=ExternalUserTurnStrategies(),
        )

    @property
    def wire_format(self) -> events.AudioFormat:
        return self._wire_format

    def _choose_wire_format(self, transport_in_rate: int, transport_out_rate: int):
        """Pick the session audio format. 8 kHz telephony ⇒ μ-law, no resample."""
        if (
            self._audio_format_mode == AUDIO_FORMAT_AUTO
            and transport_in_rate == 8000
            and transport_out_rate == 8000
        ):
            return events.AudioFormat(type="audio/pcmu", rate=8000)
        return events.AudioFormat(type="audio/pcm", rate=OPENAI_SAMPLE_RATE)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._pipeline_in_rate = frame.audio_in_sample_rate
        self._pipeline_out_rate = frame.audio_out_sample_rate
        self._wire_format = self._choose_wire_format(
            self._pipeline_in_rate, self._pipeline_out_rate
        )
        logger.info(
            f"{self}: audio path transport_in={self._pipeline_in_rate}Hz "
            f"transport_out={self._pipeline_out_rate}Hz wire={self._wire_format.type}"
            f"@{self._wire_format.rate}"
        )
        await self._connect()
        # Node prompt and tools are set before the pipeline starts; send the
        # session config now so the model is listening as soon as the call is.
        await self._maybe_send_session_config()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._close_open_turns()
        await self._close_session()
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()

    async def _update_settings(self, delta):
        """Apply settings; a changed node prompt is pushed to the backend model."""
        changed = await super()._update_settings(delta)
        if "system_instruction" in changed:
            await self._maybe_send_session_config()
            await self._maybe_send_delegation_update()
        return changed

    # ------------------------------------------------------------------
    # frame processing
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserMuteStartedFrame):
            self._user_is_muted = True
        elif isinstance(frame, UserMuteStoppedFrame):
            self._user_is_muted = False
        elif isinstance(frame, LLMContextFrame):
            self._context = frame.context
            await self._maybe_send_session_config()
            await self._maybe_send_delegation_update()
        elif isinstance(frame, InputAudioRawFrame):
            await self._send_user_audio(frame)
        elif isinstance(frame, LLMSetToolsFrame):
            self._sync_registered_tool_handlers(frame.tools)
            await self._maybe_send_delegation_update()
        elif isinstance(frame, TTSSpeakFrame):
            await self._handle_speak_request(frame)
            return  # the model owns audio; never forward to TTS
        elif isinstance(frame, LLMMessagesAppendFrame):
            await self._handle_messages_append(frame)
            return
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_is_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_is_speaking = False
            await self._run_deferred_transition_calls()

        await self.push_frame(frame, direction)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        # Function results are broadcast by the base service; observe the
        # downstream copy and answer the backend model with it.
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, FunctionCallResultFrame):
                await self._handle_function_call_result(frame)
            elif isinstance(frame, FunctionCallCancelFrame):
                await self._handle_function_call_cancel(frame)
        await super().push_frame(frame, direction)

    # ------------------------------------------------------------------
    # session configuration
    # ------------------------------------------------------------------

    def _invocation_params(self) -> OpenAILiveLLMInvocationParams:
        context = self._context or LLMContext()
        return self.get_llm_adapter().get_llm_invocation_params(
            context, system_instruction=assert_given(self._settings.system_instruction)
        )

    def _delegation_responses(
        self, params: OpenAILiveLLMInvocationParams
    ) -> dict[str, Any]:
        cfg: dict[str, Any] = {"model": self._backend_model}
        if params["instructions"]:
            cfg["instructions"] = params["instructions"]
        if params["tools"]:
            cfg["tools"] = params["tools"]
        if params["tool_choice"] is not None:
            cfg["tool_choice"] = params["tool_choice"]
        return cfg

    def build_session_start(self) -> events.SessionStartEvent:
        """The ``session.start`` event for the current call state."""
        params = self._invocation_params()
        responses = self._delegation_responses(params)
        self._sent_delegation_snapshot = json.dumps(
            responses, sort_keys=True, default=str
        )
        return events.SessionStartEvent(
            session=events.SessionConfig(
                model=self._model,
                instructions=self._frontend_instructions,
                audio=events.AudioConfig(
                    output=events.AudioOutputConfig(voice=self._voice),
                    format=self._wire_format,
                ),
                delegation=events.ResponsesDelegationConfig(responses=responses),
                input=params["input"] or None,
            )
        )

    async def _maybe_send_session_config(self):
        if self._session_config_sent or not self._websocket:
            return
        # Wait for the node prompt: starting without it would give the backend
        # model no workflow instructions (they cannot be replaced afterwards
        # without an update round-trip).
        if assert_given(self._settings.system_instruction) is None and not (
            self._context and self._context.get_messages()
        ):
            return
        self._session_config_sent = True
        await self.send_client_event(self.build_session_start())

    async def _maybe_send_delegation_update(self):
        if not self._session_started or not self._context:
            return
        responses = self._delegation_responses(self._invocation_params())
        snapshot = json.dumps(responses, sort_keys=True, default=str)
        if snapshot == self._sent_delegation_snapshot:
            return
        self._sent_delegation_snapshot = snapshot
        await self.send_client_event(
            events.SessionUpdateEvent(
                session=events.SessionUpdateConfig(
                    delegation=events.ResponsesDelegationConfig(responses=responses)
                )
            )
        )

    # ------------------------------------------------------------------
    # websocket
    # ------------------------------------------------------------------

    async def send_client_event(self, event: events.ClientEvent):
        await self._ws_send(event.to_payload())

    async def _connect(self):
        if self._websocket:
            return
        self._latency.mark(vl.SESSION_CONNECT_START)
        try:
            self._websocket = await websocket_connect(
                uri=self.base_url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
            )
            self._receive_task = self.create_task(self._receive_task_handler())
        except Exception as e:  # noqa: BLE001
            self._websocket = None
            # Never log the key; the exception text from websockets does not include headers.
            logger.error(f"{self}: GPT-Live connection failed: {type(e).__name__}: {e}")
            await self.push_error(
                error_msg=f"OpenAI GPT-Live connection failed: {e}",
                exception=e,
                fatal=True,
            )

    async def _disconnect(self):
        if self._disconnecting:
            return
        try:
            self._disconnecting = True
            self._session_started = False
            await self.stop_all_metrics()
            if self._defer_timer:
                await self.cancel_task(self._defer_timer)
                self._defer_timer = None
            for turn in (self._user_turn, self._assistant_turn):
                if turn.timer is not None:
                    await self.cancel_task(turn.timer)
                    turn.timer = None
            if self._websocket:
                await self._websocket.close()
                self._websocket = None
            if self._receive_task:
                await self.cancel_task(self._receive_task, timeout=1.0)
                self._receive_task = None
            self._deferred_transition_calls.clear()
            self._open_function_calls.clear()
            self._pending_responses.clear()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self}: error during GPT-Live disconnect: {e}")
        finally:
            self._disconnecting = False

    async def _close_session(self):
        if not self._websocket or not self._session_started:
            return
        self._session_closed_event.clear()
        await self.send_client_event(events.SessionCloseEvent())
        try:
            await asyncio.wait_for(
                self._session_closed_event.wait(), timeout=SESSION_CLOSE_TIMEOUT_SECS
            )
        except TimeoutError:
            logger.debug(f"{self}: timed out waiting for session.closed")

    async def _ws_send(self, message: dict[str, Any]):
        if self._disconnecting or not self._websocket:
            return
        try:
            await self._websocket.send(json.dumps(message))
        except Exception as e:  # noqa: BLE001
            if self._disconnecting or not self._websocket:
                return
            logger.error(f"{self}: GPT-Live send failed: {type(e).__name__}: {e}")
            await self.push_error(
                error_msg=f"Error sending GPT-Live event: {e}", exception=e, fatal=True
            )

    async def _receive_task_handler(self):
        ws = self._websocket
        assert ws is not None
        try:
            async for message in ws:
                try:
                    evt = events.parse_server_event(message)
                except ValueError as e:
                    logger.warning(f"{self}: ignoring unparseable server event: {e}")
                    continue
                await self._handle_server_event(evt)
        except ConnectionClosed as e:
            if not self._disconnecting:
                await self._on_unexpected_disconnect(f"connection closed: {e}")
            return
        if not self._disconnecting and self._session_started:
            await self._on_unexpected_disconnect("connection ended")

    async def _on_unexpected_disconnect(self, reason: str):
        """Upstream dropped mid-call: stop stale output and fail the call explicitly."""
        logger.error(f"{self}: OpenAI GPT-Live disconnected mid-call ({reason})")
        self._session_started = False
        self._output_gate.record_suppressed()
        await self._close_open_turns()
        await self.push_error(
            error_msg=f"OpenAI GPT-Live disconnected: {reason}", fatal=True
        )

    # ------------------------------------------------------------------
    # server events
    # ------------------------------------------------------------------

    async def _handle_server_event(self, evt: events.ServerEvent):
        if isinstance(evt, events.SessionStartedEvent):
            await self._handle_session_started(evt)
        elif isinstance(evt, events.OutputAudioDeltaEvent):
            await self._handle_audio_delta(evt)
        elif isinstance(evt, events.TranscriptDeltaEvent):
            await self._handle_transcript_delta(evt)
        elif isinstance(evt, events.SessionDelegationCreatedEvent):
            self._latency.mark(vl.BACKEND_START)
            logger.debug(
                f"{self}: delegation {evt.delegation.id} ({evt.delegation.target})"
            )
        elif isinstance(evt, events.ResponseEventEnvelope):
            await self._handle_response_event(evt)
        elif isinstance(evt, events.SessionClosedEvent):
            self._session_started = False
            self._session_closed_event.set()
        elif isinstance(evt, events.ErrorEvent):
            await self._handle_error(evt)
        elif isinstance(evt, events.SessionUsageUpdatedEvent):
            if evt.usage.seconds is not None:
                logger.debug(f"{self}: live audio usage {evt.usage.seconds:.1f}s")

    async def _handle_session_started(self, evt: events.SessionStartedEvent):
        self._session_started = True
        self._latency.mark(vl.SESSION_READY)
        ms = self._latency.delta_ms(vl.SESSION_CONNECT_START, vl.SESSION_READY)
        logger.info(f"{self}: session {evt.session.id} ready (connect_ms={ms})")
        await self._call_event_handler("on_session_started", evt.session)
        await self._maybe_send_delegation_update()
        if self._pending_opening_text is not None:
            text, self._pending_opening_text = self._pending_opening_text, None
            await self._send_context_append(None, text, spoken=True)

    async def _handle_error(self, evt: events.ErrorEvent):
        error = evt.error
        details = f"{error.type or 'error'}/{error.code or 'unknown'}: {error.message}"
        if error.param:
            details += f" (param: {error.param})"
        if self._voice_type == VOICE_TYPE_CUSTOM and (
            error.param
            and "voice" in error.param
            or error.code in ("voice_not_found", "invalid_voice", "permission_denied")
        ):
            # Surface — never substitute another voice.
            details = f"{CUSTOM_VOICE_UNAVAILABLE_MESSAGE} ({details})"
            await self.push_error(error_msg=details, fatal=True)
            return
        if not self._session_started:
            await self.push_error(
                error_msg=f"GPT-Live session startup failed: {details}", fatal=True
            )
        else:
            await self.push_error(error_msg=details)

    # ------------------------------------------------------------------
    # audio
    # ------------------------------------------------------------------

    async def _send_user_audio(self, frame: InputAudioRawFrame):
        if not self._session_started or self._user_is_muted or self._disconnecting:
            return
        audio = frame.audio
        if self._wire_format.type == "audio/pcmu":
            audio = await pcm_to_ulaw(
                audio, frame.sample_rate, 8000, self._in_resampler
            )
        elif frame.sample_rate != OPENAI_SAMPLE_RATE:
            audio = await self._in_resampler.resample(
                audio, frame.sample_rate, OPENAI_SAMPLE_RATE
            )
        if not audio:
            return
        await self.send_client_event(
            events.InputAudioAppendEvent(audio=base64.b64encode(audio).decode("utf-8"))
        )

    def _output_allowed(self) -> bool:
        return self._output_gate.enabled and not self._silent_until_user_speaks

    async def _handle_audio_delta(self, evt: events.OutputAudioDeltaEvent):
        if not self._output_allowed():
            self._output_gate.record_suppressed()
            return
        raw = base64.b64decode(evt.delta)
        if self._wire_format.type == "audio/pcmu":
            pcm = await ulaw_to_pcm(
                raw, 8000, self._pipeline_out_rate, self._out_resampler
            )
            rate = self._pipeline_out_rate
        else:
            pcm, rate = raw, OPENAI_SAMPLE_RATE
        if not pcm:
            return
        if not self._first_audio_marked:
            self._first_audio_marked = True
            self._latency.mark(vl.FIRST_AUDIO_BYTE)
        # The model streams continuously at real-time pace (silence included),
        # so this is a speech stream, not TTS output.
        await self.push_frame(
            SpeechOutputAudioRawFrame(audio=pcm, sample_rate=rate, num_channels=1)
        )
        self._latency.mark(vl.FIRST_AUDIO_FRAME_SENT)

    # ------------------------------------------------------------------
    # transcript turns
    # ------------------------------------------------------------------

    async def _handle_transcript_delta(self, evt: events.TranscriptDeltaEvent):
        if not evt.delta:
            return
        role = evt.role
        if role == "assistant" and not self._output_allowed():
            self._output_gate.record_suppressed()
            return
        turn = self._user_turn if role == "user" else self._assistant_turn
        async with turn.lock:
            if not turn.open:
                turn.open = True
                turn.text = ""
                await self._open_turn(role)
            turn.text += evt.delta
            if role == "assistant":
                await self._push_assistant_text(evt.delta)
            else:
                self._latency.mark(vl.USER_SPEECH_STOPPED)  # last fragment wins
            if turn.timer is not None:
                await self.cancel_task(turn.timer)
            turn.timer = self.create_task(
                self._close_turn_after_gap(role, turn), f"turn-gap:{role}"
            )

    async def _close_turn_after_gap(self, role: str, turn: _TurnGrouper):
        await asyncio.sleep(turn.gap_secs)
        async with turn.lock:
            turn.timer = None
            await self._end_turn(role)

    async def _close_turn(self, role: str):
        turn = self._user_turn if role == "user" else self._assistant_turn
        async with turn.lock:
            if turn.timer is not None:
                await self.cancel_task(turn.timer)
                turn.timer = None
            await self._end_turn(role)

    async def _close_open_turns(self):
        await self._close_turn("user")
        await self._close_turn("assistant")

    async def _open_turn(self, role: str):
        if role == "user":
            self._latency.mark(vl.USER_SPEECH_STARTED)
            # Caller spoke: model speech is allowed again after a silent continuation.
            self._silent_until_user_speaks = False
            if self._bot_is_speaking:
                logger.debug(f"{self}: caller barged in while assistant was speaking")
            await self.broadcast_frame(UserStartedSpeakingFrame)
        else:
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(TTSStartedFrame())

    async def _end_turn(self, role: str):
        turn = self._user_turn if role == "user" else self._assistant_turn
        if not turn.open:
            return
        turn.open = False
        text, turn.text = turn.text, ""
        if role == "user":
            if text.strip():
                self._latency.mark(vl.TRANSCRIPT_COMPLETE)
                await self.broadcast_frame(
                    TranscriptionFrame,
                    text=text.strip(),
                    user_id="",
                    timestamp=time_now_iso8601(),
                    finalized=True,
                )
            await self.broadcast_frame(UserStoppedSpeakingFrame)
        else:
            await self.push_frame(TTSStoppedFrame())
            await self.push_frame(LLMFullResponseEndFrame())
            self._latency.mark(vl.RESPONSE_COMPLETE)
            self._latency.finish_turn()
            self._first_audio_marked = False

    async def _push_assistant_text(self, text: str):
        llm_text = LLMTextFrame(text)
        llm_text.append_to_context = False
        await self.push_frame(llm_text)
        tts_text = TTSTextFrame(text, aggregated_by=AggregationType.SENTENCE)
        tts_text.includes_inter_frame_spaces = True
        await self.push_frame(tts_text)

    # ------------------------------------------------------------------
    # engine-originated speech / context appends
    # ------------------------------------------------------------------

    async def _send_context_append(
        self, delegation_id: str | None, text: str, *, spoken: bool
    ):
        if not text.strip():
            return
        cls = (
            events.SessionCommentaryAppendEvent
            if spoken
            else events.SessionThinkingAppendEvent
        )
        # The API accepts at most ~500 tokens per append; keep well under.
        for start in range(0, len(text), 1500):
            await self.send_client_event(
                cls(delegation_id=delegation_id, content=text[start : start + 1500])
            )

    async def _handle_speak_request(self, frame: TTSSpeakFrame):
        text = (frame.text or "").strip()
        if not text:
            return
        if not self._output_allowed():
            logger.info(
                f"{self}: dropping speak request while conversation output is suppressed"
            )
            self._output_gate.record_suppressed()
            return
        prompt = format_static_greeting_prompt(text)
        if self._session_started:
            await self._send_context_append(None, prompt, spoken=True)
        else:
            self._pending_opening_text = prompt

    async def _handle_messages_append(self, frame: LLMMessagesAppendFrame):
        texts = []
        for m in frame.messages:
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                texts.append(m["content"])
        text = "\n".join(texts).strip()
        if not text:
            return
        speak = bool(frame.run_llm) and self._output_allowed()
        if not self._session_started:
            if speak:
                self._pending_opening_text = text
            return
        await self._send_context_append(None, text, spoken=speak)

    # ------------------------------------------------------------------
    # delegation: function calls run by the Dograh engine's handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _correlation_key(evt: events.ResponseEventEnvelope) -> str:
        return evt.delegation_id or _UNCORRELATED

    async def _handle_response_event(self, evt: events.ResponseEventEnvelope):
        inner = evt.inner_type
        key = self._correlation_key(evt)
        if inner == "response.created":
            self._pending_responses.setdefault(key, _PendingResponse())
        elif inner == "response.output_item.done":
            await self._handle_output_item_done(evt, key)
        elif inner in ("response.completed", "response.incomplete", "response.failed"):
            self._latency.mark(vl.BACKEND_COMPLETE)
            if inner != "response.completed":
                response = evt.event.get("response") or {}
                await self.push_error(
                    error_msg=f"Delegated response {inner.removeprefix('response.')}: "
                    f"{response.get('status', 'unknown')}"
                )
            pending = self._pending_responses.get(key)
            if pending is not None:
                pending.finished = True
                await self._maybe_continue_response(key)

    async def _handle_output_item_done(
        self, evt: events.ResponseEventEnvelope, key: str
    ):
        item = events.ResponseOutputItem.model_validate(evt.event.get("item") or {})
        if item.type != "function_call" or item.status != "completed":
            return
        if not item.call_id or not item.name:
            logger.warning(f"{self}: function call item without call_id or name")
            return
        if item.call_id in self._open_function_calls:
            return
        try:
            arguments = json.loads(item.arguments) if item.arguments else {}
        except json.JSONDecodeError as e:
            await self.push_error(
                error_msg=f"Invalid arguments for {item.name}: {e}", exception=e
            )
            return

        pending = self._pending_responses.setdefault(key, _PendingResponse())
        pending.call_ids.add(item.call_id)
        pending.had_calls = True
        self._open_function_calls[item.call_id] = key

        call = FunctionCallFromLLM(
            context=self._context,
            tool_call_id=item.call_id,
            function_name=item.name,
            arguments=arguments,
        )
        # Workflow-control calls (transitions, end call, transfer) wait for the
        # current sentence to finish so the caller isn't cut off mid-word —
        # except when output is suppressed and nobody is being spoken to.
        if (
            self._bot_is_speaking
            and self._output_allowed()
            and self._function_is_node_transition(item.name)
        ):
            self._deferred_transition_calls.append(call)
            if self._defer_timer is None:
                self._defer_timer = self.create_task(
                    self._defer_timeout(), "defer-transition"
                )
            return
        await self.run_function_calls([call])

    async def _defer_timeout(self):
        await asyncio.sleep(TRANSITION_DEFER_TIMEOUT_SECS)
        self._defer_timer = None
        await self._run_deferred_transition_calls()

    async def _run_deferred_transition_calls(self):
        if not self._deferred_transition_calls:
            return
        calls, self._deferred_transition_calls = self._deferred_transition_calls, []
        timer, self._defer_timer = self._defer_timer, None
        if timer is not None and timer is not asyncio.current_task():
            await self.cancel_task(timer)
        await self.run_function_calls(calls)

    async def _handle_function_call_result(self, frame: FunctionCallResultFrame):
        if frame.tool_call_id not in self._open_function_calls:
            return
        props = frame.properties
        if props is not None and not props.is_final:
            logger.warning(
                f"{self}: dropping non-final result for {frame.function_name}"
            )
            return
        if props is not None and props.run_llm is False:
            # Existing contract: the bot stays silent after this tool until the
            # caller speaks (play_audio, end_call, transfer). The backend still
            # needs its output to complete, so keep the model quiet instead.
            self._silent_until_user_speaks = True
        output = (
            json.dumps(frame.result, ensure_ascii=False)
            if frame.result
            else "COMPLETED"
        )
        await self._send_function_call_output(frame.tool_call_id, output)

    async def _handle_function_call_cancel(self, frame: FunctionCallCancelFrame):
        if frame.tool_call_id not in self._open_function_calls:
            return
        await self._send_function_call_output(
            frame.tool_call_id,
            json.dumps(
                {
                    "error": "The function call was cancelled before it produced a result."
                }
            ),
        )

    async def _send_function_call_output(self, call_id: str, output: str):
        key = self._open_function_calls.pop(call_id, "")
        await self.send_client_event(
            events.ResponseItemCreateEvent(
                item=events.FunctionCallOutputItem(call_id=call_id, output=output)
            )
        )
        pending = self._pending_responses.get(key)
        if pending is not None:
            pending.call_ids.discard(call_id)
        await self._maybe_continue_response(key)

    async def _maybe_continue_response(self, key: str):
        pending = self._pending_responses.get(key)
        if (
            pending is None
            or not pending.finished
            or not pending.had_calls
            or pending.call_ids
        ):
            return
        del self._pending_responses[key]
        await self.send_client_event(events.ResponseCreateEvent())

    # ------------------------------------------------------------------
    # output gate
    # ------------------------------------------------------------------

    def _on_output_gate_changed(self, enabled: bool) -> None:
        if enabled:
            return
        # Audio already buffered toward the caller cannot be recalled, but no
        # further model audio/transcript will be forwarded; close any open
        # assistant turn so response frames stay balanced.
        coro = self._close_turn("assistant")
        try:
            self.create_task(coro, "gate-close-assistant-turn")
        except Exception as e:  # noqa: BLE001
            coro.close()
            logger.debug(f"{self}: could not schedule assistant turn close: {e}")
