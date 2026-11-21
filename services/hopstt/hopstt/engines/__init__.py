"""Engine registry. Heavy imports (torch, nemo, faster_whisper, qwen_asr,
websockets) happen inside the factories, so the stub engines run with only
the base dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..models import ModelSpec
from .base import (
    BatchEngine,
    ChannelTranscript,
    EngineError,
    Hypothesis,
    JobCancelled,
    Segment,
    StreamingEngine,
    Word,
)


def _stub(spec, settings):
    from .stub import StubStreamingEngine

    return StubStreamingEngine(spec, settings)


def _stub_batch(spec, settings):
    from .stub import StubBatchEngine

    return StubBatchEngine(spec, settings)


def _nemo_cache_aware(spec, settings):
    from .nemo_cache_aware import NemoCacheAwareEngine

    return NemoCacheAwareEngine(spec, settings)


def _vllm_realtime(spec, settings):
    from .vllm_realtime import VllmRealtimeEngine

    return VllmRealtimeEngine(spec, settings)


def _nemo_speech_cpp(spec, settings):
    from .nemo_speech_cpp import NemoSpeechCppEngine

    return NemoSpeechCppEngine(spec, settings)


def _nemo_offline(spec, settings):
    from .nemo_offline import NemoOfflineEngine

    return NemoOfflineEngine(spec, settings)


def _faster_whisper(spec, settings):
    from .faster_whisper import FasterWhisperEngine

    return FasterWhisperEngine(spec, settings)


def _qwen3_asr(spec, settings):
    from .qwen3_asr import Qwen3AsrEngine

    return Qwen3AsrEngine(spec, settings)


ENGINE_FACTORIES: dict[str, Callable[[ModelSpec, Any], Any]] = {
    "stub": _stub,
    "stub_batch": _stub_batch,
    "nemo_cache_aware": _nemo_cache_aware,
    "vllm_realtime": _vllm_realtime,
    "nemo_speech_cpp": _nemo_speech_cpp,
    "nemo_offline": _nemo_offline,
    "faster_whisper": _faster_whisper,
    "qwen3_asr": _qwen3_asr,
}


def build_engine(spec: ModelSpec, settings: Any):
    try:
        factory = ENGINE_FACTORIES[spec.engine]
    except KeyError as exc:
        raise ValueError(f"no engine adapter {spec.engine!r}") from exc
    return factory(spec, settings)


__all__ = [
    "BatchEngine",
    "ChannelTranscript",
    "ENGINE_FACTORIES",
    "EngineError",
    "Hypothesis",
    "JobCancelled",
    "Segment",
    "StreamingEngine",
    "Word",
    "build_engine",
]
