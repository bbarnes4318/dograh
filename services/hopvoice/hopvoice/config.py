from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    backend: str = "qwen"
    profile: str = "native-fast"
    model_id: str = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    use_flash_attention: bool = True
    preload: bool = True
    api_key: str | None = None
    voices_file: str = "/voices/voices.json"
    output_sample_rate: int = 24000
    audio_chunk_ms: int = 20
    max_concurrency: int = 1
    generation_temperature: float = 0.8
    generation_top_p: float = 0.95

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            backend=os.getenv("HOPVOICE_BACKEND", "qwen").strip().lower(),
            profile=os.getenv("HOPVOICE_PROFILE", "native-fast").strip(),
            model_id=os.getenv(
                "HOPVOICE_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
            ).strip(),
            device=os.getenv("HOPVOICE_DEVICE", "cuda:0").strip(),
            dtype=os.getenv("HOPVOICE_DTYPE", "bfloat16").strip(),
            use_flash_attention=_env_bool("HOPVOICE_FLASH_ATTENTION", True),
            preload=_env_bool("HOPVOICE_PRELOAD", True),
            api_key=os.getenv("HOPVOICE_API_KEY") or None,
            voices_file=os.getenv("HOPVOICE_VOICES_FILE", "/voices/voices.json").strip(),
            output_sample_rate=int(os.getenv("HOPVOICE_OUTPUT_SAMPLE_RATE", "24000")),
            audio_chunk_ms=int(os.getenv("HOPVOICE_AUDIO_CHUNK_MS", "20")),
            max_concurrency=max(1, int(os.getenv("HOPVOICE_MAX_CONCURRENCY", "1"))),
            generation_temperature=float(
                os.getenv("HOPVOICE_GENERATION_TEMPERATURE", "0.8")
            ),
            generation_top_p=float(os.getenv("HOPVOICE_GENERATION_TOP_P", "0.95")),
        )
