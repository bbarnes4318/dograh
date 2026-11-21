"""Deterministic test engines (no ML dependencies).

The streaming stub turns audio energy into words: every
``WORD_MS`` of voiced audio (RMS above ``VOICED_DBFS``) adds one word from a
fixed vocabulary. Interims therefore change only when a word is added, and a
finalize yields the capitalised sentence. Silence produces no text.

``HOPSTT_STUB_STEP_LATENCY_MS`` adds a sleep per ``step()`` call (one per
batch, like a GPU forward pass) to exercise backpressure and lag handling.
Tests can set ``fail_next_steps`` to make steps raise ``EngineError``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..audio import dbfs
from .base import (
    BatchEngine,
    ChannelTranscript,
    EngineError,
    Gate,
    Hypothesis,
    Segment,
    StreamingEngine,
    Word,
    _noop_gate,
)

VOCAB = (
    "my", "date", "of", "birth", "is", "march", "third", "nineteen",
    "fifty", "one", "yes", "no", "i", "live", "in", "texas",
)
VOICED_DBFS = -45.0
WORD_MS = 200


def _words(n: int, offset: int = 0) -> list[str]:
    return [VOCAB[(offset + i) % len(VOCAB)] for i in range(n)]


def _language(requested: str) -> str:
    return "en" if requested == "auto" else requested


@dataclass
class StubState:
    language: str
    chunk_ms: int
    voiced_ms: float = 0.0
    utterances: int = 0
    steps: int = 0
    freed: bool = False
    history: list[int] = field(default_factory=list)


class StubStreamingEngine(StreamingEngine):
    name = "stub"

    def __init__(self, spec, settings):
        super().__init__(spec, settings)
        self.fail_next_steps = 0
        self.live_states: set[int] = set()
        self.step_calls = 0
        self.batch_sizes: list[int] = []

    def load(self) -> None:
        self.loaded = True

    def warmup(self) -> None:
        state = self.new_state("en", self.spec.default_chunk_ms or 320)
        try:
            self.step([state], [np.zeros(16 * state.chunk_ms, np.float32)])
            self.finalize(state, np.zeros(0, np.float32))
        finally:
            self.free(state)
        self.warm = True

    def new_state(self, language: str, chunk_ms: int) -> StubState:
        state = StubState(language=language, chunk_ms=chunk_ms)
        self.live_states.add(id(state))
        return state

    def _hyp(self, state: StubState) -> Hypothesis:
        n = int(state.voiced_ms // WORD_MS)
        text = " ".join(_words(n, state.utterances * 3))
        return Hypothesis(text=text, language=_language(state.language), confidence=0.9)

    def _consume(self, state: StubState, audio: np.ndarray) -> None:
        if len(audio) and dbfs(audio) > VOICED_DBFS:
            state.voiced_ms += len(audio) * 1000.0 / self.sample_rate

    def step(self, states, chunks):
        self.step_calls += 1
        self.batch_sizes.append(len(states))
        latency = float(getattr(self.settings, "stub_step_latency_ms", 0.0) or 0.0)
        if latency > 0:
            time.sleep(latency / 1000.0)
        if self.fail_next_steps > 0:
            self.fail_next_steps -= 1
            raise EngineError("stub failure")
        out = []
        for state, chunk in zip(states, chunks):
            if state.freed:
                raise RuntimeError("step on freed state")
            state.steps += 1
            state.history.append(len(chunk))
            self._consume(state, chunk)
            out.append(self._hyp(state))
        return out

    def finalize(self, state: StubState, tail: np.ndarray) -> Hypothesis:
        self._consume(state, tail)
        hyp = self._hyp(state)
        if hyp.text:
            hyp.text = hyp.text[0].upper() + hyp.text[1:] + "."
        state.voiced_ms = 0.0
        state.utterances += 1
        return hyp

    def reset_utterance(self, state: StubState) -> None:
        state.voiced_ms = 0.0

    def free(self, state: StubState) -> None:
        state.freed = True
        self.live_states.discard(id(state))


class StubBatchEngine(BatchEngine):
    """Segments each channel on energy; one word per WORD_MS of voice."""

    name = "stub"

    def __init__(self, spec, settings):
        super().__init__(spec, settings)
        self.fail_next_calls = 0
        self.calls = 0

    def load(self) -> None:
        self.loaded = True

    def warmup(self) -> None:
        self.transcribe([np.zeros(16000, np.float32)], 16000, "en", True)
        self.warm = True

    def transcribe(self, channels, sample_rate, language, word_timestamps, gate: Gate = _noop_gate):
        self.calls += 1
        if self.fail_next_calls > 0:
            self.fail_next_calls -= 1
            raise EngineError("stub batch failure")
        latency = float(getattr(self.settings, "stub_batch_latency_ms", 0.0) or 0.0)
        results = []
        for ch_index, audio in enumerate(channels):
            gate()
            if latency > 0:
                # Simulated inference in slices so cancellation/priority gates
                # are honoured mid-job.
                slices = 5
                for _ in range(slices):
                    time.sleep(latency / 1000.0 / slices / max(1, len(channels)))
                    gate()
            results.append(self._channel(audio, sample_rate, language, ch_index))
        return results

    def _channel(self, audio, sample_rate, language, ch_index) -> ChannelTranscript:
        frame = max(1, sample_rate // 50)  # 20 ms
        n = len(audio) // frame
        voiced = [dbfs(audio[i * frame : (i + 1) * frame]) > VOICED_DBFS for i in range(n)]
        segments: list[Segment] = []
        i = 0
        word_idx = 0
        while i < n:
            if not voiced[i]:
                i += 1
                continue
            j = i
            while j < n and (voiced[j] or (j + 10 < n and any(voiced[j : j + 10]))):
                j += 1
            start, end = i * 0.02, j * 0.02
            count = max(1, int(math.floor((end - start) * 1000 / WORD_MS)))
            words = []
            step = (end - start) / count
            for w_i, w in enumerate(_words(count, word_idx)):
                words.append(Word(w, start + w_i * step, start + (w_i + 1) * step, 0.9))
            word_idx += count
            text = " ".join(w.word for w in words)
            text = text[0].upper() + text[1:] + "."
            segments.append(Segment(start, end, text, words))
            i = j
        return ChannelTranscript(
            text=" ".join(s.text for s in segments),
            language=_language(language),
            segments=segments,
        )
