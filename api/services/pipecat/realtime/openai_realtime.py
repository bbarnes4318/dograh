"""Dograh subclass of pipecat's OpenAI Realtime LLM service.

Layers Dograh engine integration quirks onto upstream-pristine
:class:`OpenAIRealtimeLLMService`. Substantially smaller than the Gemini
subclass because OpenAI Realtime supports runtime ``session.update`` for
both ``system_instruction`` and tools, so node changes do not require a
reconnect.

Adds:

- **User-mute audio gating** via ``UserMuteStarted/StoppedFrame``.
- **TTSSpeakFrame as initial-response trigger** so the engine's greeting
  flow kicks off the bot's first response.
- **One-off LLMMessagesAppendFrame handling** for ephemeral realtime prompts
  like user-idle checks, without mutating Dograh's local ``LLMContext``.
- **Workflow-control deferral** so node transitions, call termination, and
  transfers wait for any current bot audio to finish while ordinary tools run
  immediately.
- **finalized=True on TranscriptionFrame** because every OpenAI
  transcription via the ``completed`` event is final by construction.
- **Output gate** (:class:`ConversationOutputGate`): while suppressed (media
  playback / terminal tool chains) model audio and text are dropped, in-flight
  speech is cancelled and new responses are text-only, so the model can still
  call tools but cannot speak.
- **Custom voice objects** (``{"id": "voice_..."}``) and separate **voice
  delivery instructions**, both applied when the session update is serialized.
- **Latency marks** per turn (see :mod:`api.services.pipecat.voice_latency`).
"""

import json
from typing import Any

from loguru import logger

from api.services.configuration.options.openai import (
    VOICE_TYPE_BUILTIN,
    VOICE_TYPE_CUSTOM,
)
from api.services.pipecat import voice_latency as vl
from api.services.pipecat.output_gate import ConversationOutputGate
from api.services.pipecat.realtime.static_greeting import format_static_greeting_prompt
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserMuteStartedFrame,
    UserMuteStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallFromLLM
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601


class DograhOpenAIRealtimeLLMService(OpenAIRealtimeLLMService):
    """OpenAI Realtime with Dograh engine integration quirks. See module docstring."""

    def __init__(
        self,
        *,
        output_gate: ConversationOutputGate | None = None,
        voice_type: str = VOICE_TYPE_BUILTIN,
        custom_voice_id: str | None = None,
        voice_instructions: str | None = None,
        latency_tracker: vl.VoiceLatencyTracker | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        if voice_type == VOICE_TYPE_CUSTOM and not custom_voice_id:
            raise ValueError("custom_voice_id is required for a custom OpenAI voice")
        self._voice_type = voice_type
        self._custom_voice_id = custom_voice_id
        self._voice_instructions = (voice_instructions or "").strip() or None
        self._output_gate = output_gate or ConversationOutputGate()
        self._output_gate.add_listener(self._on_output_gate_changed)
        self._latency = latency_tracker or vl.VoiceLatencyTracker(
            component="openai_realtime"
        )
        self._first_audio_marked = False
        self._user_is_muted: bool = False
        # Dograh pre-populates self._context via the engine before the first
        # LLMContextFrame arrives, so upstream's "first arrival means
        # self._context is None" check no longer works.
        self._handled_initial_context: bool = False
        # Track bot speech locally so workflow-control calls can wait until the
        # bot has finished speaking without delaying ordinary tools.
        self._bot_is_speaking: bool = False
        self._deferred_node_transition_function_calls: list[FunctionCallFromLLM] = []
        self._pending_initial_greeting_text: str | None = None

    # ------------------------------------------------------------------
    # Session serialization: custom voice object + voice delivery block
    # ------------------------------------------------------------------

    def build_client_payload(self, event: events.ClientEvent) -> dict[str, Any]:
        """Serialize an event, applying custom-voice and delivery instructions."""
        payload = event.model_dump(exclude_none=True)
        session = payload.get("session") if isinstance(payload, dict) else None
        if isinstance(session, dict):
            if self._voice_type == VOICE_TYPE_CUSTOM and self._custom_voice_id:
                audio = session.setdefault("audio", {})
                output = audio.setdefault("output", {})
                output["voice"] = {"id": self._custom_voice_id}
            if self._voice_instructions and session.get("instructions"):
                session["instructions"] = (
                    f"{session['instructions']}\n\n## Voice delivery\n"
                    f"{self._voice_instructions}"
                )
        return payload

    async def send_client_event(self, event: events.ClientEvent):
        await self._ws_send(self.build_client_payload(event))

    # ------------------------------------------------------------------
    # Output gate: model output is never forwarded while suppressed
    # ------------------------------------------------------------------

    def _get_enabled_modalities(self) -> list[str]:
        if not self._output_gate.enabled:
            # Tool-calling turns during a terminal chain: no audio at all.
            return ["text"]
        return super()._get_enabled_modalities()

    def _on_output_gate_changed(self, enabled: bool) -> None:
        if enabled or self._disconnecting:
            return
        # Silence whatever is already being generated or spoken.
        coro = self._cancel_active_output()
        try:
            self.create_task(coro, "gate-cancel-output")
        except Exception as e:  # noqa: BLE001
            coro.close()
            logger.warning(f"{self}: could not schedule output cancel: {e}")

    async def _cancel_active_output(self):
        if self._disconnecting:
            return
        await self.send_client_event(events.ResponseCancelEvent())
        await self._truncate_current_audio_response()
        self._current_audio_response = None

    async def _handle_evt_audio_delta(self, evt):
        if not self._output_gate.enabled:
            self._output_gate.record_suppressed()
            return
        if not self._first_audio_marked:
            self._first_audio_marked = True
            self._latency.mark(vl.FIRST_AUDIO_BYTE)
        await super()._handle_evt_audio_delta(evt)
        if self._first_audio_marked:
            self._latency.mark(vl.FIRST_AUDIO_FRAME_SENT)

    async def _handle_evt_audio_transcript_delta(self, evt):
        if not self._output_gate.enabled:
            self._output_gate.record_suppressed()
            return
        await super()._handle_evt_audio_transcript_delta(evt)

    async def _handle_evt_text_delta(self, evt):
        if not self._output_gate.enabled:
            self._output_gate.record_suppressed()
            return
        await super()._handle_evt_text_delta(evt)

    async def _handle_evt_response_done(self, evt):
        await super()._handle_evt_response_done(evt)
        if self._first_audio_marked:
            self._latency.mark(vl.RESPONSE_COMPLETE)
            self._latency.finish_turn()
        self._first_audio_marked = False

    async def _handle_evt_speech_started(self, evt):
        self._latency.mark(vl.USER_SPEECH_STARTED)
        await super()._handle_evt_speech_started(evt)
        # Barge-in: stale assistant audio was truncated/interrupted upstream.
        self._latency.mark(vl.INTERRUPTION_DETECTED)
        self._first_audio_marked = False

    async def _handle_evt_speech_stopped(self, evt):
        self._latency.mark(vl.USER_SPEECH_STOPPED)
        await super()._handle_evt_speech_stopped(evt)

    # ------------------------------------------------------------------
    # Frame handling: mute, TTSSpeakFrame as greeting trigger
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, UserMuteStartedFrame):
            self._user_is_muted = True
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, UserMuteStoppedFrame):
            self._user_is_muted = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TTSSpeakFrame):
            # Greeting trigger: the engine queues a TTSSpeakFrame after node
            # setup. OpenAI Realtime renders its own audio, so we don't pass
            # the frame to TTS. For configured static text greetings, ask the
            # model to say the exact greeting; otherwise route through
            # _handle_context so the initial response and later tool-result
            # turns share the same context lifecycle.
            if not self._handled_initial_context:
                greeting_text = frame.text.strip() if frame.text else ""
                if greeting_text:
                    await self._handle_initial_greeting(self._context, greeting_text)
                else:
                    await self._handle_context(self._context)
            else:
                logger.warning(
                    f"{self}: TTSSpeakFrame after initial context already "
                    "handled — OpenAI Realtime owns audio generation, ignoring"
                )
            # Don't forward the frame; the audio path is owned by the realtime
            # service itself.
            return
        if isinstance(frame, LLMMessagesAppendFrame):
            await self._handle_messages_append(frame)
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_is_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_is_speaking = False
            await self._run_pending_node_transition_function_calls()
        await super().process_frame(frame, direction)

    async def _handle_messages_append(self, frame: LLMMessagesAppendFrame):
        """Consume a one-off append frame without mutating the local LLMContext."""
        if self._disconnecting:
            return

        if not self._api_session_ready:
            if frame.run_llm:
                logger.debug(
                    f"{self}: LLMMessagesAppendFrame received before session ready; "
                    "deferring response until the session is initialized"
                )
                self._run_llm_when_api_session_ready = True
            return

        appended_any = False
        for message in frame.messages:
            item = self._message_to_conversation_item(message)
            if item is None:
                continue
            evt = events.ConversationItemCreateEvent(item=item)
            self._messages_added_manually[evt.item.id] = True
            await self.send_client_event(evt)
            appended_any = True

        if frame.run_llm and appended_any:
            await self._send_manual_response_create()

    async def _handle_context(self, context: LLMContext):
        if not self._handled_initial_context:
            if context is None:
                logger.warning(
                    f"{self}: received initial context trigger before context was set"
                )
                return
            self._handled_initial_context = True
            self._context = context
            await self._create_response()
        else:
            self._context = context
            await self._process_completed_function_calls(send_new_results=True)

    async def _handle_initial_greeting(self, context: LLMContext, greeting_text: str):
        if context is None:
            logger.warning(
                f"{self}: received initial greeting trigger before context was set"
            )
            return

        self._handled_initial_context = True
        self._context = context
        await self._create_initial_greeting_response(greeting_text)

    async def _create_initial_greeting_response(self, greeting_text: str):
        if self._disconnecting:
            return

        if not self._api_session_ready:
            self._pending_initial_greeting_text = greeting_text
            self._run_llm_when_api_session_ready = True
            return

        self._pending_initial_greeting_text = None
        await self._ensure_conversation_setup()
        await self._send_manual_response_create(
            instructions=format_static_greeting_prompt(greeting_text),
            tool_choice="none",
        )

    async def _ensure_conversation_setup(self):
        if not self._llm_needs_conversation_setup:
            return

        adapter = self.get_llm_adapter()
        llm_invocation_params = adapter.get_llm_invocation_params(self._context)
        for item in llm_invocation_params["messages"]:
            evt = events.ConversationItemCreateEvent(item=item)
            self._messages_added_manually[evt.item.id] = True
            await self.send_client_event(evt)

        await self._send_session_update()
        self._llm_needs_conversation_setup = False

    async def _handle_evt_session_updated(self, evt):
        self._api_session_ready = True
        if self._pending_initial_greeting_text is not None:
            greeting_text = self._pending_initial_greeting_text
            self._run_llm_when_api_session_ready = False
            await self._create_initial_greeting_response(greeting_text)
        elif self._run_llm_when_api_session_ready:
            self._run_llm_when_api_session_ready = False
            await self._create_response()

    async def _send_user_audio(self, frame):
        if self._user_is_muted:
            return
        await super()._send_user_audio(frame)

    def _message_to_conversation_item(
        self, message: dict[str, Any]
    ) -> events.ConversationItem | None:
        if not isinstance(message, dict):
            logger.warning(
                f"{self}: skipping unsupported appended message payload {message!r}"
            )
            return None

        role = message.get("role")
        if role not in {"user", "system", "developer"}:
            logger.warning(
                f"{self}: skipping unsupported appended message role {role!r}"
            )
            return None

        text = self._extract_text_content(message.get("content"))
        if not text:
            logger.warning(
                f"{self}: skipping appended message with unsupported content {message!r}"
            )
            return None

        item_role = "system" if role in {"system", "developer"} else "user"
        return events.ConversationItem(
            type="message",
            role=item_role,
            content=[events.ItemContent(type="input_text", text=text)],
        )

    @staticmethod
    def _extract_text_content(content: Any) -> str | None:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    return None
                if part.get("type") != "text":
                    return None
                text = part.get("text")
                if not isinstance(text, str):
                    return None
                parts.append(text)
            return "\n".join(parts) if parts else None
        return None

    async def _send_manual_response_create(
        self,
        *,
        instructions: str | None = None,
        tool_choice: str | None = None,
    ):
        """Trigger inference after manually appending conversation items."""
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        await self.send_client_event(
            events.ResponseCreateEvent(
                response=events.ResponseProperties(
                    output_modalities=self._get_enabled_modalities(),
                    instructions=instructions,
                    tool_choice=tool_choice,
                )
            )
        )

    async def _run_pending_node_transition_function_calls(self):
        if not self._deferred_node_transition_function_calls:
            return
        function_calls = self._deferred_node_transition_function_calls
        self._deferred_node_transition_function_calls = []
        logger.debug(
            f"{self}: executing {len(function_calls)} deferred workflow-control "
            "call(s) after bot turn ended"
        )
        await self.run_function_calls(function_calls)

    async def _handle_evt_function_call_arguments_done(self, evt):
        """Run ordinary tools immediately and defer workflow-control calls."""
        try:
            args = json.loads(evt.arguments)

            function_call_item = self._pending_function_calls.get(evt.call_id)
            if function_call_item:
                del self._pending_function_calls[evt.call_id]

                function_calls = [
                    FunctionCallFromLLM(
                        context=self._context,
                        tool_call_id=evt.call_id,
                        function_name=function_call_item.name,
                        arguments=args,
                    )
                ]

                is_node_transition = self._function_is_node_transition(
                    function_call_item.name
                )
                if self._bot_is_speaking and is_node_transition:
                    self._deferred_node_transition_function_calls.extend(function_calls)
                    logger.debug(
                        f"{self}: deferring workflow-control call "
                        f"{function_call_item.name} "
                        "until bot stops speaking"
                    )
                else:
                    await self.run_function_calls(function_calls)
                    logger.debug(f"Processed function call: {function_call_item.name}")
            else:
                logger.warning(
                    f"No tracked function call found for call_id: {evt.call_id}"
                )
                logger.warning(
                    f"Available pending calls: {list(self._pending_function_calls.keys())}"
                )

        except Exception as e:
            logger.error(f"Failed to process function call arguments: {e}")

    # ------------------------------------------------------------------
    # Transcription: broadcast with finalized=True for every
    # completed-transcription event from OpenAI.
    # ------------------------------------------------------------------

    async def handle_evt_input_audio_transcription_completed(self, evt):
        await self._call_event_handler(
            "on_conversation_item_updated", evt.item_id, None
        )
        await self.broadcast_frame(
            TranscriptionFrame,
            text=evt.transcript,
            user_id="",
            timestamp=time_now_iso8601(),
            result=evt,
            finalized=True,
        )
        await self._handle_user_transcription(evt.transcript, True, Language.EN)
