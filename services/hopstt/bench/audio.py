"""Audio loading/encoding helpers and synthetic test signals.

Loading prefers the stdlib ``wave`` module (PCM16 WAV, any channel count) and
falls back to ffmpeg for other formats or sample-rate conversion. Encoders for
G.711 mu-law / A-law are implemented in numpy (``audioop`` is gone in 3.13).
"""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


def _read_wav(path: Path) -> tuple[np.ndarray, int] | None:
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                return None
            rate, ch = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    except (wave.Error, EOFError):
        return None
    return data.reshape(-1, ch), rate


def _ffmpeg_decode(path: Path, sample_rate: int, channel: int | None,
                   start: float | None, end: float | None) -> np.ndarray:
    if not shutil.which("ffmpeg"):
        raise RuntimeError(f"ffmpeg is required to decode {path.suffix} or resample")
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(path)]
    if channel is not None:
        cmd += ["-af", f"pan=mono|c0=c{int(channel)}"]
    cmd += ["-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    res = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(res.stdout, dtype="<i2").copy()


def load_audio(path: str | Path, *, sample_rate: int = 8000, channel: int | None = None,
               start: float | None = None, end: float | None = None) -> np.ndarray:
    """Return mono int16 samples at ``sample_rate`` for ``path`` (optionally one channel / a segment).

    For a multi-channel file with ``channel=None`` the channels are averaged.
    """
    path = Path(path)
    parsed = _read_wav(path) if path.suffix.lower() == ".wav" else None
    if parsed is None or parsed[1] != sample_rate:
        return _ffmpeg_decode(path, sample_rate, channel, start, end)
    frames, rate = parsed
    if channel is not None:
        if channel >= frames.shape[1]:
            raise ValueError(f"{path.name}: channel {channel} not present ({frames.shape[1]} channels)")
        mono = frames[:, channel]
    elif frames.shape[1] == 1:
        mono = frames[:, 0]
    else:
        mono = frames.astype(np.int32).mean(axis=1).astype(np.int16)
    a = int(round((start or 0.0) * rate))
    b = int(round(end * rate)) if end is not None else len(mono)
    return np.ascontiguousarray(mono[a:b], dtype=np.int16)


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int, channels: int = 1) -> None:
    data = np.asarray(samples, dtype="<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(data.tobytes())


def wav_duration(path: str | Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, OSError):
        return None


# --- G.711 ------------------------------------------------------------------


def lin2ulaw(samples: np.ndarray) -> bytes:
    """PCM16 -> G.711 mu-law (ITU-T G.711, bias 0x84, clip 32635)."""
    x = np.asarray(samples, dtype=np.int32)
    sign = np.where(x < 0, 0x80, 0x00)
    mag = np.minimum(np.abs(x), 32635) + 0x84
    exponent = np.floor(np.log2(mag)).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def ulaw2lin(data: bytes) -> np.ndarray:
    u = ~np.frombuffer(data, dtype=np.uint8).astype(np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(sign != 0, -mag, mag).astype(np.int16)


def lin2alaw(samples: np.ndarray) -> bytes:
    """PCM16 -> G.711 A-law."""
    x = np.asarray(samples, dtype=np.int32) >> 3  # 13-bit
    sign = np.where(x >= 0, 0x80, 0x00)
    mag = np.minimum(np.where(x >= 0, x, -x - 1), 0xFFF)
    exponent = np.where(mag < 32, 0, np.floor(np.log2(np.maximum(mag, 1))).astype(np.int32) - 4)
    exponent = np.clip(exponent, 0, 7)
    mantissa = np.where(exponent == 0, mag >> 1, mag >> exponent) & 0x0F
    return ((sign | (exponent << 4) | mantissa) ^ 0x55).astype(np.uint8).tobytes()


def encode(samples: np.ndarray, encoding: str) -> bytes:
    if encoding == "pcm_s16le":
        return np.asarray(samples, dtype="<i2").tobytes()
    if encoding == "mulaw":
        return lin2ulaw(samples)
    if encoding == "alaw":
        return lin2alaw(samples)
    raise ValueError(f"unsupported encoding {encoding}")


def bytes_per_sample(encoding: str) -> int:
    return 2 if encoding == "pcm_s16le" else 1


# --- Synthetic signals (tests / smoke runs only; never real speech) -----------


def tone(seconds: float, sample_rate: int = 8000, freq: float = 440.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16)


def silence(seconds: float, sample_rate: int = 8000) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.int16)


def noise(seconds: float, sample_rate: int = 8000, amplitude: float = 0.02, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * sample_rate)) * amplitude * 32767).clip(-32768, 32767).astype(np.int16)


def rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))
