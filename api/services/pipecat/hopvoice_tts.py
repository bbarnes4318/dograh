"""Pipecat adapter for Hopwhistle's first-party Hopvoice inference service.

This lives in the AI Voice runtime rather than the inference container. One
persistent WebSocket is opened for the life of the call. Individual aggregated
TTS chunks use separate remote turn IDs so Hopvoice can serialize them without
losing Pipecat's downstream ordering. A caller interruption sends ``cancel_all``
so queued, not-yet-spoken chunks cannot leak out after barge-in.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.services.settings import NOT_GIVEN, TTSSettings, _NotGiven
from pipecat.services.tts_service import TextAggregationMode, WebsocketTTSService
from pipecat.utils.tracing.service_decorators import traced_tts

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.protocol import State
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("Hopvoice TTS requires the 'websockets' package") from exc


def _websocket_url(endpoint: str) -> str:
    """Normalize a Hopvoice HTTP/base URL to its persistent TTS WebSocket."""
    endpoint = endpoint.strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError("Hopvoice endpoint must be an absolute http(s) or ws(s) URL")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    if not path.endswith("/v1/tts/ws"):
        path = f"{path}/v1/tts/ws" if path else "/v1/tts/ws"
    return urlunparse(
        parsed._replace(scheme=scheme, path=path, params="", query="", fragment="")
    )


@dataclass
class HopvoiceTTSSettings(TTSSettings):
    """Runtime-updatable Hopvoice settings.

    ``instructions`` is delivery-only guidance. The inference service must never
    rewrite factual text based on this field.
    """

    instructions: str | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)


class HopvoiceTTSService(WebsocketTTSService):
    """Persistent WebSocket TTS adapter for Hopvoice."""

    Settings = HopvoiceTTSSettings

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        session_id: str | None = None,
        sample_rate: int | None = None,
        settings: HopvoiceTTSSettings | None = None,
        text_aggregation_mode: TextAggregationMode | None = None,
        connect_timeout_s: float = 5.0,
        **kwargs,
    ):
        defaults = HopvoiceTTSSettings(
            model="native-fast",
            voice="default",
            language="en",
            instructions=None,
        )
        if settings is not None:
            defaults.apply_update(settings)

        super().__init__(
            text_aggregation_mode=text_aggregation_mode,
            push_start_frame=True,
            push_stop_frames=True,
            pause_frame_processing=False,
            sample_rate=sample_rate,
            settings=defaults,
            # Each aggregated sentence/chunk is an independent remote job. The
            # server serializes them and Pipecat serializes their audio contexts.
            reuse_context_id_within_turn=False,
            **kwargs,
        )
        self._endpoint = _websocket_url(endpoint)
        self._api_key = api_key
        self._session_id = session_id or str(uuid.uuid4())
        self._connect_timeout_s = connect_timeout_s
        self._receive_task = None
        self._receiving_context_id: str | None = None

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def _connect(self):
        await super()._connect()
        if self._websocket and self._websocket.state is State.OPEN:
            return

        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        logger.debug(f"Connecting to Hopvoice TTS at {self._endpoint}")
        try:
            self._websocket = await websocket_connect(
                self._endpoint,
                additional_headers=headers or None,
            )
            await self._websocket.send(
                json.dumps(
                    {
                        "type": "start",
                        "protocol_version": 1,
                        "session_id": self._session_id,
                        "model": self._settings.model,
                        "voice_id": self._settings.voice,
                        "output": {
                            "encoding": "pcm_s16le",
                            "sample_rate": self.sample_rate,
                            "channels": 1,
                        },
                    }
                )
            )
            raw = await asyncio.wait_for(
                self._websocket.recv(), timeout=self._connect_timeout_s
            )
            if isinstance(raw, bytes):
                raise RuntimeError("Hopvoice sent audio before the ready event")
            ready = json.loads(raw)
            if ready.get("type") != "ready":
                raise RuntimeError(f"Hopvoice did not accept session: {ready}")

            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )
            await self._call_event_handler("on_connected")
            logger.info(
                f"Connected to Hopvoice profile={self._settings.model} session={self._session_id}"
            )
        except Exception as exc:
            await self._call_event_handler("on_connection_error", str(exc))
            if self._websocket:
                await self._websocket.close()
            self._websocket = None
            raise

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        if self._websocket:
            try:
                if self._websocket.state is State.OPEN:
                    await self._websocket.send(json.dumps({"type": "end"}))
            except Exception as exc:  # noqa: BLE001 - disconnect is best-effort
                logger.debug(f"Hopvoice end message failed during disconnect: {exc}")
            try:
                await self._websocket.close()
            finally:
                self._websocket = None
        self._receiving_context_id = None
        await self.stop_all_metrics()
        await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return self._websocket
        raise RuntimeError("Hopvoice WebSocket is not connected")

    async def _receive_messages(self):
        async for raw in self._get_websocket():
            if isinstance(raw, bytes):
                context_id = self._receiving_context_id
                if context_id and self.audio_context_available(context_id):
                    await self.stop_ttfb_metrics()
                    await self.append_to_audio_context(
                        context_id,
                        TTSAudioRawFrame(
                            raw,
                            self.sample_rate,
                            1,
                            context_id=context_id,
                        ),
                    )
                continue

            message = json.loads(raw)
            msg_type = message.get("type")
            context_id = message.get("turn_id")

            if msg_type in {"cancelled", "cancelled_all"}:
                continue
            if msg_type == "turn_started":
                self._receiving_context_id = context_id
                continue
            if msg_type == "audio_meta":
                self._receiving_context_id = context_id
                continue
            if msg_type == "turn_done":
                await self.stop_ttfb_metrics()
                if context_id and self.audio_context_available(context_id):
                    await self.append_to_audio_context(
                        context_id, TTSStoppedFrame(context_id=context_id)
                    )
                    await self.remove_audio_context(context_id)
                if self._receiving_context_id == context_id:
                    self._receiving_context_id = None
                continue
            if msg_type == "error":
                error = message.get("message", "unknown Hopvoice error")
                if context_id and self.audio_context_available(context_id):
                    await self.append_to_audio_context(
                        context_id, ErrorFrame(error=f"Hopvoice TTS: {error}")
                    )
                    await self.remove_audio_context(context_id)
                else:
                    await self.push_error(error_msg=f"Hopvoice TTS: {error}")
                continue
            if msg_type == "ended":
                return

            logger.warning(f"Ignoring unknown Hopvoice event: {message}")

    @traced_tts
    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        if not self._websocket or self._websocket.state is State.CLOSED:
            await self._connect()

        message = {
            "type": "text",
            "turn_id": context_id,
            "text": text,
        }
        instructions = self._settings.instructions
        if isinstance(instructions, str) and instructions.strip():
            message["delivery"] = {"instructions": instructions.strip()}

        try:
            await self._get_websocket().send(json.dumps(message))
            await self.start_tts_usage_metrics(text)
            yield None
        except Exception as exc:
            yield ErrorFrame(error=f"Hopvoice TTS request failed: {exc}")
            yield TTSStoppedFrame(context_id=context_id)

    async def on_audio_context_interrupted(self, context_id: str):
        """Drop active + queued server speech on caller barge-in."""
        await self.stop_all_metrics()
        if self._websocket and self._websocket.state is State.OPEN:
            try:
                await self._websocket.send(json.dumps({"type": "cancel_all"}))
            except Exception as exc:  # noqa: BLE001 - local interruption must continue
                logger.warning(
                    f"Unable to cancel Hopvoice queue on interruption: {exc}"
                )
        self._receiving_context_id = None
        await super().on_audio_context_interrupted(context_id)

    async def flush_audio(self, context_id: str | None = None):
        # Each Hopvoice submission is already a self-contained remote context.
        # Pipecat's sentence aggregator defines the flush boundary for v0.2.
        return
