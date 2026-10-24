"""Objective voice measurements for Hopvoice listening packs.

These numbers do not replace listening. They catch what a listener would
otherwise have to find by ear across dozens of files: a voice outside the
target register, monotone or over-acted pitch, rushed or dragging pace, dead air
before the first word, clipping, words a telephone listener would mishear, and
identity drift between utterances.

``telephone()`` renders what the caller actually hears on the PSTN leg:
8 kHz G.711 mu-law.
"""

from __future__ import annotations

import re

import numpy as np

from hopvoice.audio import resample


def mulaw_roundtrip(audio: np.ndarray, mu: float = 255.0) -> np.ndarray:
    """Encode/decode 8-bit G.711-style mu-law (quantization included)."""
    x = np.clip(np.asarray(audio, dtype=np.float64), -1.0, 1.0)
    encoded = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
    quantized = np.round((encoded + 1.0) / 2.0 * 255.0) / 255.0 * 2.0 - 1.0
    decoded = np.sign(quantized) * ((1.0 + mu) ** np.abs(quantized) - 1.0) / mu
    return decoded.astype(np.float32)


def telephone(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """What the caller hears: resampled to 8 kHz, then G.711 mu-law."""
    return mulaw_roundtrip(resample(audio, sample_rate, 8000))


def _frame_db(
    audio: np.ndarray, sample_rate: int, frame_ms: float = 10.0
) -> np.ndarray:
    hop = max(1, int(sample_rate * frame_ms / 1000.0))
    n = len(audio) // hop
    if n == 0:
        return np.zeros(0)
    frames = audio[: n * hop].reshape(n, hop).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    return 20.0 * np.log10(rms)


def silence_profile(
    audio: np.ndarray, sample_rate: int, threshold_db: float = -40.0
) -> dict[str, float]:
    """Leading/trailing silence and internal pauses (>=150 ms) in ms."""
    db = _frame_db(audio, sample_rate)
    if len(db) == 0:
        return {"leading_ms": 0.0, "trailing_ms": 0.0, "pauses": 0, "pause_ms": 0.0}
    gate = max(threshold_db, db.max() - 45.0)
    active = db > gate
    if not active.any():
        total = len(db) * 10.0
        return {"leading_ms": total, "trailing_ms": total, "pauses": 0, "pause_ms": 0.0}
    first = int(np.argmax(active))
    last = len(active) - int(np.argmax(active[::-1])) - 1
    pauses = 0
    pause_frames = 0
    run = 0
    for value in active[first : last + 1]:
        if not value:
            run += 1
            continue
        if run >= 15:
            pauses += 1
            pause_frames += run
        run = 0
    return {
        "leading_ms": first * 10.0,
        "trailing_ms": (len(active) - 1 - last) * 10.0,
        "pauses": pauses,
        "pause_ms": pause_frames * 10.0,
    }


def pitch_profile(audio: np.ndarray, sample_rate: int) -> dict[str, float | None]:
    """Median F0 and pitch movement in semitones (librosa pYIN)."""
    import librosa

    y = resample(audio, sample_rate, 16000)
    f0, voiced, _ = librosa.pyin(
        y, fmin=60.0, fmax=320.0, sr=16000, frame_length=1024, hop_length=160
    )
    values = f0[voiced & np.isfinite(f0)]
    if len(values) < 10:
        return {
            "f0_median_hz": None,
            "f0_std_st": None,
            "f0_range_st": None,
            "voiced_ratio": 0.0,
        }
    semitones = 12.0 * np.log2(values / np.median(values))
    return {
        "f0_median_hz": round(float(np.median(values)), 1),
        "f0_std_st": round(float(np.std(semitones)), 2),
        "f0_range_st": round(
            float(np.percentile(semitones, 95) - np.percentile(semitones, 5)), 2
        ),
        "voiced_ratio": round(float(np.mean(voiced)), 3),
    }


def level_profile(audio: np.ndarray) -> dict[str, float]:
    x = np.asarray(audio, dtype=np.float64)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    rms = float(np.sqrt(np.mean(x**2))) if len(x) else 0.0
    return {
        "peak_dbfs": round(20.0 * np.log10(peak + 1e-12), 2),
        "rms_dbfs": round(20.0 * np.log10(rms + 1e-12), 2),
        "clipped_ratio": float(np.mean(np.abs(x) >= 0.999)) if len(x) else 0.0,
    }


def boundary_discontinuity(audio: np.ndarray) -> float:
    """Largest sample-to-sample jump relative to the signal's typical jump.

    Splices and resampler edge transients show up as isolated jumps far larger
    than anything natural speech produces; values well above ~8 deserve a listen.
    """
    x = np.asarray(audio, dtype=np.float64)
    if len(x) < 3:
        return 0.0
    diff = np.abs(np.diff(x))
    typical = np.percentile(diff, 99) + 1e-9
    return round(float(diff.max() / typical), 2)


_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def normalize_transcript(text: str) -> str:
    """Lowercase, strip punctuation, unify spelled/digit numbers for WER."""
    text = text.lower().replace("-", " ").replace("—", " ")
    text = re.sub(r"\$\s*([\d,]+)", r"\1 dollars", text)
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in _NUMBER_WORDS:
            value = _NUMBER_WORDS[word]
            j = i + 1
            if (
                value >= 20
                and j < len(words)
                and words[j] in _NUMBER_WORDS
                and _NUMBER_WORDS[words[j]] < 10
            ):
                value += _NUMBER_WORDS[words[j]]
                j += 1
            if j < len(words) and words[j] in {"thousand", "hundred"}:
                value *= 1000 if words[j] == "thousand" else 100
                j += 1
            out.append(str(value))
            i = j
            continue
        out.append(word)
        i += 1
    return " ".join(out)


class Transcriber:
    """faster-whisper wrapper; optional dependency used only by tools/bench."""

    def __init__(self, model: str = "small.en", device: str = "cpu"):
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            model, device=device, compute_type="int8" if device == "cpu" else "float16"
        )

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        y = resample(audio, sample_rate, 16000)
        segments, _ = self._model.transcribe(y, language="en", beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()


def word_error_rate(reference: str, hypothesis: str) -> float:
    import jiwer

    ref = normalize_transcript(reference)
    hyp = normalize_transcript(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return round(float(jiwer.wer(ref, hyp)), 3)


class SpeakerEncoder:
    """Speaker embedding from the Qwen3-TTS Base checkpoint's own encoder.

    Cosine similarity between an utterance and the voice reference tracks
    identity drift with the same features the clone model conditions on.
    """

    def __init__(self, qwen_wrapper):
        self._model = qwen_wrapper.model

    def embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        import torch

        y = resample(audio, sample_rate, 24000)
        with torch.inference_mode():
            emb = self._model.extract_speaker_embedding(y.astype(np.float32), 24000)
        vec = emb.detach().float().cpu().numpy().reshape(-1)
        return vec / (np.linalg.norm(vec) + 1e-9)

    @staticmethod
    def similarity(a: np.ndarray, b: np.ndarray) -> float:
        return round(float(np.dot(a, b)), 4)


def analyze(
    audio: np.ndarray,
    sample_rate: int,
    text: str | None = None,
    transcriber: Transcriber | None = None,
) -> dict:
    duration = len(audio) / sample_rate
    result: dict = {"duration_s": round(duration, 3)}
    result.update(silence_profile(audio, sample_rate))
    result.update(pitch_profile(audio, sample_rate))
    result.update(level_profile(audio))
    result["max_jump_ratio"] = boundary_discontinuity(audio)
    if text:
        words = len(normalize_transcript(text).split())
        spoken = max(
            0.05, duration - (result["leading_ms"] + result["trailing_ms"]) / 1000.0
        )
        result["words_per_s"] = round(words / spoken, 2)
    if text and transcriber is not None:
        hyp = transcriber.transcribe(audio, sample_rate)
        result["asr"] = hyp
        result["wer"] = word_error_rate(text, hyp)
    return result
