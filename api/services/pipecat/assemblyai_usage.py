"""AssemblyAI streaming usage, application-side budgets and concurrency.

Every AssemblyAI session is metered: seconds, estimated cost, sessions, errors
and fallbacks, by day and by organization, workflow and product, in Redis.

There are no limits by default. An operator may set optional caps (daily /
total estimated spend, concurrent streams); sessions on the *platform* key
(``ASSEMBLYAI_API_KEY``) are then admitted against them at call start, reserving
``ASSEMBLYAI_RESERVE_MINUTES_PER_CALL`` of estimated cost that the call's real
duration replaces when it ends. A call that is already running is never cut
off: limits only refuse *new* sessions.
Tenant (BYOK) sessions are metered but never refused, since the tenant pays.

Cost is AssemblyAI's billed session duration (the ``Termination`` message's
``session_duration_seconds``, or wall-clock connection time when the session
ended abnormally) times the configured price. It is an estimate, not the
vendor account balance.

If Redis is unreachable while a cap is configured, platform admission fails
closed (the cap cannot be checked). With no caps, calls are never refused;
metering is best effort.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger

from api.services.configuration.assemblyai import (
    BudgetSettings,
    budget_settings,
    estimate_cost_usd,
    price_per_hour,
)

PREFIX = "assemblyai:stt"
ACTIVE_KEY = f"{PREFIX}:active"  # zset member -> expiry ts
RESERVED_KEY = f"{PREFIX}:reserved"  # hash member -> reserved usd
TOTAL_USD_KEY = f"{PREFIX}:usd:total"
TOTAL_SECONDS_KEY = f"{PREFIX}:seconds:total"
DAY_TTL_S = 120 * 24 * 3600
# A session whose process died is dropped from the active set after this long
# (longer than any call; Dograh's max call duration defaults well below it).
ACTIVE_STALE_AFTER_S = 3 * 3600 + 600


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def day_key(date: str, name: str) -> str:
    return f"{PREFIX}:day:{date}:{name}"


# Atomic admission: prune stale actives, then check concurrency and both
# budgets (spent + already reserved + this reservation), then reserve.
# KEYS: active, reserved, day_usd, total_usd
# ARGV: member, now, expiry, reserve_usd, daily_budget, total_budget, max_streams
# Returns "ok" or a refusal reason.
_ADMIT_LUA = """
local stale = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
for _, m in ipairs(stale) do redis.call('HDEL', KEYS[2], m) end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 'ok' end
-- A negative limit means "no limit".
local active = redis.call('ZCARD', KEYS[1])
local max_streams = tonumber(ARGV[7])
if max_streams >= 0 and active >= max_streams then return 'concurrency' end
local reserved = 0
for _, v in ipairs(redis.call('HVALS', KEYS[2])) do reserved = reserved + tonumber(v) end
local want = tonumber(ARGV[4])
local day = tonumber(redis.call('GET', KEYS[3]) or '0')
local daily = tonumber(ARGV[5])
if daily >= 0 and day + reserved + want > daily then return 'daily_budget' end
local total = tonumber(redis.call('GET', KEYS[4]) or '0')
local total_cap = tonumber(ARGV[6])
if total_cap >= 0 and total + reserved + want > total_cap then return 'total_budget' end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[4])
return 'ok'
"""

_redis_client = None


async def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        from api.constants import REDIS_URL

        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str  # "ok" | "concurrency" | "daily_budget" | "total_budget" | ...


async def admit_platform_session(
    member: str,
    *,
    voice_focus: bool,
    settings: BudgetSettings | None = None,
    redis_client=None,
) -> Admission:
    """Reserve budget and a stream slot for a platform-key session."""
    settings = settings or budget_settings()
    if settings.max_concurrent_streams == 0:
        return Admission(False, "concurrency")

    def cap(value) -> str:
        return "-1" if value is None else str(value)

    reserve_usd = estimate_cost_usd(
        settings.reserve_minutes_per_call * 60, voice_focus=voice_focus
    )
    now = time.time()
    try:
        client = redis_client or await _get_redis()
        result = await client.eval(
            _ADMIT_LUA,
            4,
            ACTIVE_KEY,
            RESERVED_KEY,
            day_key(_today(), "usd"),
            TOTAL_USD_KEY,
            member,
            str(now),
            str(now + ACTIVE_STALE_AFTER_S),
            str(reserve_usd),
            cap(settings.daily_budget_usd),
            cap(settings.total_budget_usd),
            cap(settings.max_concurrent_streams),
        )
    except Exception as exc:
        if settings.unlimited:
            # Nothing to enforce: the stream is just not counted as active.
            return Admission(True, "ok")
        logger.warning(
            f"AssemblyAI budget store unavailable ({type(exc).__name__}); "
            "refusing new platform-key session"
        )
        return Admission(False, "budget_store_unavailable")
    reason = str(result)
    return Admission(reason == "ok", reason)


async def release_platform_session(member: str, *, redis_client=None) -> None:
    try:
        client = redis_client or await _get_redis()
        await client.zrem(ACTIVE_KEY, member)
        await client.hdel(RESERVED_KEY, member)
    except Exception as exc:
        logger.warning(
            f"AssemblyAI: could not release stream slot ({type(exc).__name__})"
        )


async def _incr_counts(fields: dict[str, int], *, redis_client=None) -> None:
    try:
        client = redis_client or await _get_redis()
        key = day_key(_today(), "counts")
        for name, amount in fields.items():
            await client.hincrby(key, name, amount)
        await client.expire(key, DAY_TTL_S)
    except Exception:
        pass


async def record_fallback(
    reason: str, *, organization_id: int | None, redis_client=None
) -> None:
    """Count a call that could not use AssemblyAI and why."""
    logger.warning(
        f"AssemblyAI STT not used for this call (org={organization_id}): {reason}"
    )
    await _incr_counts(
        {"fallbacks": 1, f"fallback:{reason}": 1}, redis_client=redis_client
    )


async def record_refusal(reason: str, *, redis_client=None) -> None:
    await _incr_counts(
        {"refused": 1, f"refused:{reason}": 1}, redis_client=redis_client
    )


@dataclass
class AssemblyAIUsageSession:
    """Meters one call's AssemblyAI usage (possibly several connections)."""

    member: str
    organization_id: int | None
    workflow_id: int | None
    product: str | None
    credential_source: str  # "byok" | "platform"
    voice_focus: bool
    model: str
    redis_client: object | None = None
    billed_seconds: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)
    connections: int = 0
    _finished: bool = False

    def connection_opened(self) -> None:
        self.connections += 1

    def add_billed_seconds(self, seconds: float) -> None:
        if seconds and seconds > 0:
            self.billed_seconds += float(seconds)

    def record_error(self, code: int | str) -> None:
        key = str(code)
        self.errors[key] = self.errors.get(key, 0) + 1

    @property
    def estimated_cost_usd(self) -> float:
        return estimate_cost_usd(self.billed_seconds, voice_focus=self.voice_focus)

    async def finish(self) -> None:
        """Persist usage and release the stream slot. Safe to call twice."""
        if self._finished:
            return
        self._finished = True
        seconds = round(self.billed_seconds, 3)
        cost = self.estimated_cost_usd
        date = _today()
        try:
            client = self.redis_client or await _get_redis()
            pipe = client.pipeline(transaction=False)
            pipe.incrbyfloat(day_key(date, "seconds"), seconds)
            pipe.incrbyfloat(day_key(date, "usd"), cost)
            pipe.incrbyfloat(TOTAL_SECONDS_KEY, seconds)
            pipe.incrbyfloat(TOTAL_USD_KEY, cost)
            if self.credential_source == "platform":
                pipe.incrbyfloat(day_key(date, "platform_usd"), cost)
            for dim, value in (
                ("org", self.organization_id),
                ("workflow", self.workflow_id),
                ("product", self.product or "unknown"),
            ):
                if value is not None:
                    pipe.hincrbyfloat(
                        day_key(date, f"seconds_by_{dim}"), str(value), seconds
                    )
                    pipe.expire(day_key(date, f"seconds_by_{dim}"), DAY_TTL_S)
            counts = day_key(date, "counts")
            pipe.hincrby(counts, "sessions", 1)
            pipe.hincrby(counts, f"sessions:{self.credential_source}", 1)
            pipe.hincrby(counts, "connections", self.connections)
            if self.errors:
                pipe.hincrby(counts, "sessions_with_errors", 1)
                for code, n in self.errors.items():
                    pipe.hincrby(counts, f"error:{code}", n)
            pipe.expire(counts, DAY_TTL_S)
            for name in ("seconds", "usd", "platform_usd"):
                pipe.expire(day_key(date, name), DAY_TTL_S)
            await pipe.execute()
        except Exception as exc:
            logger.warning(f"AssemblyAI usage not recorded ({type(exc).__name__})")
        if self.credential_source == "platform":
            await release_platform_session(self.member, redis_client=self.redis_client)
        logger.info(
            f"AssemblyAI usage: org={self.organization_id} workflow={self.workflow_id} "
            f"product={self.product} source={self.credential_source} "
            f"seconds={seconds} est_cost_usd={cost:.4f} connections={self.connections} "
            f"errors={self.errors or 0}"
        )


async def usage_snapshot(*, days: int = 7, redis_client=None) -> dict:
    """Operator view of metered usage. Estimates only; not the vendor balance."""
    settings = budget_settings()
    out: dict = {
        "note": (
            "Estimated from session durations at the configured price. This is "
            "not AssemblyAI's account balance."
        ),
        "price_per_hour_usd": price_per_hour(voice_focus=False),
        "price_per_hour_with_voice_focus_usd": price_per_hour(voice_focus=True),
        "budgets": {
            "daily_budget_usd": "unlimited"
            if settings.daily_budget_usd is None
            else settings.daily_budget_usd,
            "total_budget_usd": "unlimited"
            if settings.total_budget_usd is None
            else settings.total_budget_usd,
            "max_concurrent_streams": "unlimited"
            if settings.max_concurrent_streams is None
            else settings.max_concurrent_streams,
            "reserve_minutes_per_call": settings.reserve_minutes_per_call,
        },
        "days": [],
    }
    try:
        client = redis_client or await _get_redis()
        now = time.time()
        await client.zremrangebyscore(ACTIVE_KEY, "-inf", now)
        out["active_platform_streams"] = int(await client.zcard(ACTIVE_KEY))
        reserved = await client.hvals(RESERVED_KEY)
        out["reserved_usd"] = round(sum(float(v) for v in reserved), 6)
        out["total_seconds"] = float(await client.get(TOTAL_SECONDS_KEY) or 0)
        out["total_estimated_usd"] = round(
            float(await client.get(TOTAL_USD_KEY) or 0), 6
        )
        today = datetime.now(UTC).date()
        for i in range(max(days, 1)):
            date = (today - timedelta(days=i)).isoformat()
            counts = await client.hgetall(day_key(date, "counts")) or {}
            seconds = float(await client.get(day_key(date, "seconds")) or 0)
            sessions = int(counts.get("sessions", 0))
            out["days"].append(
                {
                    "date": date,
                    "minutes": round(seconds / 60, 2),
                    "estimated_usd": round(
                        float(await client.get(day_key(date, "usd")) or 0), 6
                    ),
                    "sessions": sessions,
                    "error_rate": (
                        round(int(counts.get("sessions_with_errors", 0)) / sessions, 4)
                        if sessions
                        else 0.0
                    ),
                    "fallbacks": int(counts.get("fallbacks", 0)),
                    "refused": int(counts.get("refused", 0)),
                    "counts": {k: int(v) for k, v in counts.items()},
                    "minutes_by_org": _minutes(
                        await client.hgetall(day_key(date, "seconds_by_org"))
                    ),
                    "minutes_by_workflow": _minutes(
                        await client.hgetall(day_key(date, "seconds_by_workflow"))
                    ),
                    "minutes_by_product": _minutes(
                        await client.hgetall(day_key(date, "seconds_by_product"))
                    ),
                }
            )
    except Exception as exc:
        out["error"] = f"usage store unavailable ({type(exc).__name__})"
    return out


def _minutes(values: dict | None) -> dict[str, float]:
    return {k: round(float(v) / 60, 2) for k, v in (values or {}).items()}
