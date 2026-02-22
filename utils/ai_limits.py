# utils/ai_limits.py
from __future__ import annotations

from utils.redis_rate_limiter import RedisSlidingWindowLimiter
from utils.storage import get_guild_settings

_user_limiters: dict[int, RedisSlidingWindowLimiter] = {}
_guild_limiters: dict[int, RedisSlidingWindowLimiter] = {}
_scene_limiters: dict[int, RedisSlidingWindowLimiter] = {}

# Summary limiters
_summary_burst_limiters: dict[int, RedisSlidingWindowLimiter] = {}
_summary_daily_user_limiters: dict[int, RedisSlidingWindowLimiter] = {}
_summary_daily_guild_limiters: dict[int, RedisSlidingWindowLimiter] = {}


async def get_user_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)
    max_events = int(s.get("ai_user_max", 3) or 3)
    window = int(s.get("ai_user_window", 30) or 30)

    limiter = _user_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:ai:user:{int(guild_id)}",
        )
        _user_limiters[guild_id] = limiter
    return limiter


async def get_guild_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)
    max_events = int(s.get("ai_guild_max", 10) or 10)
    window = int(s.get("ai_guild_window", 30) or 30)

    limiter = _guild_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:ai:guild:{int(guild_id)}",
        )
        _guild_limiters[guild_id] = limiter
    return limiter


async def get_scene_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)
    max_events = int(s.get("scene_burst_max", 4) or 4)
    window = int(s.get("scene_burst_window", 30) or 30)

    limiter = _scene_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:scene:burst:{int(guild_id)}",
        )
        _scene_limiters[guild_id] = limiter
    return limiter


async def get_summary_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)
    max_events = int(s.get("scene_summary_burst_max", 2) or 2)
    window = int(s.get("scene_summary_burst_window", 30) or 30)

    limiter = _summary_burst_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:scene:summary:burst:{int(guild_id)}",
        )
        _summary_burst_limiters[guild_id] = limiter
    return limiter


async def get_summary_daily_user_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)
    max_events = int(s.get("scene_summary_daily_user_max", 8) or 8)
    window = int(s.get("scene_summary_daily_user_window", 86400) or 86400)

    limiter = _summary_daily_user_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:scene:summary:daily:user:{int(guild_id)}",
        )
        _summary_daily_user_limiters[guild_id] = limiter
    return limiter


async def get_summary_daily_guild_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    s = await get_guild_settings(guild_id)
    max_events = int(s.get("scene_summary_daily_guild_max", 30) or 30)
    window = int(s.get("scene_summary_daily_guild_window", 86400) or 86400)

    limiter = _summary_daily_guild_limiters.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:scene:summary:daily:guild:{int(guild_id)}",
        )
        _summary_daily_guild_limiters[guild_id] = limiter
    return limiter
