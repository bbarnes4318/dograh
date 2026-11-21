"""One short, real AssemblyAI Streaming v3 session to prove the setup works.

Used by the superuser smoke-test endpoint and ``scripts/assemblyai_smoke.py``.
It authenticates with the raw key in the ``Authorization`` header (never in
the URL), checks that ``Begin`` reports the requested model, streams audio in
real time (AssemblyAI rejects faster-than-real-time audio), forces an
endpoint, terminates cleanly and reports latency and transcripts.

With no audio it streams one second of silence: enough to prove credentials,
connectivity and model selection. Platform-key runs go through the same
budget/stream admission as calls and are metered as product ``smoke_test``.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlencode

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from api.services.configuration.assemblyai import (
    ASSEMBLYAI_DEFAULT_MODEL,
    ASSEMBLYAI_STREAMING_URL,
    platform_assemblyai_api_key,
)
from api.services.pipecat.assemblyai_usage import (
    AssemblyAIUsageSession,
    admit_platform_session,
)

CHUNK_MS = 50


def build_url(
    *,
    model: str,
    sample_rate: int,
    encoding: str,
    language_codes: list[str] | None,
    keyterms: list[str] | None,
    mode: str | None,
    base_url: str = ASSEMBLYAI_STREAMING_URL,
) -> str:
    params: dict[str, str | int] = {
        "speech_model": model,
        "sample_rate": sample_rate,
        "encoding": encoding,
        "format_turns": "true",
    }
    if mode:
        params["mode"] = mode
    if language_codes:
        params["language_codes"] = json.dumps(language_codes)
    if keyterms:
        params["keyterms_prompt"] = json.dumps(keyterms)
    return f"{base_url}?{urlencode(params)}"


async def run_smoke_test(
    *,
    api_key: str | None = None,
    audio: bytes | None = None,
    sample_rate: int = 8000,
    encoding: str = "pcm_s16le",
    model: str = ASSEMBLYAI_DEFAULT_MODEL,
    language_codes: list[str] | None = None,
    keyterms: list[str] | None = None,
    mode: str | None = "balanced",
    timeout_s: float = 20.0,
    meter: bool = True,
    base_url: str = ASSEMBLYAI_STREAMING_URL,
) -> dict:
    """Run one session. ``audio`` is raw bytes already in ``encoding``."""
    source = "byok" if api_key else "platform"
    api_key = api_key or platform_assemblyai_api_key()
    result: dict = {
        "ok": False,
        "model_requested": model,
        "endpoint": base_url,
        "encoding": encoding,
        "sample_rate": sample_rate,
        "credential_source": source,
    }
    if not api_key:
        result["error"] = "ASSEMBLYAI_API_KEY is not provisioned on this server"
        return result

    member = f"smoke:{int(time.time() * 1000)}"
    usage = None
    if meter:
        if source == "platform":
            admission = await admit_platform_session(member, voice_focus=False)
            if not admission.admitted:
                result["error"] = (
                    f"refused by application budget/stream cap: {admission.reason}"
                )
                return result
        usage = AssemblyAIUsageSession(
            member=member,
            organization_id=None,
            workflow_id=None,
            product="smoke_test",
            credential_source=source,
            voice_focus=False,
            model=model,
        )

    bytes_per_sample = 1 if encoding == "pcm_mulaw" else 2
    chunk_bytes = int(sample_rate * bytes_per_sample * CHUNK_MS / 1000)
    if audio is None:
        silence = b"\xff" if encoding == "pcm_mulaw" else b"\x00\x00"
        audio = silence * sample_rate  # one second
    url = build_url(
        model=model,
        sample_rate=sample_rate,
        encoding=encoding,
        language_codes=language_codes,
        keyterms=keyterms,
        mode=mode,
        base_url=base_url,
    )

    finals: list[dict] = []
    partial_count = 0
    first_partial_at = None
    audio_done_at = None
    final_after_audio_ms = None
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            t0 = time.monotonic()
            async with websocket_connect(
                url, additional_headers={"Authorization": api_key}
            ) as ws:
                result["connect_ms"] = round((time.monotonic() - t0) * 1000, 1)
                if usage:
                    usage.connection_opened()
                begin = json.loads(await ws.recv())
                if begin.get("type") == "Error":
                    raise RuntimeError(
                        f"{begin.get('error_code')}: {begin.get('error')}"
                    )
                result["session_id"] = begin.get("id")
                served = (begin.get("configuration") or {}).get("model")
                result["model_served"] = served
                result["model_matches"] = served in (None, model)

                async def sender():
                    nonlocal audio_done_at
                    for i in range(0, len(audio), chunk_bytes):
                        chunk = audio[i : i + chunk_bytes]
                        if len(chunk) < chunk_bytes // 2.5:  # under 20 ms: skip
                            break
                        await ws.send(chunk)
                        await asyncio.sleep(CHUNK_MS / 1000)
                    audio_done_at = time.monotonic()
                    await ws.send(json.dumps({"type": "ForceEndpoint"}))
                    await asyncio.sleep(0.3)
                    await ws.send(json.dumps({"type": "Terminate"}))

                send_task = asyncio.create_task(sender())
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        kind = msg.get("type")
                        if kind == "Turn":
                            if msg.get("end_of_turn") and msg.get("turn_is_formatted"):
                                if msg.get("transcript"):
                                    finals.append(
                                        {
                                            "turn_order": msg.get("turn_order"),
                                            "transcript": msg.get("transcript"),
                                            "language_code": msg.get("language_code"),
                                        }
                                    )
                                    if audio_done_at and final_after_audio_ms is None:
                                        final_after_audio_ms = round(
                                            (time.monotonic() - audio_done_at) * 1000, 1
                                        )
                            elif msg.get("transcript"):
                                partial_count += 1
                                if first_partial_at is None:
                                    first_partial_at = time.monotonic()
                        elif kind == "Error":
                            result["provider_error"] = {
                                "error_code": msg.get("error_code"),
                                "error": msg.get("error"),
                            }
                            if usage:
                                usage.record_error(msg.get("error_code") or "unknown")
                        elif kind == "Termination":
                            result["audio_duration_seconds"] = msg.get(
                                "audio_duration_seconds"
                            )
                            result["session_duration_seconds"] = msg.get(
                                "session_duration_seconds"
                            )
                            if usage and msg.get("session_duration_seconds"):
                                usage.add_billed_seconds(
                                    float(msg["session_duration_seconds"])
                                )
                            break
                finally:
                    send_task.cancel()
        result["ok"] = "provider_error" not in result and result.get(
            "model_matches", True
        )
    except InvalidStatus as exc:
        result["error"] = f"handshake rejected: HTTP {exc.response.status_code}"
    except ConnectionClosed as exc:
        code = getattr(exc.rcvd, "code", None)
        reason = getattr(exc.rcvd, "reason", "")
        result["error"] = f"connection closed by AssemblyAI: {code} {reason}".strip()
    except TimeoutError:
        result["error"] = f"timed out after {timeout_s:g}s"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if usage:
            if "session_duration_seconds" not in result:
                usage.add_billed_seconds(time.monotonic() - started)
            await usage.finish()

    result["finals"] = finals
    result["partials"] = partial_count
    if first_partial_at is not None:
        result["time_to_first_partial_ms"] = round(
            (first_partial_at - started) * 1000, 1
        )
    result["final_after_audio_end_ms"] = final_after_audio_ms
    return result
