"""HopSTT (self-hosted) STT provider: raw WebSocket client per services/hopstt/PROTOCOL.md §2.

Streams the file at real time in 80 ms chunks (like the other providers), sends
``finalize`` at the end of the audio and then ``end_of_stream``, and collects
finals. Also records time-to-final after ``finalize`` in ``params``.

Environment:
  HOPSTT_ENDPOINT  base URL (default http://localhost:8790)
  HOPSTT_API_KEY   bearer token (optional)
  HOPSTT_MODEL     model id (default nemotron-3.5-streaming)
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from ..audio_streamer import AudioConfig, AudioStreamer
from .base import EventCallback, STTProvider, TranscriptionResult

try:
    from websockets.asyncio.client import connect as websocket_connect
except ImportError:
    raise ImportError("websockets required: pip install websockets")


def _stream_url(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint.startswith("https://"):
        endpoint = "wss://" + endpoint[len("https://") :]
    elif endpoint.startswith("http://"):
        endpoint = "ws://" + endpoint[len("http://") :]
    if not endpoint.endswith("/v1/stt/stream"):
        endpoint += "/v1/stt/stream"
    return endpoint


class HopSTTProvider(STTProvider):
    """Hopwhistle self-hosted streaming STT."""

    def __init__(self, endpoint: str | None = None, api_key: str | None = None):
        self.endpoint = endpoint or os.getenv("HOPSTT_ENDPOINT", "http://localhost:8790")
        self.api_key = api_key or os.getenv("HOPSTT_API_KEY") or None

    @property
    def name(self) -> str:
        return "hopstt"

    async def transcribe(
        self,
        audio_path: Path,
        diarize: bool = False,
        keyterms: list[str] | None = None,
        on_event: EventCallback | None = None,
        model: str | None = None,
        language: str = "en",
        sample_rate: int = 8000,
        chunk_ms: int = 320,
        trailing_silence_seconds: float = 1.0,
        **kwargs: Any,
    ) -> TranscriptionResult:
        model = model or os.getenv("HOPSTT_MODEL", "nemotron-3.5-streaming")
        params: dict[str, Any] = {
            "type": "config",
            "model": model,
            "language": language,
            "sample_rate": sample_rate,
            "encoding": "pcm_s16le",
            "chunk_ms": chunk_ms,
            "interim_results": True,
            "vad_events": False,
            "endpointing": {"mode": "client", "silence_ms": 500},
            "session_id": uuid.uuid4().hex,
            "generation": 0,
            "stream_offset_ms": 0,
            "correlation_id": "evals",
            "keyterms": keyterms or [],
        }
        streamer = AudioStreamer(AudioConfig(sample_rate=sample_rate))
        finals: list[str] = []
        interims = 0
        finalize_sent_at: float | None = None
        time_to_final_ms: float | None = None
        duration = 0.0
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None

        try:
            async with websocket_connect(_stream_url(self.endpoint), additional_headers=headers) as ws:
                await ws.send(json.dumps(params))
                ready = json.loads(await ws.recv())
                if on_event:
                    on_event(ready.get("type", "?"), ready)
                if ready.get("type") != "ready":
                    raise RuntimeError(f"HopSTT refused the stream: {ready}")

                async def send_audio():
                    nonlocal finalize_sent_at
                    async for chunk in streamer.stream_file(
                        audio_path, trailing_silence_seconds=trailing_silence_seconds
                    ):
                        await ws.send(chunk)
                    finalize_sent_at = time.monotonic()
                    await ws.send(json.dumps({"type": "finalize"}))
                    await ws.send(json.dumps({"type": "end_of_stream"}))

                async def receive():
                    nonlocal interims, time_to_final_ms, duration
                    async for message in ws:
                        if not isinstance(message, str):
                            continue
                        data = json.loads(message)
                        if on_event:
                            on_event(data.get("type", "?"), data)
                        if data.get("type") == "transcript":
                            if data.get("is_final"):
                                finals.append(data.get("text", ""))
                                duration = max(duration, (data.get("end_ms") or 0) / 1000)
                                if data.get("from_finalize") and finalize_sent_at:
                                    time_to_final_ms = (time.monotonic() - finalize_sent_at) * 1000
                            else:
                                interims += 1
                        elif data.get("type") == "error":
                            raise RuntimeError(f"HopSTT error: {data.get('code')}")
                        elif data.get("type") == "closed":
                            return

                receive_task = asyncio.create_task(receive())
                await send_audio()
                try:
                    await asyncio.wait_for(receive_task, timeout=10.0)
                except asyncio.TimeoutError:
                    receive_task.cancel()
        except Exception as e:
            logger.exception(e)

        stored = {k: v for k, v in params.items() if k not in ("type", "session_id")}
        stored["interim_count"] = interims
        stored["time_to_final_after_finalize_ms"] = time_to_final_ms
        return TranscriptionResult(
            provider=self.name,
            transcript=" ".join(t for t in finals if t).strip(),
            words=[],
            speakers=[],
            duration=duration,
            raw_response={"finals": finals},
            params=stored,
        )
