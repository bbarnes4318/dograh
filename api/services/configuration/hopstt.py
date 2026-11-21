"""Runtime switches and defaults for the self-hosted HopSTT provider.

Environment:
  HOPSTT_ENABLED            "true" to allow the hopstt STT provider (default off).
                            Mirrors HOPVOICE_ENABLED: a saved config that selects
                            hopstt fails validation and call start until the
                            operator turns this on.
  HOPSTT_DEFAULT_ENDPOINT   Endpoint(s) used when a config leaves ``endpoint`` at
                            the built-in default, so admins configure the
                            infrastructure once and agencies just pick "HopSTT".
  HOPSTT_API_KEY            Bearer token used when a config has no api_key.
"""

from __future__ import annotations

import os

from api.services.configuration.registry import HOPSTT_DEFAULT_ENDPOINT

TRUE_VALUES = {"1", "true", "yes", "on"}

HOPSTT_DISABLED_MESSAGE = (
    "HopSTT is disabled on this runtime. Set HOPSTT_ENABLED=true after "
    "deploying the HopSTT GPU service."
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def hopstt_enabled() -> bool:
    return env_flag("HOPSTT_ENABLED", False)


def split_endpoints(value: str | None) -> list[str]:
    out: list[str] = []
    for part in (value or "").split(","):
        part = part.strip().rstrip("/")
        if part and part not in out:
            out.append(part)
    return out


def resolve_hopstt_endpoints(stt_config) -> list[str]:
    """Endpoints for a hopstt STT config, applying HOPSTT_DEFAULT_ENDPOINT."""
    configured = (getattr(stt_config, "endpoint", None) or "").strip()
    env_default = os.getenv("HOPSTT_DEFAULT_ENDPOINT", "").strip()
    if env_default and configured in ("", HOPSTT_DEFAULT_ENDPOINT):
        return split_endpoints(env_default)
    return split_endpoints(configured or HOPSTT_DEFAULT_ENDPOINT)


def resolve_hopstt_api_key(stt_config) -> str | None:
    keys = []
    if hasattr(stt_config, "get_all_api_keys"):
        keys = [k for k in stt_config.get_all_api_keys() if k]
    if keys:
        return keys[0]
    return os.getenv("HOPSTT_API_KEY", "").strip() or None
