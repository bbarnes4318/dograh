"""AssemblyAI streaming STT for Dograh: the fork's service plus what calls need.

The pinned pipecat fork already speaks Streaming v3 (``agent_context``,
``UpdateConfiguration``, ``mode``, ``voice_focus``, keyterms, ``ForceEndpoint``,
``Terminate``). This subclass adds what Dograh's telephony runtime needs on top,
without forking pipecat again:

* **Model family.** The fork gates its voice-agent features on the model-name
  prefixes ``u3-rt-pro`` / ``universal-3-5-pro``. AssemblyAI's current models
  (``assemblyai-stt-3-7-realtime-agent``, ``universal-3-6-pro``) carry the same
  feature set under new names; without registering them, ``agent_context``,
  ``mode`` and ``voice_focus`` would be silently dropped and the wrong turn
  defaults applied. ``register_voice_agent_models`` adds them to the fork's
  family tuple (read at call time by ``is_u3_pro_model``).
* **Languages.** Sends ``language_codes`` (JSON array). The fork sends the
  singular ``language_code``, which the v3 API ignores.
* **Errors.** Handles the v3 ``Error`` message (the fork treats it as an unknown
  type), stops reconnecting on non-retryable auth/billing errors, and ignores
  ``Heartbeat`` and other informational messages.
* **Begin check.** Warns when the server's ``configuration.model`` differs from
  the requested model (AssemblyAI ignores unknown parameters silently).
* **Turns.** A turn is finalised once: a repeated final for the same
  ``turn_order`` and partials for a turn that is already final are dropped, and
  identical consecutive partials are not re-emitted.
* **Backpressure.** While the socket is down, at most ``max_offline_buffer_ms``
  of the newest audio is kept. Older caller audio is discarded rather than
  replayed after a reconnect, so stale speech can never produce a late turn.
* **Encoding.** Pipeline audio is always 16-bit PCM (telephony serializers
  decode μ-law first), so ``pcm_s16le`` at the transport rate is the default and
  needs no conversion. ``pcm_mulaw`` re-encodes it and sizes chunks per byte.
* **Context.** The assistant's completed reply becomes ``agent_context`` with a
  short neutral hint (workflow step, expected reply type, language).
* **Usage.** Billed seconds (``Termination.session_duration_seconds``, or
  wall-clock when a connection ends without one) and errors feed
  :class:`AssemblyAIUsageSession`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger
from websockets.protocol import State

from api.services.pipecat.assemblyai_context import compose_agent_context
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw
from pipecat.frames.frames import CancelFrame, EndFrame, Frame, StartFrame
from pipecat.services.assemblyai import stt as _fork_stt
from pipecat.services.assemblyai.models import TurnMessage
from pipecat.services.assemblyai.stt import AssemblyAISTTService

# Model-name prefixes with AssemblyAI's voice-agent feature set.
VOICE_AGENT_MODEL_PREFIXES = (
    "assemblyai-stt-3-7-realtime-agent",
    "universal-3-6-pro",
)

# Close/error codes after which reconnecting cannot succeed unchanged.
_NON_RETRYABLE_CODES = {1008, 3006}
_INFORMATIONAL_TYPES = {"Heartbeat", "LLMGatewayResponse", "SpeakerRevision"}


def register_voice_agent_models() -> None:
    """Add AssemblyAI's current voice-agent models to the fork's U3 family."""
    current = tuple(getattr(_fork_stt, "U3_PRO_MODEL_PREFIXES", ()))
    missing = tuple(p for p in VOICE_AGENT_MODEL_PREFIXES if p not in current)
    if missing:
        _fork_stt.U3_PRO_MODEL_PREFIXES = current + missing


register_voice_agent_models()


def is_voice_agent_model(model: str | None) -> bool:
    return _fork_stt.is_u3_pro_model(model)


class DograhAssemblyAISTTService(AssemblyAISTTService):
    """AssemblyAI v3 streaming STT tuned for Dograh phone calls."""

    def __init__(
        self,
        *,
        api_key: str,
        encoding: str = "pcm_s16le",
        language_codes: list[str] | None = None,
        agent_context_enabled: bool = True,
        agent_context_hints: bool = True,
        language_hint: str | None = None,
        node_name_provider: Callable[[], str | None] | None = None,
        usage=None,
        max_offline_buffer_ms: int = 500,
        chunk_ms: int = 50,
        **kwargs,
    ):
        if encoding not in ("pcm_s16le", "pcm_mulaw"):
            raise ValueError(f"unsupported AssemblyAI encoding {encoding!r}")
        super().__init__(api_key=api_key, encoding=encoding, **kwargs)
        self._language_codes = list(language_codes) if language_codes else None
        self._agent_context_enabled = agent_context_enabled
        self._agent_context_hints = agent_context_hints
        self._language_hint = language_hint
        self._node_name_provider = node_name_provider
        self._usage = usage
        self._max_offline_buffer_ms = max(int(max_offline_buffer_ms), 0)
        self._chunk_size_ms = chunk_ms
        self._ulaw_resampler = (
            create_stream_resampler() if encoding == "pcm_mulaw" else None
        )
        self._requested_model = self._settings.model
        self._last_final_turn_order = -1
        self._last_interim: tuple[int, str] | None = None
        self._connection_started_at: float | None = None
        self._dropped_offline_bytes = 0
        self.last_provider_error: dict | None = None
        self.begin_configuration: dict | None = None

    # ------------------------------------------------------------- wiring

    def set_node_name_provider(self, provider: Callable[[], str | None] | None) -> None:
        self._node_name_provider = provider

    @property
    def _bytes_per_sample(self) -> int:
        return 1 if self._encoding == "pcm_mulaw" else 2

    def _bytes_for_ms(self, ms: int) -> int:
        return int(ms * self.sample_rate * self._bytes_per_sample / 1000)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._chunk_size_bytes = self._bytes_for_ms(self._chunk_size_ms)

    async def stop(self, frame: EndFrame):
        try:
            await super().stop(frame)
        finally:
            await self._finish_usage()

    async def cancel(self, frame: CancelFrame):
        try:
            await super().cancel(frame)
        finally:
            await self._finish_usage()

    async def _finish_usage(self) -> None:
        self._account_unterminated_connection()
        if self._usage is not None:
            await self._usage.finish()

    # ------------------------------------------------------------- request

    def _build_ws_url(self) -> str:
        url = super()._build_ws_url()
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "language_code"]
        if self._language_codes:
            query.append(("language_codes", json.dumps(self._language_codes)))
        return urlunsplit(parts._replace(query=urlencode(query)))

    # --------------------------------------------------------------- audio

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self._encoding == "pcm_mulaw":
            audio = await pcm_to_ulaw(
                audio, self.sample_rate, self.sample_rate, self._ulaw_resampler
            )
        self._audio_buffer.extend(audio)

        if not (self._websocket and self._websocket.state is State.OPEN):
            limit = self._bytes_for_ms(self._max_offline_buffer_ms)
            overflow = len(self._audio_buffer) - limit
            if overflow > 0:
                del self._audio_buffer[:overflow]
                self._dropped_offline_bytes += overflow
            yield None
            return

        while (
            self._chunk_size_bytes and len(self._audio_buffer) >= self._chunk_size_bytes
        ):
            chunk = bytes(self._audio_buffer[: self._chunk_size_bytes])
            del self._audio_buffer[: self._chunk_size_bytes]
            try:
                await self._websocket.send(chunk)
            except Exception as e:
                logger.warning(f"{self}: send failed: {e}")
                break
        yield None

    # ---------------------------------------------------------- connection

    async def _connect_websocket(self):
        await super()._connect_websocket()
        if self._websocket is not None and self._connection_started_at is None:
            self._connection_started_at = time.monotonic()
            self._last_final_turn_order = -1
            self._last_interim = None
            if self._usage is not None:
                self._usage.connection_opened()
            if self._dropped_offline_bytes:
                logger.warning(
                    f"{self} discarded {self._dropped_offline_bytes} bytes of caller "
                    "audio buffered while disconnected (not replayed)"
                )
                self._dropped_offline_bytes = 0

    async def _disconnect_websocket(self):
        try:
            await super()._disconnect_websocket()
        finally:
            self._account_unterminated_connection()

    def _account_unterminated_connection(self) -> None:
        """Bill wall-clock time for a connection that ended with no Termination."""
        if self._connection_started_at is None:
            return
        elapsed = time.monotonic() - self._connection_started_at
        self._connection_started_at = None
        if self._usage is not None:
            self._usage.add_billed_seconds(elapsed)

    # ------------------------------------------------------------ messages

    async def _handle_message(self, message: dict[str, Any]):
        msg_type = message.get("type")
        if msg_type == "Error":
            await self._handle_provider_error(message)
            return
        if msg_type in _INFORMATIONAL_TYPES:
            return
        if msg_type == "Begin":
            self._check_begin(message)
        elif msg_type == "Termination":
            billed = message.get("session_duration_seconds")
            if self._connection_started_at is not None and isinstance(
                billed, (int, float)
            ):
                self._connection_started_at = None
                if self._usage is not None:
                    self._usage.add_billed_seconds(float(billed))
        await super()._handle_message(message)

    def _check_begin(self, message: dict[str, Any]) -> None:
        configuration = message.get("configuration") or {}
        self.begin_configuration = configuration
        served = configuration.get("model") or configuration.get("speech_model")
        if served and self._requested_model and served != self._requested_model:
            logger.warning(
                f"{self} AssemblyAI served model {served!r} but {self._requested_model!r} "
                "was requested (unknown parameters are ignored by the server)"
            )

    async def _handle_provider_error(self, message: dict[str, Any]) -> None:
        code = message.get("error_code")
        text = str(message.get("error") or "")[:300]
        self.last_provider_error = {"error_code": code, "error": text}
        if self._usage is not None:
            self._usage.record_error(code if code is not None else "unknown")
        retryable = not (
            isinstance(code, int)
            and code in _NON_RETRYABLE_CODES
            and "concurrent" not in text.lower()
        )
        if not retryable:
            # Bad key, no funds, disabled key or invalid request: reconnecting
            # with the same parameters would only repeat the failure.
            self._reconnect_on_error = False
        logger.error(f"{self} AssemblyAI error {code}: {text} (retryable={retryable})")
        await self.push_error(error_msg=f"AssemblyAI error {code}: {text}")

    async def _handle_transcription(self, message: TurnMessage):
        if not message.transcript:
            return
        if (
            self._language_codes
            and len(self._language_codes) == 1
            and not message.language_code
        ):
            # A single declared language labels the transcript (the base class
            # would otherwise default every frame to English).
            message = message.model_copy(
                update={
                    "language_code": self._language_codes[0],
                    "language_confidence": 1.0,
                }
            )
        is_final = message.end_of_turn and (
            not self._settings.format_turns or message.turn_is_formatted
        )
        if message.turn_order <= self._last_final_turn_order:
            logger.debug(
                f"{self} dropping {'final' if is_final else 'partial'} for already-final "
                f"turn {message.turn_order}"
            )
            return
        if is_final:
            self._last_final_turn_order = message.turn_order
            self._last_interim = None
        else:
            key = (message.turn_order, message.transcript)
            if key == self._last_interim:
                return
            self._last_interim = key
        await super()._handle_transcription(message)

    # -------------------------------------------------------------- context

    async def _process_assistant_turn(self, text: str) -> None:
        if not self._agent_context_enabled:
            return
        node_name = None
        if self._node_name_provider is not None:
            try:
                node_name = self._node_name_provider()
            except Exception:
                node_name = None
        composed = compose_agent_context(
            text,
            node_name=node_name,
            language=self._language_hint,
            include_hints=self._agent_context_hints,
        )
        if composed:
            await super()._process_assistant_turn(composed)
