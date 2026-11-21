"""Runtime settings, all from ``HOPSTT_*`` environment variables."""

from __future__ import annotations

import dataclasses
import os
import socket
from dataclasses import dataclass, field

from .models import CATALOG, ModelSpec

MIN_API_KEY_LENGTH = 16
ROLES = ("live", "batch", "both")


class ConfigError(ValueError):
    """Settings that must stop the process from starting."""


def _env(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _kv_csv(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _csv(raw):
        key, sep, value = item.partition("=")
        if not sep:
            raise ConfigError(f"expected id=value, got {item!r}")
        out[key.strip()] = value.strip()
    return out


@dataclass(frozen=True)
class Settings:
    # --- identity / role -------------------------------------------------
    role: str = "both"
    worker_id: str = field(default_factory=socket.gethostname)
    api_key: str | None = None
    allow_query_token: bool = False
    dev_insecure: bool = False

    # --- models ----------------------------------------------------------
    allowed_models: tuple[str, ...] = ("nemotron-3.5-streaming", "parakeet-tdt-0.6b-v3")
    default_streaming_model: str = "nemotron-3.5-streaming"
    default_batch_model: str = "parakeet-tdt-0.6b-v3"
    device: str = "cuda:0"
    model_cache: str = "/models"
    # model id -> HF commit; overrides the pinned default in models.py.
    model_revisions: dict[str, str] = field(default_factory=dict)
    # model id -> local file/dir; bypasses the HF cache (air-gapped hosts).
    model_paths: dict[str, str] = field(default_factory=dict)
    # Allow downloading a missing pinned revision at startup (dev only).
    allow_download: bool = False
    vllm_url: str = "ws://127.0.0.1:8000/v1/realtime"
    vllm_model: str = "mistralai/Voxtral-Mini-4B-Realtime-2602"
    voxtral_delay_ms: int = 480
    whisper_compute_type: str = "auto"
    whisper_beam_size: int = 5
    stub_step_latency_ms: float = 0.0
    stub_batch_latency_ms: float = 0.0

    # --- live streaming --------------------------------------------------
    max_streams: int = 64
    max_batch_size: int = 64  # sessions per inference step
    config_timeout_s: float = 5.0
    idle_timeout_s: float = 30.0
    max_queue_ms: int = 2000
    max_lag_ms: int = 3000
    max_frame_bytes: int = 64 * 1024
    vad_threshold_dbfs: float = -45.0
    vad_min_speech_ms: int = 60

    # --- batch -----------------------------------------------------------
    max_upload_mb: int = 200
    sync_max_seconds: float = 900.0
    batch_max_concurrency: int = 1
    batch_queue_limit: int = 100
    batch_pause_at_live_util: float = 0.8
    job_ttl_s: float = 3600.0
    job_max_attempts: int = 2
    job_retry_backoff_s: float = 2.0
    ffmpeg_timeout_s: float = 300.0

    # --- lifecycle -------------------------------------------------------
    drain_timeout_s: float = 600.0
    warmup: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            role=_env("HOPSTT_ROLE", defaults.role).lower(),
            worker_id=_env("HOPSTT_WORKER_ID", defaults.worker_id),
            api_key=os.getenv("HOPSTT_API_KEY") or None,
            allow_query_token=_env_bool("HOPSTT_ALLOW_QUERY_TOKEN", False),
            dev_insecure=_env_bool("HOPSTT_DEV_INSECURE", False),
            allowed_models=_csv(_env("HOPSTT_ALLOWED_MODELS", ",".join(defaults.allowed_models))),
            default_streaming_model=_env(
                "HOPSTT_DEFAULT_STREAMING_MODEL", defaults.default_streaming_model
            ),
            default_batch_model=_env("HOPSTT_DEFAULT_BATCH_MODEL", defaults.default_batch_model),
            device=_env("HOPSTT_DEVICE", defaults.device),
            model_cache=_env("HOPSTT_MODEL_CACHE", defaults.model_cache),
            model_revisions=_kv_csv(_env("HOPSTT_MODEL_REVISIONS", "")),
            model_paths=_kv_csv(_env("HOPSTT_MODEL_PATHS", "")),
            allow_download=_env_bool("HOPSTT_ALLOW_DOWNLOAD", False),
            vllm_url=_env("HOPSTT_VLLM_URL", defaults.vllm_url),
            vllm_model=_env("HOPSTT_VLLM_MODEL", defaults.vllm_model),
            voxtral_delay_ms=_env_int("HOPSTT_VOXTRAL_DELAY_MS", defaults.voxtral_delay_ms),
            whisper_compute_type=_env("HOPSTT_WHISPER_COMPUTE_TYPE", defaults.whisper_compute_type),
            whisper_beam_size=_env_int("HOPSTT_WHISPER_BEAM_SIZE", defaults.whisper_beam_size),
            stub_step_latency_ms=_env_float("HOPSTT_STUB_STEP_LATENCY_MS", 0.0),
            stub_batch_latency_ms=_env_float("HOPSTT_STUB_BATCH_LATENCY_MS", 0.0),
            max_streams=_env_int("HOPSTT_MAX_STREAMS", defaults.max_streams),
            max_batch_size=_env_int("HOPSTT_MAX_BATCH_SIZE", defaults.max_batch_size),
            config_timeout_s=_env_float("HOPSTT_CONFIG_TIMEOUT_S", defaults.config_timeout_s),
            idle_timeout_s=_env_float("HOPSTT_IDLE_TIMEOUT_S", defaults.idle_timeout_s),
            max_queue_ms=_env_int("HOPSTT_MAX_QUEUE_MS", defaults.max_queue_ms),
            max_lag_ms=_env_int("HOPSTT_MAX_LAG_MS", defaults.max_lag_ms),
            max_frame_bytes=_env_int("HOPSTT_MAX_FRAME_BYTES", defaults.max_frame_bytes),
            vad_threshold_dbfs=_env_float("HOPSTT_VAD_THRESHOLD_DBFS", defaults.vad_threshold_dbfs),
            vad_min_speech_ms=_env_int("HOPSTT_VAD_MIN_SPEECH_MS", defaults.vad_min_speech_ms),
            max_upload_mb=_env_int("HOPSTT_MAX_UPLOAD_MB", defaults.max_upload_mb),
            sync_max_seconds=_env_float("HOPSTT_SYNC_MAX_SECONDS", defaults.sync_max_seconds),
            batch_max_concurrency=_env_int(
                "HOPSTT_BATCH_MAX_CONCURRENCY", defaults.batch_max_concurrency
            ),
            batch_queue_limit=_env_int("HOPSTT_BATCH_QUEUE_LIMIT", defaults.batch_queue_limit),
            batch_pause_at_live_util=_env_float(
                "HOPSTT_BATCH_PAUSE_AT_LIVE_UTIL", defaults.batch_pause_at_live_util
            ),
            job_ttl_s=_env_float("HOPSTT_JOB_TTL_S", defaults.job_ttl_s),
            job_max_attempts=_env_int("HOPSTT_JOB_MAX_ATTEMPTS", defaults.job_max_attempts),
            job_retry_backoff_s=_env_float("HOPSTT_JOB_RETRY_BACKOFF_S", defaults.job_retry_backoff_s),
            ffmpeg_timeout_s=_env_float("HOPSTT_FFMPEG_TIMEOUT_S", defaults.ffmpeg_timeout_s),
            drain_timeout_s=_env_float("HOPSTT_DRAIN_TIMEOUT_S", defaults.drain_timeout_s),
            warmup=_env_bool("HOPSTT_WARMUP", True),
        )

    def replace(self, **changes) -> "Settings":
        return dataclasses.replace(self, **changes)

    # ------------------------------------------------------------------
    @property
    def serves_live(self) -> bool:
        return self.role in ("live", "both")

    @property
    def serves_batch(self) -> bool:
        return self.role in ("batch", "both")

    def spec(self, model_id: str) -> ModelSpec:
        spec = CATALOG[model_id]
        revision = self.model_revisions.get(model_id)
        if revision and revision != spec.revision:
            spec = dataclasses.replace(spec, revision=revision)
        return spec

    def allowed_specs(self, kind: str | None = None) -> list[ModelSpec]:
        specs = [self.spec(m) for m in self.allowed_models]
        return [s for s in specs if kind is None or s.kind == kind]

    def models_to_load(self) -> list[ModelSpec]:
        """Allowed models that this role actually serves."""
        kinds = []
        if self.serves_live:
            kinds.append("streaming")
        if self.serves_batch:
            kinds.append("batch")
        return [s for s in self.allowed_specs() if s.kind in kinds]

    def validate(self) -> None:
        if self.role not in ROLES:
            raise ConfigError(f"HOPSTT_ROLE must be one of {ROLES}, got {self.role!r}")
        if not self.dev_insecure and (
            not self.api_key or len(self.api_key) < MIN_API_KEY_LENGTH
        ):
            raise ConfigError(
                f"HOPSTT_API_KEY must be set and at least {MIN_API_KEY_LENGTH} characters "
                "(set HOPSTT_DEV_INSECURE=true only for local development)"
            )
        unknown = [m for m in self.allowed_models if m not in CATALOG]
        if unknown:
            raise ConfigError(f"HOPSTT_ALLOWED_MODELS has unknown ids: {unknown}")
        for model_id in self.model_revisions:
            if model_id not in CATALOG:
                raise ConfigError(f"HOPSTT_MODEL_REVISIONS has unknown id {model_id!r}")
        if self.serves_live:
            self._check_default(self.default_streaming_model, "streaming")
        if self.serves_batch:
            self._check_default(self.default_batch_model, "batch")
        if not self.models_to_load():
            raise ConfigError("no allowed model matches this role")
        if self.max_streams < 1 or self.max_batch_size < 1:
            raise ConfigError("HOPSTT_MAX_STREAMS and HOPSTT_MAX_BATCH_SIZE must be >= 1")
        if self.batch_max_concurrency < 1:
            raise ConfigError("HOPSTT_BATCH_MAX_CONCURRENCY must be >= 1")
        if self.voxtral_delay_ms % 80 != 0:
            raise ConfigError("HOPSTT_VOXTRAL_DELAY_MS must be a multiple of 80")

    def _check_default(self, model_id: str, kind: str) -> None:
        if model_id not in self.allowed_models:
            raise ConfigError(f"default {kind} model {model_id!r} is not in HOPSTT_ALLOWED_MODELS")
        if CATALOG[model_id].kind != kind:
            raise ConfigError(f"default {kind} model {model_id!r} is a {CATALOG[model_id].kind} model")
