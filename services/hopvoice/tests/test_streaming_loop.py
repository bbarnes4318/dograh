"""No-GPU tests for the frame-level Qwen streaming loop and stateful resampling.

A fake Qwen runtime stands in for the model: each talker step yields the next
codec frame, and the fake decoder is causal (like the real 12 Hz codec), so a
correctly stitched stream must reproduce a single full decode exactly.
"""

from __future__ import annotations

import sys
import threading
import types

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from hopvoice.audio import StreamingResampler, resample  # noqa: E402

SAMPLES_PER_FRAME = 200
EOS = 999


def _install_fake_qwen(monkeypatch):
    def sample(logits, temperature, top_k, top_p, suppress):
        return torch.argmax(logits, dim=-1)

    def add_ref(window_codes, ref_code, ref_frames, window_frames):
        return window_codes, 0

    module = types.ModuleType("qwen_tts.core.models.modeling_qwen3_tts")
    module._sample_next_token = sample
    module._add_ref_code_context = add_ref
    for name in ("qwen_tts", "qwen_tts.core", "qwen_tts.core.models"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "qwen_tts.core.models.modeling_qwen3_tts", module)


def frame_wave(index: int) -> np.ndarray:
    t = np.arange(SAMPLES_PER_FRAME, dtype=np.float32) + index * SAMPLES_PER_FRAME
    return (0.3 * np.sin(2 * np.pi * t / 37.0) + 0.05 * (index % 5)).astype(np.float32)


class FakeTokenizer:
    def __init__(self, wave=frame_wave):
        self.wave = wave

    def get_decode_upsample_rate(self):
        return SAMPLES_PER_FRAME

    def decode(self, items):
        codes = items[0]["audio_codes"]
        wav = np.concatenate([self.wave(int(row[0])) for row in codes])
        return [wav], 24000


class FakeTalker:
    device = "cpu"

    def __init__(self, total_frames: int, on_step=None):
        self.total = total_frames
        self.steps = 0
        self.on_step = on_step

    def forward(self, **kwargs):
        logits = torch.zeros(1, 1, 1100)
        if kwargs.get("inputs_embeds") is not None:  # prefill
            return types.SimpleNamespace(
                past_key_values=None, past_hidden=None, generation_step=0, logits=logits
            )
        index = self.steps
        self.steps += 1
        if self.on_step:
            self.on_step(self.steps)
        code = EOS if index >= self.total else index
        return types.SimpleNamespace(
            past_key_values=None,
            past_hidden=None,
            generation_step=index + 1,
            logits=logits,
            hidden_states=(None, torch.tensor([[code, 0]])),
        )


def make_wrapper(total_frames: int, on_step=None, wave=frame_wave):
    config = types.SimpleNamespace(
        talker_config=types.SimpleNamespace(codec_eos_token_id=EOS, vocab_size=1100),
        tts_eos_token_id=EOS,
        im_end_token_id=EOS,
    )
    model = types.SimpleNamespace(
        config=config,
        talker=FakeTalker(total_frames, on_step),
        speech_tokenizer=FakeTokenizer(wave),
        _build_talker_inputs=lambda **kw: (torch.zeros(1, 1, 1), None, None, None),
    )
    return types.SimpleNamespace(
        model=model,
        _prompt_items_to_voice_clone_prompt=lambda items: {
            "ref_code": [None],
            "icl_mode": [False],
        },
        _tokenize_texts=lambda texts: [torch.zeros(1)],
        _build_assistant_text=lambda text: text,
        _build_ref_text=lambda text: text,
        _merge_generate_kwargs=lambda **kw: {"do_sample": False},
    )


def run(wrapper, params, cancel_event=None, timings=None):
    from hopvoice import qwen_stream

    return [
        chunk
        for chunk, _ in qwen_stream.stream_voice_clone(
            wrapper,
            text="hello",
            language="English",
            voice_clone_prompt=types.SimpleNamespace(ref_text=None),
            params=params,
            generation={},
            cancel_event=cancel_event,
            timings=timings,
        )
    ]


def test_stream_reproduces_full_decode_without_stitching_error(monkeypatch):
    _install_fake_qwen(monkeypatch)
    from hopvoice.qwen_stream import StreamParams

    frames = 41
    params = StreamParams(
        emit_every_frames=6,
        decode_window_frames=20,
        overlap_samples=64,
        first_chunk_emit_every=2,
        first_chunk_decode_window=10,
        first_chunk_frames=8,
        use_optimized_decode=False,
        fade_in_samples=16,
    )
    timings: dict = {}
    chunks = run(make_wrapper(frames), params, timings=timings)
    out = np.concatenate(chunks)
    ideal = np.concatenate([frame_wave(i) for i in range(frames)])

    assert len(out) == len(ideal)
    # Only the planned fade-in/fade-out may differ from a single full decode.
    np.testing.assert_allclose(out[16:-64], ideal[16:-64], atol=1e-6)
    assert len(chunks[0]) == 2 * SAMPLES_PER_FRAME - 64
    assert timings["prefill_done"] <= timings["first_frame"] <= timings["first_pcm"]
    assert timings["frames"] == frames


def test_cancel_stops_generation_within_one_frame(monkeypatch):
    _install_fake_qwen(monkeypatch)
    from hopvoice.qwen_stream import StreamParams

    cancel = threading.Event()

    def on_step(step):
        if step == 7:  # caller barges in mid-chunk (emit every 12 frames)
            cancel.set()

    wrapper = make_wrapper(500, on_step)
    params = StreamParams(
        emit_every_frames=12,
        first_chunk_emit_every=0,
        overlap_samples=0,
        use_optimized_decode=False,
    )
    chunks = run(wrapper, params, cancel_event=cancel)
    assert chunks == []
    assert wrapper.model.talker.steps == 7


def test_streaming_resampler_matches_whole_utterance():
    rng = np.random.default_rng(1)
    audio = (0.2 * rng.standard_normal(24000)).astype(np.float32)
    for target in (8000, 16000, 48000):
        resampler = StreamingResampler(24000, target)
        pieces = []
        offset = 0
        for size in rng.integers(1, 3000, 40):
            pieces.append(resampler.process(audio[offset : offset + size]))
            offset += size
        pieces.append(resampler.process(audio[offset:]))
        pieces.append(resampler.flush())
        streamed = np.concatenate(pieces)
        whole = resample(audio, 24000, target)
        assert len(streamed) == len(whole)
        np.testing.assert_allclose(streamed, whole, atol=1e-5)


def silent_until(first_speech_frame: int):
    def wave(index: int) -> np.ndarray:
        if index < first_speech_frame:
            return np.zeros(SAMPLES_PER_FRAME, dtype=np.float32)
        return frame_wave(index)

    return wave


def test_leading_silence_is_not_streamed_but_onset_preroll_is_kept(monkeypatch):
    _install_fake_qwen(monkeypatch)
    from hopvoice.qwen_stream import StreamParams

    params = StreamParams(
        emit_every_frames=4,
        first_chunk_emit_every=2,
        first_chunk_frames=6,
        overlap_samples=0,
        use_optimized_decode=False,
        preroll_ms=5.0,  # 120 samples at 24 kHz
        fade_in_samples=0,
    )
    timings: dict = {}
    out = np.concatenate(
        run(make_wrapper(20, wave=silent_until(7)), params, timings=timings)
    )
    ideal = np.concatenate([frame_wave(i) for i in range(7, 20)])
    # 7 silent frames dropped except the 120-sample pre-roll before speech.
    assert len(out) == len(ideal) + 120
    np.testing.assert_allclose(out[120:], ideal, atol=1e-6)
    assert timings["leading_silence_ms"] == pytest.approx(
        (7 * SAMPLES_PER_FRAME - 120) / 24
    )
    assert "no_speech" not in timings


def test_no_speech_stops_generation_instead_of_streaming_silence(monkeypatch):
    _install_fake_qwen(monkeypatch)
    from hopvoice.qwen_stream import StreamParams

    wrapper = make_wrapper(500, wave=silent_until(10_000))
    params = StreamParams(
        emit_every_frames=4,
        first_chunk_emit_every=0,
        overlap_samples=0,
        use_optimized_decode=False,
        max_leading_silence_frames=12,
    )
    timings: dict = {}
    assert run(wrapper, params, timings=timings) == []
    assert timings["no_speech"] is True
    assert wrapper.model.talker.steps == 12
