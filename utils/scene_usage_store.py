from __future__ import annotations

from datetime import datetime, timezone, timedelta

from utils.redis_kv import incr
from utils.backpressure import get_redis_or_none

# Day-bucket counters with TTL.
# Keys (turn counts):
#   scene:turns:guild:{guild_id}:{YYYYMMDD}
#   scene:turns:user:{guild_id}:{user_id}:{YYYYMMDD}
# Keys (token usage):
#   scene:tokens:guild:{guild_id}:{YYYYMMDD}
#   scene:tokens:user:{guild_id}:{user_id}:{YYYYMMDD}

_TTL = 8 * 24 * 3600


def _day_key(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d")


def _guild_key(guild_id: int, day: str) -> str:
    return f"scene:turns:guild:{int(guild_id)}:{day}"


def _user_key(guild_id: int, user_id: int, day: str) -> str:
    return f"scene:turns:user:{int(guild_id)}:{int(user_id)}:{day}"


def _guild_tokens_key(guild_id: int, day: str) -> str:
    return f"scene:tokens:guild:{int(guild_id)}:{day}"


def _user_tokens_key(guild_id: int, user_id: int, day: str) -> str:
    return f"scene:tokens:user:{int(guild_id)}:{int(user_id)}:{day}"


async def insert_scene_turn(*, guild_id: int, user_id: int, scene_id: int) -> None:
    # scene_id not used for counts (kept for signature compatibility)
    now = datetime.now(timezone.utc)
    day = _day_key(now)
    await incr(_guild_key(guild_id, day), 1, ex=_TTL)
    await incr(_user_key(guild_id, user_id, day), 1, ex=_TTL)


async def insert_scene_tokens(*, guild_id: int, user_id: int, tokens: int) -> None:
    """Record actual token usage for token-based budget enforcement."""
    if tokens <= 0:
        return
    now = datetime.now(timezone.utc)
    day = _day_key(now)
    await incr(_guild_tokens_key(guild_id, day), tokens, ex=_TTL)
    await incr(_user_tokens_key(guild_id, user_id, day), tokens, ex=_TTL)


async def _sum_range(keys: list[str]) -> int:
    if not keys:
        return 0
    r = await get_redis_or_none()
    if r is None:
        return 0
    vals = await r.mget(keys)
    total = 0
    for v in vals or []:
        if v is None:
            continue
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="ignore")
        try:
            total += int(v)
        except Exception:
            pass
    return total


async def count_scene_turns_guild_since(*, guild_id: int, since_utc: datetime) -> int:
    since_utc = (since_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    # Sum day buckets from since..now (inclusive)
    days = []
    cur = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        days.append(_day_key(cur))
        cur += timedelta(days=1)
    keys = [_guild_key(guild_id, d) for d in days]
    return await _sum_range(keys)


async def count_scene_turns_user_since(*, guild_id: int, user_id: int, since_utc: datetime) -> int:
    since_utc = (since_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    days = []
    cur = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        days.append(_day_key(cur))
        cur += timedelta(days=1)
    keys = [_user_key(guild_id, user_id, d) for d in days]
    return await _sum_range(keys)


# ---------------------------------------------------------------------------
# Token-based counters (for token budget enforcement)
# ---------------------------------------------------------------------------

async def count_scene_tokens_guild_since(*, guild_id: int, since_utc: datetime) -> int:
    since_utc = (since_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    days = []
    cur = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        days.append(_day_key(cur))
        cur += timedelta(days=1)
    return await _sum_range([_guild_tokens_key(guild_id, d) for d in days])


async def count_scene_tokens_user_since(*, guild_id: int, user_id: int, since_utc: datetime) -> int:
    since_utc = (since_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    days = []
    cur = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        days.append(_day_key(cur))
        cur += timedelta(days=1)
    return await _sum_range([_user_tokens_key(guild_id, user_id, d) for d in days])
