"""faster-whisper batch adapter (large-v3-turbo; ``whisper-base`` for CPU dev).

``vad_filter=True`` (Silero, bundled with faster-whisper) drops silence that
Whisper otherwise fills with hallucinated text, and
``condition_on_previous_text=False`` stops one hallucination from seeding the
next window. Segments are produced lazily, so the priority/cancel gate runs
between segments.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import BatchEngine, ChannelTranscript, EngineError, Gate, Segment, Word, _noop_gate

logger = logging.getLogger("hopstt.whisper")


class FasterWhisperEngine(BatchEngine):
    name = "faster-whisper"

    def __init__(self, spec, settings):
        super().__init__(spec, settings)
        self.model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        from ..weights import snapshot_dir

        device = self.settings.device
        kind, _, index = device.partition(":")
        compute_type = self.settings.whisper_compute_type
        if compute_type == "auto":
            compute_type = "float16" if kind == "cuda" else "int8"
        self.model = WhisperModel(
            str(snapshot_dir(self.spec, self.settings)),
            device=kind,
            device_index=int(index or 0),
            compute_type=compute_type,
        )
        self.loaded = True

    def warmup(self) -> None:
        rng = np.random.default_rng(0)
        self.transcribe([(rng.standard_normal(16000) * 0.01).astype(np.float32)], 16000, "en", False)
        self.warm = True

    def close(self) -> None:
        self.model = None

    def transcribe(self, channels, sample_rate, language, word_timestamps, gate: Gate = _noop_gate):
        if sample_rate != self.sample_rate:
            raise EngineError(f"expected {self.sample_rate} Hz audio", transient=False)
        results = []
        for audio in channels:
            gate()
            try:
                segments_iter, info = self.model.transcribe(
                    audio,
                    language=None if language == "auto" else language,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    word_timestamps=word_timestamps,
                    beam_size=self.settings.whisper_beam_size,
                )
                segments = []
                for seg in segments_iter:
                    gate()
                    words = None
                    if word_timestamps and seg.words:
                        words = [Word(w.word.strip(), w.start, w.end, w.probability) for w in seg.words]
                    segments.append(Segment(seg.start, seg.end, seg.text.strip(), words))
            except Exception as exc:
                if exc.__class__.__name__ == "JobCancelled":
                    raise
                logger.exception("faster-whisper failed")
                raise EngineError(f"transcribe failed: {type(exc).__name__}") from exc
            detected = info.language if language == "auto" else language
            results.append(
                ChannelTranscript(
                    text=" ".join(s.text for s in segments).strip(),
                    language=detected,
                    segments=segments,
                )
            )
        return results
