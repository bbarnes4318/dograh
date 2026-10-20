from __future__ import annotations

import logging
import threading
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


class VoiceBackend(Protocol):
    @property
    def loaded(self) -> bool: ...

    def load(self) -> None: ...

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
    ) -> SynthesisResult: ...


class StubBackend:
    """Deterministic no-GPU backend for protocol tests only."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def loaded(self) -> bool:
        return True

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


class QwenBackend:
    """Qwen3-TTS baseline backend.

    The current public qwen-tts wrapper returns a complete waveform. This class
    deliberately isolates that limitation behind the Hopvoice backend contract.
    The service protocol is already streaming/cancellable so we can replace this
    backend with token-level generation without changing the voice-agent runtime.
    """

    def __init__(self, settings: Settings, voices: VoiceRegistry):
        self.settings = settings
        self.voices = voices
        self._model = None
        self._model_type: str | None = None
        self._prompt_cache: dict[str, object] = {}
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

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
                    "Qwen backend dependencies are missing; install hopwhistle-hopvoice[qwen]"
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

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        instructions: str | None = None,
    ) -> SynthesisResult:
        self.load()
        spec = self._voice(voice_id)
        generation = {
            "temperature": self.settings.generation_temperature,
            "top_p": self.settings.generation_top_p,
        }

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


def build_backend(settings: Settings, voices: VoiceRegistry) -> VoiceBackend:
    if settings.backend == "stub":
        return StubBackend(settings)
    if settings.backend == "qwen":
        return QwenBackend(settings, voices)
    raise ValueError(f"unsupported HOPVOICE_BACKEND '{settings.backend}'")
