"""Call-start paid STT fallback for HopSTT. Disabled by default.

Only at call start, and only when every HopSTT endpoint fails a fast
``/readyz`` check, the call may be built with a paid STT provider instead —
and only if the operator explicitly enabled it and today's minute budget is
not spent. A call that started on HopSTT never switches provider mid-call;
mid-call outages are handled by HopSTTService's reconnect logic.

Environment:
  HOPSTT_PAID_FALLBACK_ENABLED            "true" to allow it (default false).
  HOPSTT_PAID_FALLBACK_PROVIDER           STT provider id, e.g. "cartesia"
                                          (default empty = off).
  HOPSTT_PAID_FALLBACK_MAX_MINUTES_PER_DAY  daily budget (default 0 = off).
  HOPSTT_PAID_FALLBACK_RESERVE_MINUTES_PER_CALL  minutes charged against the
                                          budget per fallback call (default 5).
                                          The real call length is unknown at
                                          start, so the budget is a
                                          conservative reservation, not a
                                          metered total.
  HOPSTT_HEALTH_TIMEOUT_S                 total health-check time (default 0.8).

Credentials come from the organization's existing configuration for the
fallback provider (its v2 STT block, or its TTS block when the same provider,
e.g. one Cartesia key serves both). No key, no fallback. Every fallback logs a
WARNING and increments ``hopstt:paid_fallback:count:<UTC date>``.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime

import aiohttp
from loguru import logger

from api.services.configuration.hopstt import env_flag, resolve_hopstt_endpoints
from api.services.configuration.registry import REGISTRY, ServiceProviders, ServiceType
from api.services.pipecat.hopstt_stt import http_base_url

_DAY_TTL_S = 2 * 24 * 3600

# Atomic "reserve N minutes if still under budget". Returns -1 when refused.
_RESERVE_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur + tonumber(ARGV[1]) > tonumber(ARGV[2]) then return '-1' end
local v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return v
"""

# Used only when Redis is unreachable: per-process, so with N workers the
# effective budget can be up to N x the configured one. Better than failing
# open with no limit, and never better than Redis.
_local_minutes: dict[str, float] = {}
_local_counts: dict[str, int] = {}

_redis_client = None


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def paid_fallback_settings() -> tuple[bool, str, float, float]:
    """(enabled, provider, max_minutes_per_day, reserve_minutes_per_call)."""
    enabled = env_flag("HOPSTT_PAID_FALLBACK_ENABLED", False)
    provider = os.getenv("HOPSTT_PAID_FALLBACK_PROVIDER", "").strip().lower()
    max_minutes = _float_env("HOPSTT_PAID_FALLBACK_MAX_MINUTES_PER_DAY", 0.0)
    reserve = _float_env("HOPSTT_PAID_FALLBACK_RESERVE_MINUTES_PER_CALL", 5.0)
    return enabled, provider, max_minutes, max(reserve, 0.0)


async def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        from api.constants import REDIS_URL

        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def reserve_budget(
    minutes: float, max_minutes: float, *, redis_client=None
) -> bool:
    """Reserve ``minutes`` of today's budget; False when it would exceed it."""
    if max_minutes <= 0 or minutes > max_minutes:
        return False
    key = f"hopstt:paid_fallback:minutes:{_today()}"
    try:
        client = redis_client or await _get_redis()
        result = await client.eval(
            _RESERVE_LUA, 1, key, str(minutes), str(max_minutes), str(_DAY_TTL_S)
        )
        return float(result) >= 0
    except Exception as exc:
        logger.warning(
            f"HopSTT fallback budget: Redis unavailable ({type(exc).__name__}); "
            "using the per-process budget"
        )
    used = _local_minutes.get(key, 0.0)
    if used + minutes > max_minutes:
        return False
    _local_minutes[key] = used + minutes
    return True


async def _count_fallback(redis_client=None) -> None:
    key = f"hopstt:paid_fallback:count:{_today()}"
    _local_counts[key] = _local_counts.get(key, 0) + 1
    try:
        client = redis_client or await _get_redis()
        await client.incr(key)
        await client.expire(key, _DAY_TTL_S)
    except Exception:
        pass


async def any_endpoint_ready(
    endpoints: list[str], *, timeout_s: float = 0.8
) -> tuple[bool, str]:
    """True when at least one endpoint answers ``/readyz`` 200 within timeout."""
    if not endpoints:
        return False, "no endpoints configured"
    reasons: list[str] = []

    async def one(session: aiohttp.ClientSession, ep: str) -> bool:
        try:
            async with session.get(f"{http_base_url(ep)}/readyz") as resp:
                if resp.status == 200:
                    return True
                reasons.append(f"http_{resp.status}")
        except Exception as exc:
            reasons.append(type(exc).__name__)
        return False

    started = time.monotonic()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            tasks = [asyncio.ensure_future(one(session, ep)) for ep in endpoints]
            try:
                for coro in asyncio.as_completed(tasks, timeout=timeout_s):
                    if await coro:
                        return True, "ready"
            except asyncio.TimeoutError:
                reasons.append("timeout")
            finally:
                for task in tasks:
                    task.cancel()
    except Exception as exc:
        reasons.append(type(exc).__name__)
    elapsed = int((time.monotonic() - started) * 1000)
    summary = ",".join(sorted(set(reasons)))[:120] or "unknown"
    return False, f"{len(endpoints)} endpoint(s) not ready in {elapsed} ms ({summary})"


def _keys_of(service) -> list[str]:
    if service is None or not hasattr(service, "get_all_api_keys"):
        return []
    return [k for k in service.get_all_api_keys() if k]


async def _find_fallback_stt_config(provider: str, user_config, organization_id):
    """Build the fallback provider's STT config from the org's own credentials."""
    stt_cls = REGISTRY[ServiceType.STT].get(provider)
    if stt_cls is None or provider == ServiceProviders.HOPSTT.value:
        return None

    candidates = []
    if organization_id is not None:
        try:
            from api.schemas.ai_model_configuration import (
                compile_ai_model_configuration_v2,
            )
            from api.services.configuration.ai_model_configuration import (
                get_organization_ai_model_configuration_v2,
            )

            org = await get_organization_ai_model_configuration_v2(organization_id)
            if org is not None:
                effective = compile_ai_model_configuration_v2(org)
                candidates += [effective.stt, effective.tts]
        except Exception as exc:
            logger.debug(
                f"HopSTT fallback: org config unavailable ({type(exc).__name__})"
            )
    candidates.append(getattr(user_config, "tts", None))

    for service in candidates:
        if service is None or getattr(service, "provider", None) != provider:
            continue
        if isinstance(service, stt_cls):
            return service
        keys = _keys_of(service)
        if keys:
            return stt_cls(provider=provider, api_key=keys)
    return None


async def apply_hopstt_paid_fallback(
    user_config,
    *,
    organization_id: int | None,
    redis_client=None,
):
    """Return ``user_config``, or a copy with a paid STT if fallback applies."""
    stt = getattr(user_config, "stt", None)
    if stt is None or getattr(stt, "provider", None) != ServiceProviders.HOPSTT.value:
        return user_config
    enabled, provider, max_minutes, reserve = paid_fallback_settings()
    if not (enabled and provider and max_minutes > 0):
        return user_config  # off: no health check, no cost

    endpoints = resolve_hopstt_endpoints(stt)
    ready, reason = await any_endpoint_ready(
        endpoints, timeout_s=_float_env("HOPSTT_HEALTH_TIMEOUT_S", 0.8)
    )
    if ready:
        return user_config

    fallback = await _find_fallback_stt_config(provider, user_config, organization_id)
    if fallback is None:
        logger.warning(
            f"HopSTT unhealthy at call start ({reason}) but no {provider} "
            "credentials in this organization's config; staying on HopSTT"
        )
        return user_config
    if not await reserve_budget(reserve, max_minutes, redis_client=redis_client):
        logger.warning(
            f"HopSTT unhealthy at call start ({reason}); paid fallback budget "
            f"of {max_minutes:g} min/day reached; staying on HopSTT"
        )
        return user_config

    await _count_fallback(redis_client)
    logger.warning(
        f"HopSTT unhealthy at call start ({reason}); using paid STT fallback "
        f"provider={provider} (reserved {reserve:g} min of {max_minutes:g} min/day)"
    )
    return user_config.model_copy(update={"stt": fallback})
