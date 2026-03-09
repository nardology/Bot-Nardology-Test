# utils/backpressure.py
from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, Tuple

from redis.asyncio import Redis
from redis.asyncio.client import Redis as RedisType


# =========================
# Circuit breaker (existing)
# =========================
_lock = asyncio.Lock()
_until: float = 0.0

def now() -> float:
    return time.time()

async def is_open() -> int:
    """Returns remaining seconds if breaker is open, else 0."""
    async with _lock:
        rem = int(_until - now())
        return rem if rem > 0 else 0

async def trip(seconds: int) -> None:
    """Open breaker for N seconds."""
    global _until
    seconds = max(1, int(seconds))
    async with _lock:
        prev = float(_until)
        _until = max(_until, now() + seconds)

    # Best-effort incident signal if this is a "new" open or a substantial extension.
    try:
        if _until > prev + 1:
            from utils.incidents import record_incident

            await record_incident(
                kind="circuit_breaker_open",
                reason=f"Circuit breaker tripped for ~{seconds}s",
                fields={"seconds": int(seconds)},
            )
    except Exception:
        pass


# =========================
# Redis-based concurrency
# =========================

@dataclass(frozen=True)
class AcquireResult:
    ok: bool
    mode: str              # "acquired" | "rejected" | "queued_timeout"
    waited_s: int
    retry_after_s: int
    debug: str = ""


# Redis client singleton (per process)
_redis: Optional[RedisType] = None
_redis_lock = asyncio.Lock()

# Local (in-process) fallback when Redis is unavailable.
# This prevents crash-loops and keeps the bot responsive in a degraded mode.
_local_global_sem: Optional[asyncio.Semaphore] = None
_local_guild_sems: dict[int, asyncio.Semaphore] = {}
_local_sem_lock = asyncio.Lock()

def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        v = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        v = default
    return max(min_value, v)

def _redis_url() -> str:
    # Railway Redis plugin often provides REDIS_URL and/or REDIS_PRIVATE_URL.
    # We support both to avoid crash-loops caused by env-var name mismatches.
    url = (
        (os.getenv("REDIS_URL") or "").strip()
        or (os.getenv("REDIS_PRIVATE_URL") or "").strip()
        or (os.getenv("REDIS_PUBLIC_URL") or "").strip()
    )
    if not url:
        raise RuntimeError("Redis URL is missing. Set REDIS_URL (or REDIS_PRIVATE_URL).")
    return url

async def get_redis() -> RedisType:
    global _redis
    if _redis is not None:
        return _redis
    async with _redis_lock:
        if _redis is not None:
            return _redis
        _redis = Redis.from_url(
            _redis_url(),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        return _redis


async def get_redis_or_none() -> Optional[RedisType]:
    """Best-effort Redis getter.

    Returns None if Redis is missing/unavailable.
    Callers should degrade gracefully instead of crashing the bot.
    """
    try:
        r = await get_redis()
        return r
    except Exception:
        return None

def _k_global() -> str:
    return "ai:conc:global"

def _k_guild(guild_id: int) -> str:
    return f"ai:conc:guild:{int(guild_id)}"

def _k_lease(lease_id: str) -> str:
    return f"ai:lease:{lease_id}"

def _limits() -> tuple[int, int]:
    g = _get_env_int("AI_CONCURRENCY_GLOBAL", 10, min_value=1)
    pg = _get_env_int("AI_CONCURRENCY_PER_GUILD", 2, min_value=1)
    return g, pg

def _lease_ttl_s() -> int:
    # TTL must exceed your AI timeout (40s) + some buffer
    return _get_env_int("AI_LEASE_TTL_S", 70, min_value=20)

def _queue_wait_s(tier: str) -> int:
    # free rejects by default (0s wait), pro queues a bit
    if (tier or "").strip().lower() == "pro":
        return _get_env_int("AI_QUEUE_WAIT_PRO", 12, min_value=0)
    return _get_env_int("AI_QUEUE_WAIT_FREE", 0, min_value=0)

def _sleep_step_s() -> float:
    return float(os.getenv("AI_QUEUE_POLL_S", "0.25").strip() or "0.25")


# Lua: atomic try-acquire
# - ensures global+guild caps
# - increments both counters if allowed
# - writes a lease key so we can safely release later
_LUA_ACQUIRE = """
local k_global = KEYS[1]
local k_guild  = KEYS[2]
local k_lease  = KEYS[3]

local global_limit = tonumber(ARGV[1])
local guild_limit  = tonumber(ARGV[2])
local lease_ttl_ms = tonumber(ARGV[3])

-- current counts
local g = tonumber(redis.call('GET', k_global) or '0')
local s = tonumber(redis.call('GET', k_guild)  or '0')

if g >= global_limit then
  return {0, g, s, 'global_full'}
end
if s >= guild_limit then
  return {0, g, s, 'guild_full'}
end

-- acquire
g = g + 1
s = s + 1
redis.call('SET', k_global, g)
redis.call('SET', k_guild,  s)

-- ensure stale counters don't stick around forever (ex: crash without release)
redis.call('PEXPIRE', k_global, lease_ttl_ms)
redis.call('PEXPIRE', k_guild,  lease_ttl_ms)

-- keep counters from sticking forever if a worker crashes mid-flight
redis.call('PEXPIRE', k_global, lease_ttl_ms)
redis.call('PEXPIRE', k_guild,  lease_ttl_ms)

-- lease key (for safe release)
redis.call('SET', k_lease, '1', 'PX', lease_ttl_ms)

return {1, g, s, 'acquired'}
"""

# Lua: atomic release
_LUA_RELEASE = """
local k_global = KEYS[1]
local k_guild  = KEYS[2]
local k_lease  = KEYS[3]

-- Only release once: if lease key is missing, do nothing
local existed = redis.call('DEL', k_lease)
if existed == 0 then
  return {0, 'no_lease'}
end

local g = tonumber(redis.call('GET', k_global) or '0')
local s = tonumber(redis.call('GET', k_guild)  or '0')

if g > 0 then g = g - 1 end
if s > 0 then s = s - 1 end

redis.call('SET', k_global, g)
redis.call('SET', k_guild,  s)

return {1, g, s, 'released'}
"""

async def _try_acquire(redis: RedisType, *, guild_id: int, lease_id: str) -> tuple[bool, str]:
    glimit, glimit_guild = _limits()
    ttl_ms = _lease_ttl_s() * 1000
    keys = [_k_global(), _k_guild(guild_id), _k_lease(lease_id)]
    args = [str(glimit), str(glimit_guild), str(ttl_ms)]
    res = await redis.eval(_LUA_ACQUIRE, len(keys), *keys, *args)
    ok = bool(int(res[1-1]))  # res[0]
    reason = str(res[3]) if len(res) >= 4 else "unknown"
    return ok, reason

async def _release(redis: RedisType, *, guild_id: int, lease_id: str) -> None:
    keys = [_k_global(), _k_guild(guild_id), _k_lease(lease_id)]
    await redis.eval(_LUA_RELEASE, len(keys), *keys)

async def acquire_ai_slot(
    *,
    guild_id: int,
    tier: str,
) -> tuple[AcquireResult, Optional[str]]:
    """
    Returns (result, lease_id).
    If ok, lease_id is non-None and must be released via release_ai_slot().
    """
    # If Redis is unavailable, fall back to local semaphores.
    r = await get_redis_or_none()
    lease_id = uuid.uuid4().hex

    if r is None:
        glimit, glimit_guild = _limits()
        async with _local_sem_lock:
            global _local_global_sem
            if _local_global_sem is None:
                _local_global_sem = asyncio.Semaphore(glimit)
            if int(guild_id) not in _local_guild_sems:
                _local_guild_sems[int(guild_id)] = asyncio.Semaphore(glimit_guild)
            gsem = _local_global_sem
            ssem = _local_guild_sems[int(guild_id)]

        # try acquire immediately (no queueing logic here; keep it simple)
        if gsem.locked() or ssem.locked():
            return AcquireResult(
                ok=False,
                mode="rejected",
                waited_s=0,
                retry_after_s=10,
                debug="redis_unavailable_local_full",
            ), None

        await gsem.acquire()
        await ssem.acquire()
        return AcquireResult(
            ok=True,
            mode="degraded_local",
            waited_s=0,
            retry_after_s=0,
            debug="redis_unavailable_local_ok",
        ), lease_id
    max_wait = _queue_wait_s(tier)
    step = _sleep_step_s()

    start = time.time()
    waited = 0

    while True:
        ok, reason = await _try_acquire(r, guild_id=guild_id, lease_id=lease_id)
        if ok:
            waited = int(time.time() - start)
            return AcquireResult(ok=True, mode="acquired", waited_s=waited, retry_after_s=0, debug=reason), lease_id

        # free tier: reject immediately by default
        waited = int(time.time() - start)
        if max_wait <= 0:
            # small suggested retry; you can tune based on reason
            retry = 10 if reason in ("global_full", "guild_full") else 5
            return AcquireResult(ok=False, mode="rejected", waited_s=waited, retry_after_s=retry, debug=reason), None

        # pro tier: queue up to max_wait seconds
        if waited >= max_wait:
            retry = 5
            return AcquireResult(ok=False, mode="queued_timeout", waited_s=waited, retry_after_s=retry, debug=reason), None

        await asyncio.sleep(step)

async def release_ai_slot(*, guild_id: int, lease_id: str) -> None:
    try:
        r = await get_redis_or_none()
        if r is not None:
            await _release(r, guild_id=guild_id, lease_id=lease_id)
            return

        # Local fallback release
        async with _local_sem_lock:
            gsem = _local_global_sem
            ssem = _local_guild_sems.get(int(guild_id))
        try:
            if gsem is not None:
                gsem.release()
        except Exception:
            pass
        try:
            if ssem is not None:
                ssem.release()
        except Exception:
            pass
    except Exception:
        # Never crash a command handler on release failure
        pass


@asynccontextmanager
async def ai_slot(*, guild_id: int, tier: str):
    """
    Usage:
      async with ai_slot(guild_id=guild_id, tier=tier) as gate:
          if not gate.ok: ...
          else: call AI
    """
    result, lease = await acquire_ai_slot(guild_id=guild_id, tier=tier)
    try:
        yield result
    finally:
        if lease:
            await release_ai_slot(guild_id=guild_id, lease_id=lease)
