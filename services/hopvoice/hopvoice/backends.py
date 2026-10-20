from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .config import Settings
from .voices import VoiceRegistry, VoiceSpec

logger = logging.getLogger("hopvoice")


@dataclass(frozen=True)
class SynthesisResult:
    audio: np.ndarray
    sample_rate: int
    model_id: str
    voice_id: str
    streaming: bool = False


class VoiceBackend(Protocol):
    @property
    def loaded(self) -> bool: ...

    @property
    def streaming_available(self) -> bool: ...

    def load(self) -> None: ...

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
    ) -> SynthesisResult: ...

    def stream_synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[SynthesisResult]: ...


class StubBackend:
    """Deterministic no-GPU backend for protocol tests only."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def loaded(self) -> bool:
        return True

    @property
    def streaming_available(self) -> bool:
        return self.settings.streaming_enabled

    def load(self) -> None:
        return

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
    ) -> SynthesisResult:
        duration = max(0.08, min(0.4, len(text) * 0.004))
        frames = max(1, int(self.settings.output_sample_rate * duration))
        t = np.arange(frames, dtype=np.float32) / self.settings.output_sample_rate
        audio = 0.005 * np.sin(2.0 * np.pi * 220.0 * t)
        return SynthesisResult(
            audio=audio,
            sample_rate=self.settings.output_sample_rate,
            model_id="stub",
            voice_id=voice_id,
        )

    def stream_synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[SynthesisResult]:
        result = self.synthesize(
            text=text,
            voice_id=voice_id,
            instructions=instructions,
        )
        if not self.settings.streaming_enabled:
            yield result
            return

        chunk_samples = max(1, round(result.sample_rate * 0.04))
        for offset in range(0, len(result.audio), chunk_samples):
            if cancel_event and cancel_event.is_set():
                return
            yield SynthesisResult(
                audio=result.audio[offset : offset + chunk_samples],
                sample_rate=result.sample_rate,
                model_id=result.model_id,
                voice_id=result.voice_id,
                streaming=True,
            )


class QwenBackend:
    """Qwen3-TTS backend with optional true codec/audio streaming.

    The official 0.1.x convenience wrapper returns a complete waveform. Hopvoice
    therefore detects the streaming methods exposed by the separately pinned
    research runtime and falls back to full-waveform generation when they are
    absent. The service protocol does not change between the two modes.
    """

    def __init__(self, settings: Settings, voices: VoiceRegistry):
        self.settings = settings
        self.voices = voices
        self._model = None
        self._model_type: str | None = None
        self._prompt_cache: dict[str, object] = {}
        self._load_lock = threading.Lock()
        self._streaming_optimized = False

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def streaming_available(self) -> bool:
        return bool(
            self.settings.streaming_enabled
            and self._model is not None
            and self._model_type == "base"
            and callable(getattr(self._model, "stream_generate_voice_clone", None))
        )

    def load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                import torch
                from qwen_tts import Qwen3TTSModel
            except ImportError as exc:
                raise RuntimeError(
                    "Qwen backend dependencies are missing; install "
                    "hopwhistle-hopvoice[qwen] or hopwhistle-hopvoice[qwen-streaming]"
                ) from exc

            dtype = getattr(torch, self.settings.dtype, None)
            if dtype is None:
                raise ValueError(f"unsupported torch dtype: {self.settings.dtype}")

            kwargs = {
                "device_map": self.settings.device,
                "dtype": dtype,
            }
            if self.settings.use_flash_attention:
                kwargs["attn_implementation"] = "flash_attention_2"

            logger.info("loading Qwen3-TTS model %s", self.settings.model_id)
            try:
                model = Qwen3TTSModel.from_pretrained(self.settings.model_id, **kwargs)
            except (ImportError, ModuleNotFoundError) as exc:
                if not self.settings.use_flash_attention:
                    raise
                logger.warning(
                    "FlashAttention unavailable (%s); retrying with the default attention backend",
                    exc,
                )
                kwargs.pop("attn_implementation", None)
                model = Qwen3TTSModel.from_pretrained(self.settings.model_id, **kwargs)

            self._model = model
            self._model_type = str(model.model.tts_model_type)
            logger.info(
                "loaded Qwen3-TTS model %s (type=%s)",
                self.settings.model_id,
                self._model_type,
            )
            self._maybe_enable_streaming_optimizations()

    def _maybe_enable_streaming_optimizations(self) -> None:
        if not self.settings.streaming_enabled or self._model_type != "base":
            return
        stream_fn = getattr(self._model, "stream_generate_voice_clone", None)
        if not callable(stream_fn):
            logger.warning(
                "Qwen runtime has no stream_generate_voice_clone(); "
                "Hopvoice will use full-waveform fallback"
            )
            return
        if not self.settings.stream_use_optimized_decode:
            return
        optimize = getattr(self._model, "enable_streaming_optimizations", None)
        if not callable(optimize):
            logger.info(
                "Qwen streaming runtime has no enable_streaming_optimizations(); "
                "continuing without compile/CUDA-graph warmup"
            )
            return
        try:
            optimize(
                decode_window_frames=self.settings.stream_decode_window_frames,
                use_compile=self.settings.stream_compile,
                use_cuda_graphs=self.settings.stream_cuda_graphs,
                compile_mode=self.settings.stream_compile_mode,
            )
            self._streaming_optimized = True
            logger.info(
                "enabled Qwen streaming optimizations window=%s compile=%s cuda_graphs=%s",
                self.settings.stream_decode_window_frames,
                self.settings.stream_compile,
                self.settings.stream_cuda_graphs,
            )
        except Exception:
            logger.exception(
                "Qwen streaming optimization warmup failed; continuing unoptimized"
            )

    def _voice(self, voice_id: str) -> VoiceSpec:
        spec = self.voices.get(voice_id)
        if spec:
            return spec
        if self._model_type == "custom_voice":
            return VoiceSpec(voice_id=voice_id, speaker=voice_id)
        raise ValueError(f"unknown Hopvoice voice_id '{voice_id}'")

    def _clone_prompt(self, spec: VoiceSpec):
        cached = self._prompt_cache.get(spec.voice_id)
        if cached is not None:
            return cached
        if not spec.ref_audio:
            raise ValueError(
                f"voice '{spec.voice_id}' has no ref_audio; a Base Qwen model needs a licensed voice reference"
            )
        if not spec.x_vector_only_mode and not spec.ref_text:
            raise ValueError(
                f"voice '{spec.voice_id}' needs ref_text unless x_vector_only_mode=true"
            )
        prompt = self._model.create_voice_clone_prompt(
            ref_audio=spec.ref_audio,
            ref_text=spec.ref_text,
            x_vector_only_mode=spec.x_vector_only_mode,
        )
        self._prompt_cache[spec.voice_id] = prompt
        return prompt

    def _generation(self) -> dict[str, float]:
        return {
            "temperature": self.settings.generation_temperature,
            "top_p": self.settings.generation_top_p,
        }

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
    ) -> SynthesisResult:
        self.load()
        spec = self._voice(voice_id)
        generation = self._generation()

        if self._model_type == "base":
            prompt = self._clone_prompt(spec)
            wavs, sample_rate = self._model.generate_voice_clone(
                text=text,
                language=spec.language,
                voice_clone_prompt=prompt,
                non_streaming_mode=True,
                **generation,
            )
        elif self._model_type == "custom_voice":
            speaker = spec.speaker or spec.voice_id
            instruct = instructions or spec.default_instruct or ""
            wavs, sample_rate = self._model.generate_custom_voice(
                text=text,
                language=spec.language,
                speaker=speaker,
                instruct=instruct,
                non_streaming_mode=True,
                **generation,
            )
        elif self._model_type == "voice_design":
            instruct = instructions or spec.default_instruct
            if not instruct:
                raise ValueError("voice_design requires instructions or default_instruct")
            wavs, sample_rate = self._model.generate_voice_design(
                text=text,
                language=spec.language,
                instruct=instruct,
                non_streaming_mode=True,
                **generation,
            )
        else:
            raise RuntimeError(f"unsupported Qwen TTS model type: {self._model_type}")

        if not wavs:
            raise RuntimeError("Qwen3-TTS returned no audio")
        return SynthesisResult(
            audio=np.asarray(wavs[0], dtype=np.float32),
            sample_rate=int(sample_rate),
            model_id=self.settings.model_id,
            voice_id=voice_id,
        )

    def _stream_kwargs(self, stream_fn) -> dict:
        desired = {
            "emit_every_frames": self.settings.stream_emit_every_frames,
            "decode_window_frames": self.settings.stream_decode_window_frames,
            "overlap_samples": self.settings.stream_overlap_samples,
            "max_frames": self.settings.stream_max_frames,
            "use_optimized_decode": self.settings.stream_use_optimized_decode,
            "first_chunk_emit_every": self.settings.stream_first_chunk_emit_every,
            "first_chunk_decode_window": self.settings.stream_first_chunk_decode_window,
            "first_chunk_frames": self.settings.stream_first_chunk_frames,
            "repetition_penalty": self.settings.stream_repetition_penalty,
            "repetition_penalty_window": self.settings.stream_repetition_penalty_window,
        }
        try:
            signature = inspect.signature(stream_fn)
        except (TypeError, ValueError):
            return desired
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
        if accepts_kwargs:
            return desired
        return {k: v for k, v in desired.items() if k in signature.parameters}

    def stream_synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[SynthesisResult]:
        self.load()

        stream_fn = getattr(self._model, "stream_generate_voice_clone", None)
        if (
            not self.settings.streaming_enabled
            or self._model_type != "base"
            or not callable(stream_fn)
        ):
            if cancel_event and cancel_event.is_set():
                return
            yield self.synthesize(
                text=text,
                voice_id=voice_id,
                instructions=instructions,
            )
            return

        spec = self._voice(voice_id)
        prompt = self._clone_prompt(spec)
        kwargs = {
            "text": text,
            "language": spec.language,
            "voice_clone_prompt": prompt,
            "non_streaming_mode": False,
            **self._stream_kwargs(stream_fn),
            **self._generation(),
        }

        for chunk, sample_rate in stream_fn(**kwargs):
            if cancel_event and cancel_event.is_set():
                return
            data = np.asarray(chunk, dtype=np.float32)
            if data.size == 0:
                continue
            yield SynthesisResult(
                audio=data,
                sample_rate=int(sample_rate),
                model_id=self.settings.model_id,
                voice_id=voice_id,
                streaming=True,
            )


def build_backend(settings: Settings, voices: VoiceRegistry) -> VoiceBackend:
    if settings.backend == "stub":
        return StubBackend(settings)
    if settings.backend == "qwen":
        return QwenBackend(settings, voices)
    raise ValueError(f"unsupported HOPVOICE_BACKEND '{settings.backend}'")
