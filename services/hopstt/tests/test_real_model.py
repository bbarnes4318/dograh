"""Real-model tests. Skipped unless HOPSTT_REAL_MODEL_TESTS=1.

    HOPSTT_REAL_MODEL_TESTS=1 HOPSTT_DEVICE=cpu \
    HOPSTT_MODEL_CACHE=/models \
    HOPSTT_TEST_WAV=/path/to/16k_or_8k_mono_speech.wav \
    python -m pytest tests/test_real_model.py -s

Weights must already be in the cache (python -m hopstt.tools.fetch_models)
or be pointed to with HOPSTT_MODEL_PATHS. Use synthetic or consented audio
only; tests print no transcript text.
"""

from __future__ import annotations

import os
import wave

import numpy as np
import pytest
from scipy.signal import resample_poly

from hopstt.config import Settings
from hopstt.engines import build_engine
from hopstt.models import CATALOG

pytestmark = pytest.mark.real_model


def _settings() -> Settings:
    env = Settings.from_env()
    return env.replace(api_key="x" * 20, device=os.getenv("HOPSTT_DEVICE", "cpu"))


def _audio16k() -> np.ndarray:
    path = os.getenv("HOPSTT_TEST_WAV")
    if not path:
        pytest.skip("HOPSTT_TEST_WAV not set")
    with wave.open(path, "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]
    if rate != 16000:
        data = resample_poly(data, 16000, rate).astype(np.float32)
    return data[: 16000 * 30]


@pytest.mark.parametrize("model_id", ["nemotron-3.5-streaming"])
def test_cache_aware_streaming_matches_offline_shape(model_id):
    audio = _audio16k()
    engine = build_engine(CATALOG[model_id], _settings())
    engine.load()
    # streaming mel features equal the offline spectrogram
    import torch

    state = engine.new_state("en", 320)
    with torch.inference_mode():
        for i in range(0, len(audio), 5120):
            engine._push_audio(state, audio[i : i + 5120])
        full, _ = engine.model.preprocessor(input_signal=torch.from_numpy(audio)[None], length=torch.tensor([len(audio)]))
    n = state.pending_frames
    assert float((full[0, :, :n] - state.mel_pending).abs().max()) < 1e-4
    engine.free(state)
    # two sessions in one batch (different languages) both produce text
    states = [engine.new_state("en", 320), engine.new_state("auto", 320)]
    for i in range(0, len(audio) - 5120 + 1, 5120):
        engine.step(states, [audio[i : i + 5120]] * 2)
    finals = [engine.finalize(s, np.zeros(0, np.float32)) for s in states]
    assert all(len(f.text.split()) > 3 for f in finals)
    assert all("<" not in f.text for f in finals)
    assert finals[1].language in ("en", "es")
    for s in states:
        engine.free(s)


@pytest.mark.parametrize("model_id", ["whisper-base", "parakeet-tdt-0.6b-v3"])
def test_batch_engines(model_id):
    audio = _audio16k()
    engine = build_engine(CATALOG[model_id], _settings())
    engine.load()
    out = engine.transcribe([audio, np.zeros_like(audio)], 16000, "en", True)
    assert len(out[0].text.split()) > 3
    assert out[0].segments and out[0].segments[0].end > out[0].segments[0].start
    # silent channel: no hallucinated words (VAD filter / model)
    assert len(out[1].text.split()) <= 2
