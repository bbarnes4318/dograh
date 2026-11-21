"""Engine adapter interfaces.

Two kinds of engine sit behind HopSTT:

* ``StreamingEngine`` advances many live sessions at once. The scheduler
  thread hands it one fixed-size chunk (``chunk_ms`` of 16 kHz float32 audio)
  per ready session and calls ``step()`` once for the whole batch. Each
  session's model state (encoder caches, decoder hypothesis) lives in the
  opaque object returned by ``new_state()``. Implementations must be genuinely
  incremental: a step processes only the new chunk, never a growing buffer.
* ``BatchEngine`` transcribes whole recordings, one array per channel.

All engine methods are called from a single inference thread per worker
(the scheduler for streaming, the batch pool for batch), so they need not be
thread-safe with respect to themselves.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..models import ModelSpec

ENGINE_SAMPLE_RATE = 16000


class EngineError(RuntimeError):
    """Inference failed. ``transient`` failures may be retried."""

    def __init__(self, message: str, *, transient: bool = True):
        super().__init__(message)
        self.transient = transient


class JobCancelled(Exception):
    """Raised from a batch gate when the job was cancelled."""


@dataclass
class Hypothesis:
    """Current text of the open utterance (interim) or its final text."""

    text: str = ""
    language: str | None = None
    confidence: float | None = None
    # Engine-detected end of utterance (only engines with an EOU signal).
    eou: bool = False


@dataclass
class Word:
    word: str
    start: float
    end: float
    confidence: float | None = None

    def to_dict(self) -> dict:
        out = {"word": self.word, "start": round(self.start, 3), "end": round(self.end, 3)}
        if self.confidence is not None:
            out["confidence"] = round(float(self.confidence), 3)
        return out


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] | None = None

    def to_dict(self, include_words: bool) -> dict:
        out = {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}
        if include_words and self.words is not None:
            out["words"] = [w.to_dict() for w in self.words]
        return out


@dataclass
class ChannelTranscript:
    text: str
    language: str | None
    segments: list[Segment] = field(default_factory=list)


# Gate passed to batch engines: call between internal chunks of work. It
# blocks while live work is pending (role=both priority gate) and raises
# JobCancelled when the job was cancelled.
Gate = Callable[[], None]


def _noop_gate() -> None:
    return None


class _EngineBase(ABC):
    kind: str = ""
    name: str = ""  # wire-level "engine" label

    def __init__(self, spec: ModelSpec, settings: Any):
        self.spec = spec
        self.settings = settings
        self.loaded = False
        self.warm = False

    @abstractmethod
    def load(self) -> None: ...

    def warmup(self) -> None:
        self.warm = True

    def close(self) -> None:
        """Release process-level resources (models, connections)."""


class StreamingEngine(_EngineBase):
    kind = "streaming"
    sample_rate = ENGINE_SAMPLE_RATE

    @abstractmethod
    def new_state(self, language: str, chunk_ms: int) -> Any: ...

    @abstractmethod
    def step(self, states: list[Any], chunks: list[np.ndarray]) -> list[Hypothesis]:
        """Advance every session by one chunk in a single batched call."""

    @abstractmethod
    def finalize(self, state: Any, tail: np.ndarray) -> Hypothesis:
        """Process ``tail`` (< one chunk), flush the right context with
        silence, return the utterance's final hypothesis and start a new
        utterance in ``state``."""

    def reset_utterance(self, state: Any) -> None:
        """Drop the open utterance without producing a final (cancel)."""

    @abstractmethod
    def free(self, state: Any) -> None: ...

    def batch_key(self, state: Any) -> Any:
        """Sessions with equal keys may share one ``step()`` call."""
        return getattr(state, "chunk_ms", None)


class BatchEngine(_EngineBase):
    kind = "batch"
    sample_rate = ENGINE_SAMPLE_RATE

    @abstractmethod
    def transcribe(
        self,
        channels: list[np.ndarray],
        sample_rate: int,
        language: str,
        word_timestamps: bool,
        gate: Gate = _noop_gate,
    ) -> list[ChannelTranscript]: ...


# --------------------------------------------------------------------------
# helpers shared by adapters
# --------------------------------------------------------------------------
_LANG_TAG = re.compile(r"\s*<([a-z]{2,3})(?:-[A-Za-z]{2,4})?>\s*")


def strip_language_tags(text: str) -> tuple[str, str | None]:
    """Remove ``<xx-XX>`` tags (Nemotron 3.5 auto mode); return the base
    language code of the last tag found."""
    found: str | None = None
    for match in _LANG_TAG.finditer(text):
        found = match.group(1).lower()
    if found is None:
        return text.strip(), None
    cleaned = _LANG_TAG.sub(" ", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(), found


def metric_language(language: str | None) -> str:
    return language if language in ("en", "es", "auto") else "other"
