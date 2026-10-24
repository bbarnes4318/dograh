from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np
from scipy.signal import firwin, resample_poly


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


class StreamingResampler:
    """Stateful polyphase resampler for incrementally generated audio.

    Resampling each streamed chunk independently (``resample`` above) zero-pads
    both edges of every chunk, which puts a filter transient — an audible tick —
    at every chunk boundary. On the 8 kHz telephone path that happens every
    emitted codec chunk. This class keeps the filter history across chunks so
    the concatenated output equals ``resample_poly`` over the whole utterance
    (same Kaiser FIR, same zero-phase alignment), while still emitting each
    output sample as soon as its input support has arrived.
    """

    def __init__(self, source_rate: int, target_rate: int):
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        divisor = math.gcd(source_rate, target_rate)
        self.up = target_rate // divisor
        self.down = source_rate // divisor
        self.passthrough = self.up == self.down
        if self.passthrough:
            return
        # Mirrors scipy.signal.resample_poly's default filter design.
        max_rate = max(self.up, self.down)
        half_len = 10 * max_rate
        taps = firwin(2 * half_len + 1, 1.0 / max_rate, window=("kaiser", 5.0))
        self._taps = (taps * self.up).astype(np.float64)
        self._delay = half_len
        self._history = np.zeros(0, dtype=np.float64)
        self._history_start = 0  # absolute input index of _history[0]
        self._inputs = 0
        self._outputs = 0

    def _emit(self, last_output: int) -> np.ndarray:
        if last_output <= self._outputs:
            return np.zeros(0, dtype=np.float32)
        m = np.arange(self._outputs, last_output, dtype=np.int64)
        k = np.arange(len(self._taps), dtype=np.int64)
        upsampled = m[:, None] * self.down + self._delay - k[None, :]
        valid = (upsampled % self.up == 0) & (upsampled >= 0)
        source = upsampled // self.up
        valid &= source < self._inputs
        local = np.clip(source - self._history_start, 0, max(len(self._history) - 1, 0))
        values = np.where(
            valid, self._history[local] if len(self._history) else 0.0, 0.0
        )
        self._outputs = last_output
        return (values @ self._taps).astype(np.float32)

    def _trim_history(self) -> None:
        # Oldest input any future output can still reference.
        oldest_upsampled = (
            self._outputs * self.down + self._delay - (len(self._taps) - 1)
        )
        keep_from = max(0, oldest_upsampled // self.up)
        drop = keep_from - self._history_start
        if drop > 0:
            self._history = self._history[drop:]
            self._history_start = keep_from

    def process(self, audio: np.ndarray) -> np.ndarray:
        data = ensure_mono_float32(audio)
        if self.passthrough:
            return data
        self._history = np.concatenate([self._history, data.astype(np.float64)])
        self._inputs += len(data)
        # Output m is final once its newest upsampled input index has arrived:
        # m * down + delay <= (inputs - 1) * up.
        ready = (self._inputs - 1) * self.up - self._delay
        last_output = ready // self.down + 1 if ready >= 0 else 0
        out = self._emit(last_output)
        self._trim_history()
        return out

    def flush(self) -> np.ndarray:
        """Emit the tail held back for filter lookahead (zero-padded end)."""
        if self.passthrough:
            return np.zeros(0, dtype=np.float32)
        total = -(-self._inputs * self.up // self.down)  # ceil, as resample_poly
        out = self._emit(total)
        self._trim_history()
        return out


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
