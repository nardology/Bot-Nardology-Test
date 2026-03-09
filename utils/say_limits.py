# utils/say_limits.py
from __future__ import annotations

from utils.redis_rate_limiter import RedisSlidingWindowLimiter
from utils.storage import get_guild_settings

_user_limiters: dict[int, RedisSlidingWindowLimiter] = {}
_guild_limiters: dict[int, RedisSlidingWindowLimiter] = {}


async def get_say_user_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)

    # Defaults (free-like)
    max_events = int(s.get("say_user_max", 1) or 1)
    window = int(s.get("say_user_window", 5) or 5)

    limiter = _user_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:say:user:{int(guild_id)}",
        )
        _user_limiters[guild_id] = limiter
    return limiter


async def get_say_guild_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)

    max_events = int(s.get("say_guild_max", 3) or 3)
    window = int(s.get("say_guild_window", 30) or 30)

    limiter = _guild_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:say:guild:{int(guild_id)}",
        )
        _guild_limiters[guild_id] = limiter
    return limiter
