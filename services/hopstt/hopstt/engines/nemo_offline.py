"""NeMo offline batch adapter (nvidia/parakeet-tdt-0.6b-v3).

Word and segment timestamps come from ``transcribe(..., timestamps=True)``.
For recordings longer than ``LOCAL_ATTENTION_AFTER_S`` the model card's
long-audio recipe is applied: local attention ``rel_pos_local_attn`` with a
[256, 256] window and subsampling conv chunking factor 1. Parakeet v3
detects the language itself (25 European languages); the requested language
is only reported back.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import BatchEngine, ChannelTranscript, EngineError, Gate, Segment, Word, _noop_gate

logger = logging.getLogger("hopstt.nemo_offline")
LOCAL_ATTENTION_AFTER_S = 20 * 60


class NemoOfflineEngine(BatchEngine):
    name = "nemo-offline"

    def __init__(self, spec, settings):
        super().__init__(spec, settings)
        self.model = None
        self._local_attention = False

    def load(self) -> None:
        import torch
        import nemo.collections.asr as nemo_asr

        from ..weights import weight_file

        path = weight_file(self.spec, self.settings)
        model = nemo_asr.models.ASRModel.restore_from(str(path), map_location=torch.device(self.settings.device))
        model.eval()
        self.model = model
        self.loaded = True

    def warmup(self) -> None:
        rng = np.random.default_rng(0)
        self.transcribe([(rng.standard_normal(16000 * 2) * 0.01).astype(np.float32)], 16000, "en", True)
        self.warm = True

    def close(self) -> None:
        self.model = None

    def _set_long_audio(self, enabled: bool) -> None:
        if enabled == self._local_attention:
            return
        if enabled:
            self.model.change_attention_model("rel_pos_local_attn", [256, 256])
            self.model.change_subsampling_conv_chunking_factor(1)
        else:
            self.model.change_attention_model("rel_pos")
            self.model.change_subsampling_conv_chunking_factor(-1)
        self._local_attention = enabled

    def transcribe(self, channels, sample_rate, language, word_timestamps, gate: Gate = _noop_gate):
        if sample_rate != self.sample_rate:
            raise EngineError(f"expected {self.sample_rate} Hz audio", transient=False)
        results = []
        for audio in channels:
            gate()
            self._set_long_audio(len(audio) / sample_rate > LOCAL_ATTENTION_AFTER_S)
            if len(audio) < sample_rate // 10:
                results.append(ChannelTranscript(text="", language=language, segments=[]))
                continue
            try:
                out = self.model.transcribe([audio], batch_size=1, timestamps=True, verbose=False)
            except Exception as exc:  # pragma: no cover - real model only
                logger.exception("parakeet transcribe failed")
                raise EngineError(f"transcribe failed: {type(exc).__name__}") from exc
            hyp = out[0] if isinstance(out, list) else out[0][0]
            results.append(_to_transcript(hyp, language, word_timestamps))
        return results


def _to_transcript(hyp, language: str, word_timestamps: bool) -> ChannelTranscript:
    text = getattr(hyp, "text", "") or ""
    stamps = getattr(hyp, "timestamp", None) or {}
    words = [
        Word(str(w.get("word", "")), float(w.get("start", 0.0)), float(w.get("end", 0.0)))
        for w in stamps.get("word", []) or []
    ]
    segments: list[Segment] = []
    for seg in stamps.get("segment", []) or []:
        start, end = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        seg_words = [w for w in words if w.start >= start - 1e-3 and w.end <= end + 1e-3]
        segments.append(Segment(start, end, str(seg.get("segment", "")).strip(), seg_words if word_timestamps else None))
    if not segments and text:
        end = words[-1].end if words else 0.0
        segments = [Segment(words[0].start if words else 0.0, end, text, words if word_timestamps else None)]
    return ChannelTranscript(text=text.strip(), language=language, segments=segments)
