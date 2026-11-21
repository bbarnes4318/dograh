"""AssemblyAI streaming STT: platform credential, pricing, budgets and rollout.

Everything here is server-side only. The platform key is read from the
environment on each call, so rotating it is a deploy-time env change with no
code change, and it is never returned by any API, logged, or put in a URL.

Environment (all optional unless noted):
  ASSEMBLYAI_API_KEY                   Platform AssemblyAI key. Used only when an
                                       AssemblyAI STT config carries no key of
                                       its own (a tenant's BYOK key always wins).
  STT_PLATFORM_DEFAULT_PROVIDER        STT provider preselected for new
                                       configurations (default "assemblyai").
                                       Set to "deepgram" to roll back.
  ASSEMBLYAI_STT_ROLLOUT               "off" (default), "canary" or "global":
                                       whether calls whose STT was inherited
                                       from the platform (not chosen by the
                                       tenant or the workflow) are moved onto
                                       AssemblyAI at call start. See
                                       ``api.services.pipecat.assemblyai_rollout``.
  ASSEMBLYAI_STT_CANARY_ORG_IDS        Comma-separated org ids for "canary".
  ASSEMBLYAI_STT_CANARY_WORKFLOW_IDS   Comma-separated workflow ids for "canary".
  ASSEMBLYAI_STT_PRICE_PER_HOUR        Estimated USD per streaming hour (0.45).
  ASSEMBLYAI_VOICE_FOCUS_PRICE_PER_HOUR  Voice Focus add-on USD/hour (0.10).
  ASSEMBLYAI_DAILY_BUDGET_USD          Optional application-side daily budget
                                       for platform-key sessions. Unset (the
                                       default) = no limit.
  ASSEMBLYAI_TOTAL_BUDGET_USD          Optional lifetime budget. Unset = no limit.
  ASSEMBLYAI_MAX_CONCURRENT_STREAMS    Optional cap on simultaneous platform-key
                                       sessions. Unset = no limit.
  ASSEMBLYAI_RESERVE_MINUTES_PER_CALL  Minutes reserved against the budget at
                                       call start, trued up at call end (5).
  ASSEMBLYAI_FALLBACK_PROVIDER         STT provider to use when AssemblyAI cannot
                                       be used at call start (default empty =
                                       no fallback). Credentials come from the
                                       org's own config; no key, no fallback.

The budgets are *estimates* the application keeps from session durations. They
are not AssemblyAI's account balance and must never be presented as such.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

ASSEMBLYAI_PROVIDER = "assemblyai"
ASSEMBLYAI_STREAMING_URL = "wss://streaming.assemblyai.com/v3/ws"
ASSEMBLYAI_DEFAULT_MODEL = "assemblyai-stt-3-7-realtime-agent"
# Models that run AssemblyAI's voice-agent feature set (semantic end of turn,
# agent_context, mode, voice_focus, UpdateConfiguration). Older saved configs
# keep working: ``u3-rt-pro`` and ``universal-3-6-pro`` are still accepted.
ASSEMBLYAI_STT_MODELS = [
    ASSEMBLYAI_DEFAULT_MODEL,
    "universal-3-6-pro",
    "u3-rt-pro",
]
ASSEMBLYAI_MODES = ["balanced", "min_latency", "max_accuracy"]
ASSEMBLYAI_DEFAULT_MODE = "balanced"
ASSEMBLYAI_VOICE_FOCUS_OPTIONS = ["near-field", "far-field", "off"]
ASSEMBLYAI_TURN_DETECTION_OPTIONS = ["assemblyai", "pipecat"]
# "multi" = English/Spanish with native code-switching.
ASSEMBLYAI_STT_LANGUAGES = ["en", "es", "multi", "de", "fr", "pt", "it"]
ASSEMBLYAI_MULTI_LANGUAGE_CODES = ["en", "es"]
ASSEMBLYAI_ENCODINGS = ["pcm_s16le", "pcm_mulaw"]

# Documented server limits.
ASSEMBLYAI_MAX_KEYTERMS = 100
ASSEMBLYAI_MAX_KEYTERM_CHARS = 50
ASSEMBLYAI_MAX_AGENT_CONTEXT_CHARS = 1500  # server allows 1750; keep headroom

ROLLOUT_MODES = ("off", "canary", "global")
TRUE_VALUES = {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _id_set(name: str) -> frozenset[int]:
    out = set()
    for part in _env(name).split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def platform_assemblyai_api_key() -> str | None:
    """The platform key, or None when it has not been provisioned."""
    return _env("ASSEMBLYAI_API_KEY") or None


def config_api_keys(stt_config) -> list[str]:
    if stt_config is None or not hasattr(stt_config, "get_all_api_keys"):
        return []
    return [k for k in stt_config.get_all_api_keys() if k]


def resolve_assemblyai_api_key(stt_config) -> tuple[str | None, str]:
    """Return ``(key, source)``; source is "byok", "platform" or "missing".

    A key saved on the config (the tenant's own) always wins over the platform
    key, so a BYOK credential is never replaced.
    """
    keys = config_api_keys(stt_config)
    if keys:
        # Honour multi-key configs the same way BaseServiceConfiguration does.
        return getattr(stt_config, "api_key", None) or keys[0], "byok"
    key = platform_assemblyai_api_key()
    if key:
        return key, "platform"
    return None, "missing"


def credential_fingerprint(key: str | None) -> str | None:
    """A non-reversible hint for operators ("…ab12"), never the key itself."""
    if not key:
        return None
    return f"…{key[-4:]}" if len(key) >= 12 else "…"


MISSING_CREDENTIAL_MESSAGE = (
    "AssemblyAI is selected but no credential is available: the STT config has "
    "no api_key and ASSEMBLYAI_API_KEY is not set on the server. An operator "
    "must provision ASSEMBLYAI_API_KEY in the deployment's secret store."
)


# --------------------------------------------------------------------------- #
# Pricing and budgets (application-side estimates)
# --------------------------------------------------------------------------- #


def price_per_hour(*, voice_focus: bool) -> float:
    base = _float_env("ASSEMBLYAI_STT_PRICE_PER_HOUR", 0.45)
    if voice_focus:
        base += _float_env("ASSEMBLYAI_VOICE_FOCUS_PRICE_PER_HOUR", 0.10)
    return base


def estimate_cost_usd(seconds: float, *, voice_focus: bool) -> float:
    return round(
        max(seconds, 0.0) / 3600.0 * price_per_hour(voice_focus=voice_focus), 6
    )


@dataclass(frozen=True)
class BudgetSettings:
    """``None`` means no limit (the default for every limit)."""

    daily_budget_usd: float | None
    total_budget_usd: float | None
    max_concurrent_streams: int | None
    reserve_minutes_per_call: float

    @property
    def unlimited(self) -> bool:
        return (
            self.daily_budget_usd is None
            and self.total_budget_usd is None
            and self.max_concurrent_streams is None
        )


_UNLIMITED = {"", "none", "unlimited", "off", "no"}


def _limit_env(name: str, cast):
    """An optional limit: unset, empty, 'unlimited' or negative = no limit."""
    raw = _env(name).lower()
    if raw in _UNLIMITED:
        return None
    try:
        value = cast(float(raw))
    except ValueError:
        return None
    return None if value < 0 else value


def budget_settings() -> BudgetSettings:
    return BudgetSettings(
        daily_budget_usd=_limit_env("ASSEMBLYAI_DAILY_BUDGET_USD", float),
        total_budget_usd=_limit_env("ASSEMBLYAI_TOTAL_BUDGET_USD", float),
        max_concurrent_streams=_limit_env("ASSEMBLYAI_MAX_CONCURRENT_STREAMS", int),
        reserve_minutes_per_call=max(
            _float_env("ASSEMBLYAI_RESERVE_MINUTES_PER_CALL", 5.0), 0.0
        ),
    )


# --------------------------------------------------------------------------- #
# Rollout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RolloutSettings:
    mode: str
    canary_org_ids: frozenset[int]
    canary_workflow_ids: frozenset[int]
    fallback_provider: str
    platform_default_provider: str

    def as_dict(self) -> dict:
        data = asdict(self)
        data["canary_org_ids"] = sorted(self.canary_org_ids)
        data["canary_workflow_ids"] = sorted(self.canary_workflow_ids)
        return data


def platform_default_stt_provider() -> str:
    """STT provider new configurations are preselected with."""
    return _env("STT_PLATFORM_DEFAULT_PROVIDER", ASSEMBLYAI_PROVIDER).lower()


def rollout_settings() -> RolloutSettings:
    mode = _env("ASSEMBLYAI_STT_ROLLOUT", "off").lower()
    if mode not in ROLLOUT_MODES:
        mode = "off"
    return RolloutSettings(
        mode=mode,
        canary_org_ids=_id_set("ASSEMBLYAI_STT_CANARY_ORG_IDS"),
        canary_workflow_ids=_id_set("ASSEMBLYAI_STT_CANARY_WORKFLOW_IDS"),
        fallback_provider=_env("ASSEMBLYAI_FALLBACK_PROVIDER").lower(),
        platform_default_provider=platform_default_stt_provider(),
    )


def in_rollout(
    settings: RolloutSettings, *, organization_id: int | None, workflow_id: int | None
) -> bool:
    if settings.mode == "global":
        return True
    if settings.mode == "canary":
        return (organization_id in settings.canary_org_ids) or (
            workflow_id in settings.canary_workflow_ids
        )
    return False


# --------------------------------------------------------------------------- #
# Request shaping
# --------------------------------------------------------------------------- #


def language_codes_for(language: str | None) -> list[str] | None:
    """Map Dograh's language setting to AssemblyAI ``language_codes``.

    ``multi`` (and an unset language) declares English + Spanish; the model
    code-switches natively between the declared languages.
    """
    if not language or language in {"multi", "auto", "unknown"}:
        return list(ASSEMBLYAI_MULTI_LANGUAGE_CODES)
    return [language.split("-")[0].lower()]


def sanitize_keyterms(terms: list[str] | None) -> list[str] | None:
    """Dedupe, drop terms AssemblyAI would ignore (>50 chars), cap at 100."""
    if not terms:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        term = " ".join(str(term).split())
        if not term or len(term) > ASSEMBLYAI_MAX_KEYTERM_CHARS:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= ASSEMBLYAI_MAX_KEYTERMS:
            break
    return out or None
