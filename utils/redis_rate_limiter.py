# utils/redis_rate_limiter.py
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from utils.backpressure import get_redis_or_none


@dataclass
class LimitResult:
    allowed: bool
    retry_after_seconds: int = 0
    used: int = 0
    remaining: int = 0


_LUA_SLIDING_WINDOW = '-- KEYS[1] = zset key\n-- ARGV[1] = now_ms\n-- ARGV[2] = window_ms\n-- ARGV[3] = limit\n-- ARGV[4] = member\nlocal key = KEYS[1]\nlocal now = tonumber(ARGV[1])\nlocal window = tonumber(ARGV[2])\nlocal limit = tonumber(ARGV[3])\nlocal member = ARGV[4]\n\nlocal min_score = now - window\nredis.call("ZREMRANGEBYSCORE", key, 0, min_score)\n\nlocal count = redis.call("ZCARD", key)\nif count >= limit then\n  local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")\n  local oldest_score = tonumber(oldest[2]) or now\n  local retry_ms = (oldest_score + window) - now\n  if retry_ms < 0 then retry_ms = 0 end\n  return {0, count, retry_ms}\nend\n\nredis.call("ZADD", key, now, member)\nlocal ttl = math.floor(window / 1000) + 5\nredis.call("EXPIRE", key, ttl)\nreturn {1, count + 1, 0}'


class RedisSlidingWindowLimiter:
    """Redis-backed sliding-window limiter (sorted set + Lua for atomicity).

    Keys are zsets. Each request inserts a unique member with score=now_ms.
    Old entries are trimmed within the same Lua call.
    """

    def __init__(self, *, max_events: int, window_seconds: int, key_prefix: str) -> None:
        self.max_events = int(max_events)
        self.window_seconds = int(window_seconds)
        self.key_prefix = key_prefix.rstrip(":")

    def _k(self, key: str) -> str:
        # caller passes something like "user:123" or "guild:456"
        return f"{self.key_prefix}:{key}"

    async def check(self, key: str) -> LimitResult:
        r = await get_redis_or_none()
        if r is None:
            # Fail closed: if Redis is unavailable we can't enforce rate limits safely.
            return LimitResult(allowed=False, retry_after_seconds=60, used=0, remaining=0)
        now_ms = int(time.time() * 1000)
        window_ms = int(self.window_seconds * 1000)
        member = str(uuid.uuid4())

        k = self._k(key)
        allowed, used, retry_ms = await r.eval(_LUA_SLIDING_WINDOW, 1, k, now_ms, window_ms, self.max_events, member)

        allowed = bool(int(allowed))
        used = int(used)
        retry_s = int((int(retry_ms) + 999) // 1000) if not allowed else 0
        remaining = max(0, self.max_events - used) if allowed else 0

        return LimitResult(allowed=allowed, retry_after_seconds=retry_s, used=used, remaining=remaining)

    async def peek(self, key: str) -> LimitResult:
        """Best-effort view of current window usage (no mutation)."""
        r = await get_redis_or_none()
        if r is None:
            # Best-effort: show "unknown" usage as empty, but do not block purely for viewing.
            return LimitResult(allowed=True, retry_after_seconds=0, used=0, remaining=self.max_events)
        now_ms = int(time.time() * 1000)
        window_ms = int(self.window_seconds * 1000)
        k = self._k(key)

        # Trim old entries to keep counts accurate, but do not add a new one.
        await r.zremrangebyscore(k, 0, now_ms - window_ms)
        used = int(await r.zcard(k))
        if used >= self.max_events:
            oldest = await r.zrange(k, 0, 0, withscores=True)
            oldest_score = int(oldest[0][1]) if oldest else now_ms
            retry_ms = (oldest_score + window_ms) - now_ms
            retry_s = int((max(0, retry_ms) + 999) // 1000)
            return LimitResult(allowed=False, retry_after_seconds=retry_s, used=used, remaining=0)
        return LimitResult(allowed=True, retry_after_seconds=0, used=used, remaining=max(0, self.max_events - used))
