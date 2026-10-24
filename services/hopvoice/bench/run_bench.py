"""Hopvoice benchmark + listening-pack harness over the production WebSocket.

It exercises exactly the path the Pipecat adapter uses (/v1/tts/ws), so the
numbers include protocol, queueing, resampling and framing, not just the model.

Measures per utterance (client clock unless noted):
  ttfa_ms              text sent -> first PCM byte received
  server breakdown     queue_wait / prefill / first_frame / first_pcm_generated /
                       first_pcm_sent (server clock, from turn_done)
  generation_ms, rtf   server generation time / generated audio duration
  underrun_ms          real-time playout gaps if audio were played as it arrived
Cold start (the first request after the worker starts) is reported separately
from warm runs. Interruption scenarios measure stale audio after cancel_all, the
server's GPU release tail (/metrics), and the next turn's TTFA.

Outputs into --out:
  results.json, utterances.csv
  wav/<id>_24k.wav                   native session audio
  wav/<id>_8k_mulaw.wav              8 kHz session audio after G.711 mu-law
  wav/dialogue_*.wav                 sentence-turn dialogue, concatenated

Usage:
  python -m bench.run_bench --endpoint ws://gpu-host:8787 --voice alex_fec_v1 \
      --profile native-fast --out bench-out --repeats 3 [--analyze]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import statistics
import subprocess
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PACK = Path(__file__).with_name("final_expense_pack.json")


@dataclass
class Turn:
    turn_id: str
    text: str
    sent_at: float
    chunks: list[tuple[float, bytes]] = field(default_factory=list)
    started_at: float | None = None
    done: dict | None = None
    done_at: float | None = None
    error: str | None = None

    @property
    def pcm(self) -> bytes:
        return b"".join(chunk for _, chunk in self.chunks)


class Session:
    def __init__(
        self,
        endpoint: str,
        api_key: str | None,
        voice: str,
        profile: str,
        sample_rate: int,
    ):
        self.url = endpoint.rstrip("/")
        if not self.url.endswith("/v1/tts/ws"):
            self.url += "/v1/tts/ws"
        self.url = self.url.replace("http://", "ws://").replace("https://", "wss://")
        self.api_key = api_key
        self.voice = voice
        self.profile = profile
        self.sample_rate = sample_rate
        self.turns: dict[str, Turn] = {}
        self.order: list[str] = []
        self.current: str | None = None
        self.events: list[tuple[float, dict]] = []
        self._ws = None
        self._reader = None
        self._done_waiters: dict[str, asyncio.Future] = {}

    async def __aenter__(self):
        from websockets.asyncio.client import connect

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        self._ws = await connect(self.url, additional_headers=headers, max_size=None)
        await self._ws.send(
            json.dumps(
                {
                    "type": "start",
                    "protocol_version": 1,
                    "session_id": f"bench-{uuid.uuid4()}",
                    "model": self.profile,
                    "voice_id": self.voice,
                    "output": {
                        "encoding": "pcm_s16le",
                        "sample_rate": self.sample_rate,
                        "channels": 1,
                    },
                }
            )
        )
        ready = json.loads(await self._ws.recv())
        if ready.get("type") != "ready":
            raise RuntimeError(f"session rejected: {ready}")
        self.ready = ready
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        try:
            await self._ws.send(json.dumps({"type": "end"}))
        except Exception:
            pass
        self._reader.cancel()
        await self._ws.close()

    async def _read(self):
        async for raw in self._ws:
            now = time.perf_counter()
            if isinstance(raw, bytes):
                if self.current and self.current in self.turns:
                    self.turns[self.current].chunks.append((now, raw))
                else:
                    self.events.append(
                        (now, {"type": "_stray_audio", "bytes": len(raw)})
                    )
                continue
            msg = json.loads(raw)
            self.events.append((now, msg))
            turn = self.turns.get(msg.get("turn_id") or "")
            kind = msg.get("type")
            if kind in {"turn_started", "audio_meta"} and turn:
                self.current = turn.turn_id
                turn.started_at = turn.started_at or now
            elif kind in {"turn_done", "error"} and turn:
                turn.done = msg
                turn.done_at = now
                if kind == "error":
                    turn.error = msg.get("message")
                if self.current == turn.turn_id:
                    self.current = None
                waiter = self._done_waiters.pop(turn.turn_id, None)
                if waiter and not waiter.done():
                    waiter.set_result(turn)

    async def say(self, text: str) -> Turn:
        turn_id = f"t-{uuid.uuid4().hex[:8]}"
        turn = Turn(turn_id=turn_id, text=text, sent_at=time.perf_counter())
        self.turns[turn_id] = turn
        self.order.append(turn_id)
        self._done_waiters[turn_id] = asyncio.get_running_loop().create_future()
        await self._ws.send(
            json.dumps({"type": "text", "turn_id": turn_id, "text": text})
        )
        return turn

    async def wait(self, turn: Turn, timeout: float = 300.0) -> Turn:
        fut = self._done_waiters.get(turn.turn_id)
        if fut is not None:
            await asyncio.wait_for(fut, timeout)
        return turn

    async def cancel_all(self) -> float:
        self.current = None
        at = time.perf_counter()
        await self._ws.send(json.dumps({"type": "cancel_all"}))
        for fut in self._done_waiters.values():
            if not fut.done():
                fut.cancel()
        self._done_waiters.clear()
        return at


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def ttfa_ms(turn: Turn) -> float | None:
    if not turn.chunks:
        return None
    return round((turn.chunks[0][0] - turn.sent_at) * 1000.0, 1)


def underrun_ms(
    chunks: list[tuple[float, bytes]], sample_rate: int, start_at: float | None = None
) -> float:
    """Total playout gap if audio were played in real time from first arrival."""
    if not chunks:
        return 0.0
    clock = start_at if start_at is not None else chunks[0][0]
    gap = 0.0
    for arrived, chunk in chunks:
        if arrived > clock:
            gap += arrived - clock
            clock = arrived
        clock += len(chunk) / 2 / sample_rate
    return round(gap * 1000.0, 1)


def pct(values: list[float], q: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def summarize(values: list[float]) -> dict:
    clean = [v for v in values if v is not None]
    return {
        "n": len(clean),
        "p50": pct(clean, 0.5),
        "p95": pct(clean, 0.95) if len(clean) >= 5 else None,
        "mean": round(statistics.fmean(clean), 1) if clean else None,
        "min": min(clean) if clean else None,
        "max": max(clean) if clean else None,
    }


def scrape_metrics(endpoint: str) -> dict[str, float]:
    url = endpoint.rstrip("/").replace("ws://", "http://").replace("wss://", "https://")
    url = url.removesuffix("/v1/tts/ws") + "/metrics"
    try:
        body = urllib.request.urlopen(url, timeout=5).read().decode()
    except Exception:
        return {}
    out = {}
    for line in body.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition(" ")
            try:
                out[name] = float(value)
            except ValueError:
                pass
    return out


def gpu_snapshot() -> dict:
    if not shutil.which("nvidia-smi"):
        return {"available": False}
    query = (
        "name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu"
    )
    try:
        line = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            text=True,
            timeout=10,
        ).splitlines()[0]
        name, driver, total, used, util, temp = [
            part.strip() for part in line.split(",")
        ]
        cuda = subprocess.check_output(["nvidia-smi"], text=True, timeout=10)
        cuda_version = (
            cuda.split("CUDA Version:")[1].split()[0]
            if "CUDA Version:" in cuda
            else None
        )
        return {
            "available": True,
            "name": name,
            "driver": driver,
            "cuda": cuda_version,
            "memory_total_mib": float(total),
            "memory_used_mib": float(used),
            "utilization_pct": float(util),
            "temperature_c": float(temp),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio, sample_rate, subtype="PCM_16")


def turn_row(turn: Turn, phrase: dict, sample_rate: int, run: int, cold: bool) -> dict:
    done = turn.done or {}
    audio_s = len(turn.pcm) / 2 / sample_rate
    return {
        "id": phrase["id"],
        "category": phrase["category"],
        "sample_rate": sample_rate,
        "run": run,
        "cold": cold,
        "chars": len(turn.text),
        "ttfa_ms": ttfa_ms(turn),
        "server_first_pcm_sent_ms": done.get("first_pcm_sent_ms"),
        "server_first_pcm_generated_ms": done.get("first_pcm_generated_ms"),
        "server_first_frame_ms": done.get("first_frame_ms"),
        "server_prefill_ms": done.get("prefill_ms"),
        "server_queue_wait_ms": done.get("queue_wait_ms"),
        "generation_ms": done.get("generation_ms"),
        "audio_ms": round(audio_s * 1000.0, 1),
        "rtf": done.get("rtf"),
        "frames": done.get("frames"),
        "underrun_ms": underrun_ms(turn.chunks, sample_rate),
        "streaming": done.get("streaming"),
        "error": turn.error,
    }


async def run_phrases(
    args, phrases, sample_rate, out: Path, rows: list, cold_state: dict
) -> None:
    async with Session(
        args.endpoint, args.api_key, args.voice, args.profile, sample_rate
    ) as session:
        for run in range(args.repeats):
            for phrase in phrases:
                turn = await session.say(phrase["text"])
                await session.wait(turn)
                cold = not cold_state["seen"]
                cold_state["seen"] = True
                rows.append(turn_row(turn, phrase, sample_rate, run, cold))
                if run == 0:
                    audio = pcm_to_float(turn.pcm)
                    if sample_rate == 8000:
                        from tools.voice_metrics import mulaw_roundtrip

                        write_wav(
                            out / "wav" / f"{phrase['id']}_8k_mulaw.wav",
                            mulaw_roundtrip(audio),
                            8000,
                        )
                    else:
                        write_wav(
                            out / "wav" / f"{phrase['id']}_{sample_rate // 1000}k.wav",
                            audio,
                            sample_rate,
                        )
                print(json.dumps(rows[-1]), flush=True)


async def run_dialogue(args, pack, sample_rate, out: Path) -> dict:
    """Sentence-turns submitted back-to-back, as Pipecat does within a reply."""
    async with Session(
        args.endpoint, args.api_key, args.voice, args.profile, sample_rate
    ) as session:
        turns = [await session.say(text) for text in pack["dialogue"]]
        for turn in turns:
            await session.wait(turn)
        chunks = [c for t in turns for c in t.chunks]
        audio = pcm_to_float(b"".join(t.pcm for t in turns))
        suffix = "8k_mulaw" if sample_rate == 8000 else f"{sample_rate // 1000}k"
        if sample_rate == 8000:
            from tools.voice_metrics import mulaw_roundtrip

            audio = mulaw_roundtrip(audio)
        write_wav(out / "wav" / f"dialogue_{suffix}.wav", audio, sample_rate)
        return {
            "sample_rate": sample_rate,
            "turns": len(turns),
            "ttfa_ms": ttfa_ms(turns[0]),
            "audio_ms": round(len(audio) / sample_rate * 1000.0, 1),
            "underrun_ms": underrun_ms(chunks, sample_rate),
            "per_turn_ttfa_ms": [ttfa_ms(t) for t in turns],
        }


async def run_interruptions(args, pack, sample_rate: int) -> list[dict]:
    text = pack["interruption_text"]
    results = []
    scenarios = [
        ("mid_speech_after_600ms", 0.6, 1),
        ("immediate_after_first_audio", 0.0, 1),
        ("cancel_before_audio", None, 1),
        ("queued_turns_dropped", 0.3, 3),
    ]
    async with Session(
        args.endpoint, args.api_key, args.voice, args.profile, sample_rate
    ) as session:
        for name, delay, queued in scenarios:
            before = scrape_metrics(args.endpoint)
            turns = [await session.say(text) for _ in range(queued)]
            first = turns[0]
            if delay is None:
                await asyncio.sleep(0.01)
            else:
                while not first.chunks:
                    await asyncio.sleep(0.002)
                await asyncio.sleep(delay)
            heard_before = sum(len(c) for t in turns for c in [t.pcm])
            cancel_at = await session.cancel_all()
            # Everything arriving for these turns after cancel is stale audio
            # the client must drop; the adapter does, but it measures leakage.
            await asyncio.sleep(1.0)
            stale = [
                (arrived, chunk)
                for t in turns
                for arrived, chunk in t.chunks
                if arrived > cancel_at
            ]
            stray = [
                e
                for at, e in session.events
                if at > cancel_at and e.get("type") == "_stray_audio"
            ]
            stale_ms = sum(len(c) for _, c in stale) / 2 / sample_rate * 1000.0
            stale_ms += sum(e["bytes"] for e in stray) / 2 / sample_rate * 1000.0
            last_stale = max((a for a, _ in stale), default=None)
            nxt = await session.say("Oh, sorry, go ahead.")
            await session.wait(nxt)
            after = scrape_metrics(args.endpoint)
            tail_obs = after.get(
                "hopvoice_cancel_tail_observations_total", 0
            ) - before.get("hopvoice_cancel_tail_observations_total", 0)
            tail_sum = after.get("hopvoice_cancel_tail_seconds_total", 0) - before.get(
                "hopvoice_cancel_tail_seconds_total", 0
            )
            later_turns = turns[1:]
            results.append(
                {
                    "scenario": name,
                    "sample_rate": sample_rate,
                    "audio_before_cancel_ms": round(
                        heard_before / 2 / sample_rate * 1000.0, 1
                    ),
                    "stale_audio_after_cancel_ms": round(stale_ms, 1),
                    "stale_window_ms": round((last_stale - cancel_at) * 1000.0, 1)
                    if last_stale
                    else 0.0,
                    "queued_turn_audio_ms": round(
                        sum(len(t.pcm) for t in later_turns) / 2 / sample_rate * 1000.0,
                        1,
                    ),
                    "gpu_cancel_tail_ms": round(tail_sum / tail_obs * 1000.0, 1)
                    if tail_obs
                    else None,
                    "next_turn_ttfa_ms": ttfa_ms(nxt),
                    "next_turn_ok": bool(
                        nxt.done and nxt.done.get("type") == "turn_done" and nxt.pcm
                    ),
                }
            )
            print(json.dumps(results[-1]), flush=True)
    return results


def analyze_outputs(
    out: Path, pack: dict, voice_ref: Path | None, base_model: str | None
) -> dict:
    import soundfile as sf

    from tools.voice_metrics import SpeakerEncoder, Transcriber, analyze

    transcriber = Transcriber()
    encoder = None
    ref_emb = None
    if voice_ref and base_model:
        import torch
        from qwen_tts import Qwen3TTSModel

        wrapper = Qwen3TTSModel.from_pretrained(
            base_model, device_map="cpu", dtype=torch.float32
        )
        encoder = SpeakerEncoder(wrapper)
        ref_audio, ref_sr = sf.read(voice_ref, dtype="float32")
        ref_emb = encoder.embed(ref_audio, ref_sr)
    report = {}
    for phrase in pack["phrases"]:
        for path in sorted((out / "wav").glob(f"{phrase['id']}_*.wav")):
            audio, sr = sf.read(path, dtype="float32")
            row = analyze(audio, sr, phrase["text"], transcriber)
            if encoder is not None and sr >= 16000 and len(audio) / sr >= 1.0:
                row["speaker_similarity"] = encoder.similarity(
                    ref_emb, encoder.embed(audio, sr)
                )
            report[path.stem] = row
            print(path.stem, json.dumps(row), flush=True)
    return report


async def main_async(args) -> None:
    pack = json.loads(Path(args.pack).read_text())
    phrases = pack["phrases"]
    if args.only:
        phrases = [
            p
            for p in phrases
            if p["id"] in set(args.only) or p["category"] in set(args.only)
        ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results: dict = {
        "endpoint": args.endpoint,
        "voice": args.voice,
        "profile": args.profile,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gpu_before": gpu_snapshot(),
        "metrics_before": scrape_metrics(args.endpoint),
    }
    rows: list[dict] = []
    cold_state = {"seen": not args.first_is_cold}
    for sample_rate in args.sample_rates:
        await run_phrases(args, phrases, sample_rate, out, rows, cold_state)
    if not args.skip_dialogue:
        results["dialogue"] = [
            await run_dialogue(args, pack, sr, out) for sr in args.sample_rates
        ]
    if not args.skip_interruptions:
        results["interruptions"] = await run_interruptions(
            args, pack, args.sample_rates[-1]
        )

    warm = [r for r in rows if not r["cold"] and not r["error"]]
    results["cold"] = [r for r in rows if r["cold"]]
    results["summary"] = {}
    for sample_rate in args.sample_rates:
        subset = [r for r in warm if r["sample_rate"] == sample_rate]
        results["summary"][str(sample_rate)] = {
            key: summarize([r[key] for r in subset])
            for key in (
                "ttfa_ms",
                "server_first_pcm_sent_ms",
                "server_first_pcm_generated_ms",
                "server_prefill_ms",
                "generation_ms",
                "rtf",
                "underrun_ms",
            )
        }
        results["summary"][str(sample_rate)]["short_ttfa_ms"] = summarize(
            [r["ttfa_ms"] for r in subset if r["category"] in {"short", "very_short"}]
        )
    results["errors"] = [r for r in rows if r["error"]]
    results["gpu_after"] = gpu_snapshot()
    results["metrics_after"] = scrape_metrics(args.endpoint)
    if args.analyze:
        results["voice_analysis"] = analyze_outputs(
            out, pack, Path(args.voice_ref) if args.voice_ref else None, args.base_model
        )

    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    with (out / "utterances.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(results["summary"], indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hopvoice WebSocket benchmark")
    parser.add_argument("--endpoint", default="ws://127.0.0.1:8787")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--voice", default="alex_fec_v1")
    parser.add_argument("--profile", default="native-fast")
    parser.add_argument("--pack", default=str(PACK))
    parser.add_argument("--out", default="bench-out")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sample-rates", type=int, nargs="+", default=[24000, 8000])
    parser.add_argument("--only", nargs="*", help="phrase ids or categories")
    parser.add_argument(
        "--first-is-cold",
        action="store_true",
        help="first request follows a fresh worker start",
    )
    parser.add_argument("--skip-dialogue", action="store_true")
    parser.add_argument("--skip-interruptions", action="store_true")
    parser.add_argument(
        "--analyze", action="store_true", help="ASR/pitch/pace/identity analysis (CPU)"
    )
    parser.add_argument("--voice-ref", help="reference WAV for speaker similarity")
    parser.add_argument(
        "--base-model", help="Qwen Base checkpoint for the speaker encoder"
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
