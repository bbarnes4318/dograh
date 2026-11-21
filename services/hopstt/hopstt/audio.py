"""Audio decoding for live frames and recorded files.

Live path: binary WebSocket frames (PCM16 / G.711 mu-law / A-law at 8 or
16 kHz) -> float32 -> stateful 16 kHz resampler -> fixed-size engine chunks.
Batch path: any container ffmpeg can read -> float32 per channel at the
model's rate.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from scipy.signal import firwin

MODEL_SAMPLE_RATE = 16000
ENCODINGS = ("pcm_s16le", "mulaw", "alaw")
SAMPLE_RATES = (8000, 16000)


# --------------------------------------------------------------------------
# ITU-T G.711 decode tables (same values as the reference g711.c by Sun
# Microsystems / the ITU software tool library). Values are 16-bit linear.
# --------------------------------------------------------------------------
def _build_ulaw_table() -> np.ndarray:
    u = (~np.arange(256, dtype=np.int32)) & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t = t << ((u & 0x70) >> 4)
    return np.where(u & 0x80, 0x84 - t, t - 0x84).astype(np.int16)


def _build_alaw_table() -> np.ndarray:
    a = np.arange(256, dtype=np.int32) ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    t = np.where(seg == 0, t + 8, t + 0x108)
    t = np.where(seg > 1, t << np.maximum(seg - 1, 0), t)
    return np.where(a & 0x80, t, -t).astype(np.int16)


ULAW_TO_PCM16 = _build_ulaw_table()
ALAW_TO_PCM16 = _build_alaw_table()
_ULAW_F32 = ULAW_TO_PCM16.astype(np.float32) / 32768.0
_ALAW_F32 = ALAW_TO_PCM16.astype(np.float32) / 32768.0


def decode_mulaw(data: bytes) -> np.ndarray:
    return _ULAW_F32[np.frombuffer(data, dtype=np.uint8)]


def decode_alaw(data: bytes) -> np.ndarray:
    return _ALAW_F32[np.frombuffer(data, dtype=np.uint8)]


def decode_pcm16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def bytes_per_sample(encoding: str) -> int:
    return 2 if encoding == "pcm_s16le" else 1


class FrameDecoder:
    """Decodes binary frames to float32, carrying a trailing odd byte.

    The protocol asks for whole samples per frame, but some transports split
    PCM16 at arbitrary byte boundaries; the leftover byte is prepended to the
    next frame instead of being dropped (which would swap byte order for the
    rest of the call).
    """

    def __init__(self, encoding: str):
        if encoding not in ENCODINGS:
            raise ValueError(f"unsupported encoding {encoding!r}")
        self.encoding = encoding
        self._carry = b""
        self.samples = 0

    def decode(self, frame: bytes) -> np.ndarray:
        if self.encoding == "mulaw":
            out = decode_mulaw(frame)
        elif self.encoding == "alaw":
            out = decode_alaw(frame)
        else:
            data = self._carry + frame if self._carry else frame
            whole = len(data) - (len(data) % 2)
            self._carry = data[whole:]
            out = decode_pcm16(data[:whole])
        self.samples += len(out)
        return out

    @property
    def pending_bytes(self) -> int:
        return len(self._carry)


# --------------------------------------------------------------------------
# Stateful resampling
# --------------------------------------------------------------------------
class StreamingResampler:
    """Polyphase FIR resampler whose output over a whole stream equals
    ``scipy.signal.resample_poly`` over the concatenated input.

    Same filter design as ``resample_poly`` (Kaiser beta 5, half length
    10 * max(up, down)) and the same zero-phase alignment. The filter history
    is carried across chunks, so there is no transient at chunk boundaries;
    output samples are emitted as soon as their input support has arrived
    (lookahead = 10 input samples at 8 kHz = 1.25 ms). Pattern taken from
    services/hopvoice/hopvoice/audio.py.
    """

    def __init__(self, source_rate: int, target_rate: int):
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        divisor = math.gcd(source_rate, target_rate)
        self.up = target_rate // divisor
        self.down = source_rate // divisor
        self.passthrough = self.up == self.down
        self._inputs = 0
        self._outputs = 0
        if self.passthrough:
            return
        max_rate = max(self.up, self.down)
        half_len = 10 * max_rate
        taps = firwin(2 * half_len + 1, 1.0 / max_rate, window=("kaiser", 5.0))
        self._taps = (taps * self.up).astype(np.float64)
        self._delay = half_len
        self._history = np.zeros(0, dtype=np.float64)
        self._history_start = 0

    def _emit(self, last_output: int) -> np.ndarray:
        if last_output <= self._outputs:
            return np.zeros(0, dtype=np.float32)
        m = np.arange(self._outputs, last_output, dtype=np.int64)
        k = np.arange(len(self._taps), dtype=np.int64)
        upsampled = m[:, None] * self.down + self._delay - k[None, :]
        valid = (upsampled % self.up == 0) & (upsampled >= 0)
        source = upsampled // self.up
        valid &= source < self._inputs
        if len(self._history):
            local = np.clip(source - self._history_start, 0, len(self._history) - 1)
            values = np.where(valid, self._history[local], 0.0)
        else:
            values = np.zeros(valid.shape)
        self._outputs = last_output
        return (values @ self._taps).astype(np.float32)

    def _trim_history(self) -> None:
        oldest_upsampled = self._outputs * self.down + self._delay - (len(self._taps) - 1)
        keep_from = max(0, oldest_upsampled // self.up)
        drop = keep_from - self._history_start
        if drop > 0:
            self._history = self._history[drop:]
            self._history_start = keep_from

    def process(self, audio: np.ndarray) -> np.ndarray:
        data = np.asarray(audio, dtype=np.float32)
        if self.passthrough:
            self._inputs += len(data)
            self._outputs += len(data)
            return data
        if not len(data):
            return np.zeros(0, dtype=np.float32)
        self._history = np.concatenate([self._history, data.astype(np.float64)])
        self._inputs += len(data)
        ready = (self._inputs - 1) * self.up - self._delay
        last_output = ready // self.down + 1 if ready >= 0 else 0
        out = self._emit(last_output)
        self._trim_history()
        return out

    def flush(self) -> np.ndarray:
        """Emit the held-back tail (zero-padded end of stream)."""
        if self.passthrough:
            return np.zeros(0, dtype=np.float32)
        total = -(-self._inputs * self.up // self.down)
        out = self._emit(total)
        self._trim_history()
        return out


class Chunker:
    """Cuts a continuous float32 stream into fixed-size engine chunks."""

    def __init__(self, chunk_samples: int):
        self.chunk_samples = chunk_samples
        self._parts: list[np.ndarray] = []
        self._size = 0

    def push(self, audio: np.ndarray) -> list[np.ndarray]:
        if len(audio):
            self._parts.append(audio)
            self._size += len(audio)
        out: list[np.ndarray] = []
        if self._size < self.chunk_samples:
            return out
        data = np.concatenate(self._parts)
        whole = (len(data) // self.chunk_samples) * self.chunk_samples
        for start in range(0, whole, self.chunk_samples):
            out.append(data[start : start + self.chunk_samples])
        rest = data[whole:]
        self._parts = [rest] if len(rest) else []
        self._size = len(rest)
        return out

    def take_rest(self) -> np.ndarray:
        data = np.concatenate(self._parts) if self._parts else np.zeros(0, np.float32)
        self._parts, self._size = [], 0
        return data

    def clear(self) -> None:
        self._parts, self._size = [], 0

    @property
    def pending(self) -> int:
        return self._size


@dataclass
class Timeline:
    """Maps audio received on this connection to the call's timeline (ms)."""

    sample_rate: int
    offset_ms: int = 0
    samples: int = 0

    def advance(self, n: int) -> None:
        self.samples += n

    @property
    def now_ms(self) -> int:
        return self.offset_ms + int(self.samples * 1000 // self.sample_rate)

    def at(self, samples: int) -> int:
        return self.offset_ms + int(samples * 1000 // self.sample_rate)


def dbfs(audio: np.ndarray) -> float:
    if not len(audio):
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    return 20.0 * math.log10(max(rms, 1e-6))


# --------------------------------------------------------------------------
# Batch decode with ffmpeg
# --------------------------------------------------------------------------
class DecodeError(ValueError):
    pass


@dataclass
class DecodedAudio:
    channels: list[np.ndarray]  # float32 per channel at ``sample_rate``
    sample_rate: int  # rate of ``channels`` (the model rate)
    source_sample_rate: int
    source_channels: int
    decode_ms: int

    @property
    def duration_sec(self) -> float:
        if not self.channels:
            return 0.0
        return len(self.channels[0]) / float(self.sample_rate)


def probe_audio(path: str, timeout_s: float) -> dict:
    """Return {"sample_rate", "channels", "duration"} via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,duration:format=duration",
        "-of",
        "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DecodeError("ffprobe timed out") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is not installed") from exc
    if proc.returncode != 0:
        raise DecodeError("unsupported or corrupt audio file")
    info = json.loads(proc.stdout or b"{}")
    streams = info.get("streams") or []
    if not streams:
        raise DecodeError("no audio stream found")
    stream = streams[0]
    duration = stream.get("duration") or (info.get("format") or {}).get("duration")
    return {
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "duration": float(duration) if duration not in (None, "N/A") else None,
    }


def decode_file(
    path: str,
    *,
    target_rate: int = MODEL_SAMPLE_RATE,
    mix: bool = False,
    timeout_s: float = 300.0,
    max_seconds: float | None = None,
) -> DecodedAudio:
    """Decode ``path`` to float32 at ``target_rate``, one array per channel.

    ffmpeg runs without a shell, with an argument list and a timeout. Channel
    separation is preserved unless ``mix`` (down-mix to mono).
    """
    import time

    started = time.perf_counter()
    info = probe_audio(path, timeout_s)
    if info["channels"] <= 0:
        raise DecodeError("audio stream has no channels")
    if max_seconds is not None and info["duration"] is not None and info["duration"] > max_seconds:
        raise DurationExceeded(info["duration"])
    out_channels = 1 if mix else info["channels"]
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        str(out_channels),
        "-ar",
        str(target_rate),
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DecodeError("ffmpeg timed out") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is not installed") from exc
    if proc.returncode != 0:
        raise DecodeError("ffmpeg could not decode the file")
    raw = np.frombuffer(proc.stdout, dtype="<f4")
    usable = len(raw) - (len(raw) % out_channels)
    frames = raw[:usable].reshape(-1, out_channels)
    channels = [np.ascontiguousarray(frames[:, c], dtype=np.float32) for c in range(out_channels)]
    decoded = DecodedAudio(
        channels=channels,
        sample_rate=target_rate,
        source_sample_rate=info["sample_rate"] or target_rate,
        source_channels=info["channels"],
        decode_ms=int((time.perf_counter() - started) * 1000),
    )
    if max_seconds is not None and decoded.duration_sec > max_seconds:
        raise DurationExceeded(decoded.duration_sec)
    return decoded


class DurationExceeded(DecodeError):
    def __init__(self, seconds: float):
        super().__init__(f"audio is {seconds:.1f}s long")
        self.seconds = seconds


def decode_bytes_via_tempfile(
    chunks, *, suffix: str = "", **kwargs
) -> DecodedAudio:
    """Write an iterable of byte chunks to a private temp file, decode, and
    delete the file in ``finally`` (batch audio never stays on disk)."""
    fd, path = tempfile.mkstemp(prefix="hopstt-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
        return decode_file(path, **kwargs)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
