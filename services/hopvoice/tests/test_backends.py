import threading

import numpy as np

from hopvoice.backends import QwenBackend
from hopvoice.config import Settings
from hopvoice.voices import VoiceRegistry, VoiceSpec


class FakeVoiceRegistry(VoiceRegistry):
    def __init__(self):
        self._voices = {
            "v": VoiceSpec(
                voice_id="v",
                language="English",
                ref_audio="ref.wav",
                ref_text="Reference.",
            )
        }

    def get(self, voice_id):
        return self._voices.get(voice_id)


class FakeModel:
    def __init__(self):
        self.model = type("Inner", (), {"tts_model_type": "base"})()
        self.optimized = None
        self.prompt = object()

    def create_voice_clone_prompt(self, **kwargs):
        return self.prompt

    def enable_streaming_optimizations(self, **kwargs):
        self.optimized = kwargs

    def stream_generate_voice_clone(self, **kwargs):
        yield np.ones(240, dtype=np.float32) * 0.01, 24000
        yield np.ones(240, dtype=np.float32) * 0.02, 24000


def test_qwen_stream_contract_passes_two_phase_controls(monkeypatch):
    settings = Settings(
        preload=False,
        use_flash_attention=False,
        stream_emit_every_frames=12,
        stream_decode_window_frames=80,
        stream_first_chunk_emit_every=5,
        stream_first_chunk_decode_window=48,
        stream_first_chunk_frames=48,
    )
    backend = QwenBackend(settings, FakeVoiceRegistry())
    fake = FakeModel()
    backend._model = fake
    backend._model_type = "base"

    captured = {}

    def stream(**kwargs):
        captured.update(kwargs)
        yield np.ones(240, dtype=np.float32), 24000

    fake.stream_generate_voice_clone = stream

    chunks = list(
        backend.stream_synthesize(
            text="hello",
            voice_id="v",
            cancel_event=threading.Event(),
        )
    )
    assert len(chunks) == 1
    assert chunks[0].streaming is True
    assert captured["emit_every_frames"] == 12
    assert captured["decode_window_frames"] == 80
    assert captured["first_chunk_emit_every"] == 5
    assert captured["first_chunk_decode_window"] == 48
    assert captured["first_chunk_frames"] == 48
    assert captured["overlap_samples"] == 512


def test_qwen_stream_contract_honors_cancel_before_next_chunk():
    settings = Settings(preload=False, use_flash_attention=False)
    backend = QwenBackend(settings, FakeVoiceRegistry())
    fake = FakeModel()
    backend._model = fake
    backend._model_type = "base"
    cancel = threading.Event()

    iterator = backend.stream_synthesize(text="hello", voice_id="v", cancel_event=cancel)
    first = next(iterator)
    assert first.streaming is True
    cancel.set()
    assert list(iterator) == []
