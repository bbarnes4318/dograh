"""Qwen3-ASR-1.7B batch adapter via the ``qwen-asr`` package (transformers
backend).

No word timestamps: they need the separate Qwen3-ForcedAligner-0.6B, which
is not wired or verified here, so ``word_timestamps`` is ignored and each
channel is one segment spanning the audio. ``qwen-asr`` pins its own
``transformers`` version, so it must be built into its own image
(``HOPSTT_EXTRAS=qwen``), not alongside NeMo.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import BatchEngine, ChannelTranscript, EngineError, Gate, Segment, _noop_gate

logger = logging.getLogger("hopstt.qwen3")

LANG_NAMES = {"en": "English", "es": "Spanish"}
NAME_TO_CODE = {"english": "en", "spanish": "es"}


class Qwen3AsrEngine(BatchEngine):
    name = "qwen3-asr"

    def __init__(self, spec, settings):
        super().__init__(spec, settings)
        self.model = None

    def load(self) -> None:
        import torch
        from qwen_asr import Qwen3ASRModel

        from ..weights import snapshot_dir

        device = self.settings.device
        self.model = Qwen3ASRModel.from_pretrained(
            str(snapshot_dir(self.spec, self.settings)),
            dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
            device_map=device,
            max_inference_batch_size=8,
            max_new_tokens=4096,
        )
        self.loaded = True

    def warmup(self) -> None:
        rng = np.random.default_rng(0)
        self.transcribe([(rng.standard_normal(16000) * 0.01).astype(np.float32)], 16000, "en", False)
        self.warm = True

    def close(self) -> None:
        self.model = None

    def transcribe(self, channels, sample_rate, language, word_timestamps, gate: Gate = _noop_gate):
        results = []
        for audio in channels:
            gate()
            try:
                out = self.model.transcribe(
                    audio=[(audio, sample_rate)],
                    language=None if language == "auto" else LANG_NAMES.get(language),
                )
            except Exception as exc:  # pragma: no cover - real model only
                logger.exception("qwen3-asr failed")
                raise EngineError(f"transcribe failed: {type(exc).__name__}") from exc
            r = out[0]
            text = (getattr(r, "text", "") or "").strip()
            detected = NAME_TO_CODE.get(str(getattr(r, "language", "")).lower(), language)
            duration = len(audio) / float(sample_rate)
            segments = [Segment(0.0, duration, text, None)] if text else []
            results.append(ChannelTranscript(text=text, language=detected, segments=segments))
        return results
