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

    # True streaming is opt-in at the service level. The Qwen backend still
    # falls back safely if the installed qwen_tts package lacks the streaming
    # methods used by the pinned research extra.
    streaming_enabled: bool = True
    stream_emit_every_frames: int = 12
    stream_decode_window_frames: int = 80
    stream_overlap_samples: int = 512
    stream_max_frames: int = 10000
    stream_first_chunk_emit_every: int = 5
    stream_first_chunk_decode_window: int = 48
    stream_first_chunk_frames: int = 48
    stream_repetition_penalty: float = 1.05
    stream_repetition_penalty_window: int = 100
    stream_use_optimized_decode: bool = True
    stream_compile: bool = True
    stream_cuda_graphs: bool = True
    stream_compile_mode: str = "reduce-overhead"
    # "hopvoice" = frame-level cancellable loop in qwen_stream.py;
    # "library" = the pinned runtime's own stream_generate_voice_clone.
    stream_loop: str = "hopvoice"
    stream_fade_in_samples: int = 96
    delivery_planner: bool = True
    speech_gate_dbfs: float = -36.0

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
            voices_file=os.getenv(
                "HOPVOICE_VOICES_FILE", "/voices/voices.json"
            ).strip(),
            output_sample_rate=int(os.getenv("HOPVOICE_OUTPUT_SAMPLE_RATE", "24000")),
            audio_chunk_ms=int(os.getenv("HOPVOICE_AUDIO_CHUNK_MS", "20")),
            max_concurrency=max(1, int(os.getenv("HOPVOICE_MAX_CONCURRENCY", "1"))),
            generation_temperature=float(
                os.getenv("HOPVOICE_GENERATION_TEMPERATURE", "0.8")
            ),
            generation_top_p=float(os.getenv("HOPVOICE_GENERATION_TOP_P", "0.95")),
            streaming_enabled=_env_bool("HOPVOICE_STREAMING_ENABLED", True),
            stream_emit_every_frames=max(
                1, int(os.getenv("HOPVOICE_STREAM_EMIT_EVERY_FRAMES", "12"))
            ),
            stream_decode_window_frames=max(
                1, int(os.getenv("HOPVOICE_STREAM_DECODE_WINDOW_FRAMES", "80"))
            ),
            stream_overlap_samples=max(
                0, int(os.getenv("HOPVOICE_STREAM_OVERLAP_SAMPLES", "512"))
            ),
            stream_max_frames=max(
                1, int(os.getenv("HOPVOICE_STREAM_MAX_FRAMES", "10000"))
            ),
            stream_first_chunk_emit_every=max(
                0, int(os.getenv("HOPVOICE_STREAM_FIRST_CHUNK_EMIT_EVERY", "5"))
            ),
            stream_first_chunk_decode_window=max(
                1, int(os.getenv("HOPVOICE_STREAM_FIRST_CHUNK_DECODE_WINDOW", "48"))
            ),
            stream_first_chunk_frames=max(
                0, int(os.getenv("HOPVOICE_STREAM_FIRST_CHUNK_FRAMES", "48"))
            ),
            stream_repetition_penalty=float(
                os.getenv("HOPVOICE_STREAM_REPETITION_PENALTY", "1.05")
            ),
            stream_repetition_penalty_window=max(
                0, int(os.getenv("HOPVOICE_STREAM_REPETITION_PENALTY_WINDOW", "100"))
            ),
            stream_use_optimized_decode=_env_bool(
                "HOPVOICE_STREAM_OPTIMIZED_DECODE", True
            ),
            stream_compile=_env_bool("HOPVOICE_STREAM_COMPILE", True),
            stream_cuda_graphs=_env_bool("HOPVOICE_STREAM_CUDA_GRAPHS", True),
            stream_compile_mode=os.getenv(
                "HOPVOICE_STREAM_COMPILE_MODE", "reduce-overhead"
            ).strip(),
            stream_loop=os.getenv("HOPVOICE_STREAM_LOOP", "hopvoice").strip().lower(),
            delivery_planner=_env_bool("HOPVOICE_DELIVERY_PLANNER", True),
            speech_gate_dbfs=float(os.getenv("HOPVOICE_SPEECH_GATE_DBFS", "-36")),
            stream_fade_in_samples=max(
                0, int(os.getenv("HOPVOICE_STREAM_FADE_IN_SAMPLES", "96"))
            ),
        )
