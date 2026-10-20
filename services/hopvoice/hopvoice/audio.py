from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np
from scipy.signal import resample_poly


def ensure_mono_float32(audio: np.ndarray) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float32)
    if data.ndim == 2:
        data = data.mean(axis=-1 if data.shape[-1] <= 8 else 0)
    if data.ndim != 1:
        raise ValueError(f"expected mono audio, got shape={data.shape}")
    return np.nan_to_num(data, nan=0.0, posinf=1.0, neginf=-1.0)


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    data = ensure_mono_float32(audio)
    if source_rate == target_rate:
        return data
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    divisor = math.gcd(source_rate, target_rate)
    return np.asarray(
        resample_poly(data, target_rate // divisor, source_rate // divisor),
        dtype=np.float32,
    )


def float_to_pcm_s16le(audio: np.ndarray) -> bytes:
    data = np.clip(ensure_mono_float32(audio), -1.0, 1.0)
    return (data * 32767.0).round().astype("<i2", copy=False).tobytes()


def pcm_chunks(pcm: bytes, sample_rate: int, chunk_ms: int) -> Iterator[bytes]:
    if chunk_ms <= 0:
        raise ValueError("chunk_ms must be positive")
    samples = max(1, round(sample_rate * chunk_ms / 1000.0))
    chunk_bytes = samples * 2
    for offset in range(0, len(pcm), chunk_bytes):
        yield pcm[offset : offset + chunk_bytes]
