"""Stream a WAV file to a HopSTT worker like a live call and report latency.

    python -m hopstt.tools.smoke --url ws://127.0.0.1:8790/v1/stt/stream \\
        --key $HOPSTT_API_KEY --wav call.wav --realtime

The client paces 20 ms frames in real time (``--realtime``), runs a simple
energy VAD and sends ``finalize`` after ``--vad-stop-ms`` of silence (what
Dograh does on its VAD stop), and measures finalize -> final latency per
utterance. Transcript text is printed only with ``--show-text``; never use
that on customer audio. Needs the ``websockets`` package.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import wave

import numpy as np


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit("only PCM16 WAV is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    if channels > 1:
        data = data.reshape(-1, channels)[:, 0]
    return data, rate


_MULAW_LUT: np.ndarray | None = None


def mulaw_encode(pcm: np.ndarray) -> bytes:
    """Nearest-code mu-law encoding via a 64K lookup table (test tool only)."""
    global _MULAW_LUT
    if _MULAW_LUT is None:
        from hopstt.audio import ULAW_TO_PCM16

        table = ULAW_TO_PCM16.astype(np.int32)
        order = np.argsort(table)
        values = np.arange(-32768, 32768, dtype=np.int32)
        idx = np.searchsorted(table[order], values).clip(0, 255)
        _MULAW_LUT = order[idx].astype(np.uint8)
    return _MULAW_LUT[pcm.astype(np.int32) + 32768].tobytes()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:8790/v1/stt/stream")
    p.add_argument("--key", required=True)
    p.add_argument("--wav", required=True)
    p.add_argument("--model")
    p.add_argument("--language", default="en")
    p.add_argument("--chunk-ms", type=int)
    p.add_argument("--encoding", default="pcm_s16le", choices=["pcm_s16le", "mulaw"])
    p.add_argument("--seconds", type=float, help="only stream the first N seconds")
    p.add_argument("--realtime", action="store_true", help="pace frames in real time")
    p.add_argument("--vad-stop-ms", type=int, default=600)
    p.add_argument("--vad-dbfs", type=float, default=-40.0)
    p.add_argument("--show-text", action="store_true")
    p.add_argument("--json", action="store_true", help="print a JSON summary only")
    args = p.parse_args(argv)

    from websockets.sync.client import connect

    pcm, rate = read_wav(args.wav)
    if args.seconds:
        pcm = pcm[: int(args.seconds * rate)]
    frame = rate // 50
    cfg = {"type": "config", "language": args.language, "sample_rate": rate, "encoding": args.encoding,
           "interim_results": True, "session_id": "smoke", "generation": 0, "stream_offset_ms": 0}
    if args.model:
        cfg["model"] = args.model
    if args.chunk_ms:
        cfg["chunk_ms"] = args.chunk_ms

    stats = {"interims": 0, "finals": 0, "final_latency_ms": [], "first_interim_ms": None}
    finalize_sent: list[float] = []
    lock = threading.Lock()
    texts: list[str] = []
    t0 = time.perf_counter()

    with connect(args.url, additional_headers={"Authorization": f"Bearer {args.key}"}, max_size=None) as ws:
        ws.send(json.dumps(cfg))
        ready = json.loads(ws.recv(timeout=30))
        if ready.get("type") != "ready":
            print(json.dumps(ready))
            return 1
        done = threading.Event()

        def reader():
            try:
                for raw in ws:
                    m = json.loads(raw)
                    now = time.perf_counter()
                    if m["type"] == "transcript":
                        if m["is_final"]:
                            stats["finals"] += 1
                            with lock:
                                if finalize_sent:
                                    stats["final_latency_ms"].append((now - finalize_sent.pop(0)) * 1000)
                            texts.append(m["text"])
                            if args.show_text and not args.json:
                                print(f"[final {m['start_ms']}-{m['end_ms']} {m['language']}] {m['text']}")
                        else:
                            stats["interims"] += 1
                            if stats["first_interim_ms"] is None:
                                stats["first_interim_ms"] = (now - t0) * 1000
                    elif m["type"] in ("error", "closed"):
                        if m["type"] == "error":
                            stats["error"] = m["code"]
                        break
            except Exception:
                pass
            done.set()

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        in_speech, silent_ms = False, 0
        start = time.perf_counter()
        for i in range(0, len(pcm), frame):
            chunk = pcm[i : i + frame]
            payload = chunk.astype("<i2").tobytes() if args.encoding == "pcm_s16le" else mulaw_encode(chunk)
            ws.send(payload)
            level = 20 * np.log10(max(np.sqrt(np.mean((chunk / 32768.0) ** 2)), 1e-6))
            if level > args.vad_dbfs:
                in_speech, silent_ms = True, 0
            elif in_speech:
                silent_ms += 20
                if silent_ms >= args.vad_stop_ms:
                    in_speech = False
                    with lock:
                        finalize_sent.append(time.perf_counter())
                    ws.send(json.dumps({"type": "finalize"}))
            if args.realtime:
                target = start + (i + frame) / rate
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        with lock:
            finalize_sent.append(time.perf_counter())
        ws.send(json.dumps({"type": "end_of_stream"}))
        done.wait(120)

    lat = stats["final_latency_ms"]
    summary = {
        "audio_s": round(len(pcm) / rate, 2),
        "wall_s": round(time.perf_counter() - t0, 2),
        "interims": stats["interims"],
        "finals": stats["finals"],
        "first_interim_ms": round(stats["first_interim_ms"], 1) if stats["first_interim_ms"] else None,
        "final_latency_ms_p50": round(statistics.median(lat), 1) if lat else None,
        "final_latency_ms_max": round(max(lat), 1) if lat else None,
        "error": stats.get("error"),
        "words": sum(len(t.split()) for t in texts),
    }
    print(json.dumps(summary))
    return 0 if not summary["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
