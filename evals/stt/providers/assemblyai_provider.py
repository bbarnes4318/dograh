"""AssemblyAI Streaming v3 provider (wss://streaming.assemblyai.com/v3/ws).

Streams the file at real time (AssemblyAI rejects faster-than-real-time
audio), lets AssemblyAI's semantic end of turn decide turns, then sends
``ForceEndpoint`` and ``Terminate`` and waits for ``Termination``.

Records in ``params``: time to first partial, time from end of audio to the
last final, number of end-of-turn events (for false-endpoint counting), the
model the server reported in ``Begin``, billed session seconds and errors.

Environment:
  ASSEMBLYAI_API_KEY   raw key, sent in the Authorization header (never in the URL)
  ASSEMBLYAI_MODEL     default assemblyai-stt-3-7-realtime-agent
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from ..audio_streamer import AudioConfig, AudioStreamer
from .base import EventCallback, STTProvider, TranscriptionResult, Word

try:
    from websockets.asyncio.client import connect as websocket_connect
except ImportError:
    raise ImportError("websockets required: pip install websockets")

URL = "wss://streaming.assemblyai.com/v3/ws"
DEFAULT_MODEL = "assemblyai-stt-3-7-realtime-agent"


def language_codes(language: str) -> list[str]:
    if language in ("multi", "auto"):
        return ["en", "es"]
    return [language.split("-")[0]]


class AssemblyAIProvider(STTProvider):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("ASSEMBLYAI_API_KEY")
        if not self.api_key:
            raise ValueError("ASSEMBLYAI_API_KEY is required")

    @property
    def name(self) -> str:
        return "assemblyai"

    async def transcribe(
        self,
        audio_path: Path,
        diarize: bool = False,
        keyterms: list[str] | None = None,
        on_event: EventCallback | None = None,
        model: str | None = None,
        language: str = "en",
        sample_rate: int = 8000,
        mode: str = "balanced",
        voice_focus: str | None = "near-field",
        agent_context: str | None = None,
        trailing_silence_seconds: float = 1.5,
        **kwargs: Any,
    ) -> TranscriptionResult:
        model = model or os.getenv("ASSEMBLYAI_MODEL", DEFAULT_MODEL)
        query: dict[str, Any] = {
            "speech_model": model,
            "sample_rate": sample_rate,
            "encoding": "pcm_s16le",
            "format_turns": "true",
            "mode": mode,
            "language_codes": json.dumps(language_codes(language)),
            "language_detection": "true",
        }
        if voice_focus:
            query["voice_focus"] = voice_focus
        if keyterms:
            query["keyterms_prompt"] = json.dumps(keyterms[:100])
        if agent_context:
            query["agent_context"] = agent_context[-1500:]
        if diarize:
            query["speaker_labels"] = "true"

        # AssemblyAI recommends 50-100 ms chunks.
        streamer = AudioStreamer(AudioConfig(sample_rate=sample_rate, chunk_duration_ms=50))
        finals: list[dict[str, Any]] = []
        words: list[Word] = []
        partials = 0
        end_of_turns = 0
        started = time.monotonic()
        first_partial_ms = None
        audio_done_at: float | None = None
        last_final_after_audio_ms = None
        begin: dict[str, Any] = {}
        termination: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []

        try:
            async with websocket_connect(
                f"{URL}?{urlencode(query)}",
                additional_headers={"Authorization": self.api_key},
            ) as ws:

                async def send_audio():
                    nonlocal audio_done_at
                    full = streamer.config.chunk_size
                    async for chunk in streamer.stream_file(
                        audio_path, trailing_silence_seconds=trailing_silence_seconds
                    ):
                        # Chunks must hold >= 20 ms; pad the file's last one.
                        await ws.send(chunk + bytes(max(full - len(chunk), 0)))
                    audio_done_at = time.monotonic()
                    await ws.send(json.dumps({"type": "ForceEndpoint"}))
                    await asyncio.sleep(0.5)
                    await ws.send(json.dumps({"type": "Terminate"}))

                async def receive():
                    nonlocal partials, end_of_turns, first_partial_ms, last_final_after_audio_ms
                    async for message in ws:
                        data = json.loads(message)
                        kind = data.get("type")
                        if on_event:
                            on_event(kind or "?", data)
                        if kind == "Begin":
                            begin.update(data)
                        elif kind == "Turn":
                            final = data.get("end_of_turn") and data.get("turn_is_formatted")
                            if final:
                                if not data.get("transcript"):
                                    continue
                                end_of_turns += 1
                                finals.append(
                                    {
                                        "turn_order": data.get("turn_order"),
                                        "transcript": data.get("transcript"),
                                        "end_of_turn_confidence": data.get(
                                            "end_of_turn_confidence"
                                        ),
                                        "language_code": data.get("language_code"),
                                    }
                                )
                                for w in data.get("words") or []:
                                    words.append(
                                        Word(
                                            word=w.get("text", ""),
                                            start=(w.get("start") or 0) / 1000,
                                            end=(w.get("end") or 0) / 1000,
                                            confidence=w.get("confidence") or 0.0,
                                            speaker=data.get("speaker_label"),
                                        )
                                    )
                                if audio_done_at:
                                    last_final_after_audio_ms = (
                                        time.monotonic() - audio_done_at
                                    ) * 1000
                            elif data.get("transcript"):
                                partials += 1
                                if first_partial_ms is None:
                                    first_partial_ms = (time.monotonic() - started) * 1000
                        elif kind == "Error":
                            errors.append(data)
                        elif kind == "Termination":
                            termination.update(data)
                            return

                receive_task = asyncio.create_task(receive())
                await send_audio()
                try:
                    await asyncio.wait_for(receive_task, timeout=10.0)
                except asyncio.TimeoutError:
                    receive_task.cancel()
        except Exception as e:
            logger.exception(e)
            errors.append({"error": f"{type(e).__name__}: {e}"})

        stored = {k: v for k, v in query.items() if k != "agent_context"}
        stored.update(
            {
                "model_served": (begin.get("configuration") or {}).get("model"),
                "partial_count": partials,
                "end_of_turn_count": end_of_turns,
                "time_to_first_partial_ms": first_partial_ms,
                "last_final_after_audio_end_ms": last_final_after_audio_ms,
                "billed_session_seconds": termination.get("session_duration_seconds"),
                "errors": errors,
            }
        )
        return TranscriptionResult(
            provider=self.name,
            transcript=" ".join(f["transcript"] for f in finals).strip(),
            words=words,
            speakers=sorted({w.speaker for w in words if w.speaker}),
            duration=float(termination.get("audio_duration_seconds") or 0.0),
            raw_response={"finals": finals},
            params=stored,
        )
