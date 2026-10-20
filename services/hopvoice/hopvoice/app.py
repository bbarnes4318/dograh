from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .audio import float_to_pcm_s16le, pcm_chunks, resample
from .backends import build_backend
from .config import Settings
from .voices import VoiceRegistry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hopvoice")


class SpeechRequest(BaseModel):
    model: str = "native-fast"
    input: str = Field(min_length=1, max_length=12000)
    voice: str = Field(min_length=1, max_length=128)
    response_format: str = "pcm"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    instructions: str | None = Field(default=None, max_length=2000)


@dataclass
class RuntimeMetrics:
    requests_total: int = 0
    failures_total: int = 0
    cancelled_total: int = 0
    generated_audio_seconds: float = 0.0
    generation_seconds: float = 0.0
    inflight: int = 0


def _authorized(expected: str | None, authorization: str | None) -> bool:
    if not expected:
        return True
    return authorization == f"Bearer {expected}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    voices = VoiceRegistry(settings.voices_file)
    backend = build_backend(settings, voices)
    metrics = RuntimeMetrics()
    gate = asyncio.Semaphore(settings.max_concurrency)

    async def synthesize(
        *, text: str, voice_id: str, instructions: str | None, sample_rate: int
    ) -> tuple[bytes, float, float]:
        started = time.perf_counter()
        metrics.requests_total += 1
        metrics.inflight += 1
        try:
            async with gate:
                result = await asyncio.to_thread(
                    backend.synthesize,
                    text=text,
                    voice_id=voice_id,
                    instructions=instructions,
                )
            audio = resample(result.audio, result.sample_rate, sample_rate)
            pcm = float_to_pcm_s16le(audio)
            generation_seconds = time.perf_counter() - started
            audio_seconds = len(audio) / sample_rate
            metrics.generation_seconds += generation_seconds
            metrics.generated_audio_seconds += audio_seconds
            return pcm, generation_seconds, audio_seconds
        except asyncio.CancelledError:
            metrics.cancelled_total += 1
            raise
        except Exception:
            metrics.failures_total += 1
            raise
        finally:
            metrics.inflight -= 1

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.preload:
            logger.info("preloading Hopvoice backend=%s profile=%s", settings.backend, settings.profile)
            await asyncio.to_thread(backend.load)
        yield

    app = FastAPI(title="Hopwhistle Native Voice Engine", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.voices = voices
    app.state.backend = backend
    app.state.metrics = metrics

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "backend": settings.backend,
            "profile": settings.profile,
            "model_id": settings.model_id,
            "loaded": backend.loaded,
            "voices": len(voices.ids()),
            "inflight": metrics.inflight,
        }

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)):
        if not _authorized(settings.api_key, authorization):
            raise HTTPException(status_code=401, detail="unauthorized")
        return {
            "object": "list",
            "data": [
                {
                    "id": settings.profile,
                    "object": "model",
                    "owned_by": "hopwhistle",
                    "backend_model": settings.model_id,
                }
            ],
        }

    @app.get("/v1/voices")
    async def voice_catalog(authorization: str | None = Header(default=None)):
        if not _authorized(settings.api_key, authorization):
            raise HTTPException(status_code=401, detail="unauthorized")
        return {"voices": voices.public_catalog()}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus_metrics():
        lines = [
            "# TYPE hopvoice_requests_total counter",
            f"hopvoice_requests_total {metrics.requests_total}",
            "# TYPE hopvoice_failures_total counter",
            f"hopvoice_failures_total {metrics.failures_total}",
            "# TYPE hopvoice_cancelled_total counter",
            f"hopvoice_cancelled_total {metrics.cancelled_total}",
            "# TYPE hopvoice_inflight gauge",
            f"hopvoice_inflight {metrics.inflight}",
            "# TYPE hopvoice_generated_audio_seconds_total counter",
            f"hopvoice_generated_audio_seconds_total {metrics.generated_audio_seconds:.6f}",
            "# TYPE hopvoice_generation_seconds_total counter",
            f"hopvoice_generation_seconds_total {metrics.generation_seconds:.6f}",
        ]
        return "\n".join(lines) + "\n"

    @app.post("/v1/audio/speech")
    async def audio_speech(
        request: SpeechRequest,
        authorization: str | None = Header(default=None),
    ):
        if not _authorized(settings.api_key, authorization):
            raise HTTPException(status_code=401, detail="unauthorized")
        if request.model not in {settings.profile, settings.model_id}:
            raise HTTPException(
                status_code=400,
                detail=f"this worker serves only model/profile '{settings.profile}'",
            )
        if request.response_format != "pcm":
            raise HTTPException(status_code=400, detail="v0.1 supports response_format=pcm only")
        if request.speed != 1.0:
            logger.warning(
                "speed=%s requested; Qwen baseline does not yet apply time-scale modification",
                request.speed,
            )
        try:
            pcm, generation_seconds, audio_seconds = await synthesize(
                text=request.input.strip(),
                voice_id=request.voice,
                instructions=request.instructions,
                sample_rate=settings.output_sample_rate,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("speech generation failed")
            raise HTTPException(status_code=500, detail="speech generation failed") from exc

        return StreamingResponse(
            pcm_chunks(pcm, settings.output_sample_rate, settings.audio_chunk_ms),
            media_type="application/octet-stream",
            headers={
                "X-Hopvoice-Sample-Rate": str(settings.output_sample_rate),
                "X-Hopvoice-Generation-Ms": f"{generation_seconds * 1000.0:.1f}",
                "X-Hopvoice-Audio-Ms": f"{audio_seconds * 1000.0:.1f}",
                "X-Hopvoice-Profile": settings.profile,
            },
        )

    @app.websocket("/v1/tts/ws")
    async def tts_ws(websocket: WebSocket):
        authorization = websocket.headers.get("authorization")
        if not _authorized(settings.api_key, authorization):
            await websocket.close(code=4401, reason="unauthorized")
            return

        await websocket.accept()
        session: dict | None = None
        active_task: asyncio.Task | None = None
        active_turn_id: str | None = None

        async def generate_turn(message: dict) -> None:
            nonlocal active_turn_id
            turn_id = str(message.get("turn_id") or "")
            if not turn_id:
                await websocket.send_json({"type": "error", "message": "turn_id is required"})
                return
            active_turn_id = turn_id
            text = str(message.get("text") or "").strip()
            if not text:
                await websocket.send_json(
                    {"type": "error", "turn_id": turn_id, "message": "text is required"}
                )
                return
            delivery = message.get("delivery") or {}
            instructions = delivery.get("instructions") or message.get("instructions")
            try:
                await websocket.send_json({"type": "turn_started", "turn_id": turn_id})
                pcm, generation_seconds, audio_seconds = await synthesize(
                    text=text,
                    voice_id=session["voice_id"],
                    instructions=instructions,
                    sample_rate=session["sample_rate"],
                )
                await websocket.send_json(
                    {
                        "type": "audio_meta",
                        "turn_id": turn_id,
                        "generation_ms": round(generation_seconds * 1000.0, 1),
                        "generated_audio_ms": round(audio_seconds * 1000.0, 1),
                        "sample_rate": session["sample_rate"],
                        "encoding": "pcm_s16le",
                    }
                )
                for chunk in pcm_chunks(
                    pcm, session["sample_rate"], settings.audio_chunk_ms
                ):
                    await websocket.send_bytes(chunk)
                    await asyncio.sleep(0)
                await websocket.send_json({"type": "turn_done", "turn_id": turn_id})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("websocket speech generation failed")
                await websocket.send_json(
                    {"type": "error", "turn_id": turn_id, "message": str(exc)}
                )
            finally:
                if active_turn_id == turn_id:
                    active_turn_id = None

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "invalid JSON"})
                    continue

                msg_type = message.get("type")
                if msg_type == "start":
                    if session is not None:
                        await websocket.send_json({"type": "error", "message": "session already started"})
                        continue
                    model = str(message.get("model") or settings.profile)
                    if model not in {settings.profile, settings.model_id}:
                        await websocket.send_json(
                            {"type": "error", "message": f"worker serves only '{settings.profile}'"}
                        )
                        continue
                    output = message.get("output") or {}
                    encoding = output.get("encoding", "pcm_s16le")
                    sample_rate = int(output.get("sample_rate", settings.output_sample_rate))
                    channels = int(output.get("channels", 1))
                    if encoding != "pcm_s16le" or channels != 1:
                        await websocket.send_json(
                            {"type": "error", "message": "v0.1 requires mono pcm_s16le"}
                        )
                        continue
                    if sample_rate not in {8000, 16000, 24000, 48000}:
                        await websocket.send_json(
                            {"type": "error", "message": "unsupported output sample rate"}
                        )
                        continue
                    session = {
                        "session_id": str(message.get("session_id") or ""),
                        "voice_id": str(message.get("voice_id") or ""),
                        "sample_rate": sample_rate,
                    }
                    if not session["session_id"] or not session["voice_id"]:
                        session = None
                        await websocket.send_json(
                            {"type": "error", "message": "session_id and voice_id are required"}
                        )
                        continue
                    await websocket.send_json(
                        {
                            "type": "ready",
                            "protocol_version": 1,
                            "session_id": session["session_id"],
                            "profile": settings.profile,
                            "backend_model": settings.model_id,
                        }
                    )
                elif msg_type == "text":
                    if session is None:
                        await websocket.send_json({"type": "error", "message": "start is required"})
                        continue
                    if active_task and not active_task.done():
                        await websocket.send_json(
                            {"type": "error", "message": "v0.1 permits one active turn per session"}
                        )
                        continue
                    active_turn_id = str(message.get("turn_id") or "")
                    active_task = asyncio.create_task(generate_turn(message))
                elif msg_type == "cancel_turn":
                    turn_id = str(message.get("turn_id") or "")
                    if active_task and not active_task.done() and turn_id == active_turn_id:
                        active_task.cancel()
                        await websocket.send_json({"type": "cancelled", "turn_id": turn_id})
                    else:
                        await websocket.send_json({"type": "cancelled", "turn_id": turn_id, "active": False})
                elif msg_type == "end":
                    if active_task and not active_task.done():
                        active_task.cancel()
                    await websocket.send_json({"type": "ended"})
                    await websocket.close(code=1000)
                    return
                else:
                    await websocket.send_json({"type": "error", "message": "unknown message type"})
        except WebSocketDisconnect:
            if active_task and not active_task.done():
                active_task.cancel()

    return app


app = create_app()
