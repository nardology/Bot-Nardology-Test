from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from utils.backpressure import get_redis_or_none
from utils.redis_kv import incr


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d")


def _guild_count_key(guild_id: int, day: str) -> str:
    return f"feedback:count:guild:{int(guild_id)}:{day}"


def _guild_events_key(guild_id: int) -> str:
    return f"feedback:events:guild:{int(guild_id)}"


def _user_count_key(guild_id: int, user_id: int, day: str) -> str:
    """Per-user feedback counter scoped to a guild.

    We scope by guild_id to avoid one server's limits affecting another.
    """

    return f"feedback:count:guild:{int(guild_id)}:user:{int(user_id)}:{day}"


def _j(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _unj(s: str | bytes | None, default=None):
    if s is None:
        return default
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("utf-8", errors="ignore")
    try:
        return json.loads(s)
    except Exception:
        return default


@dataclass
class FeedbackItem:
    created_at: datetime
    guild_id: int
    channel_id: int
    user_id: int
    message: str
    attachments: list[dict]


async def insert_feedback(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    message: str,
    attachments: list[dict] | None = None,
) -> None:
    now = _now_utc()
    day = _day_key(now)
    # Count (guild + user)
    # Keep counters slightly longer than a week so rolling windows still work.
    ttl = 8 * 24 * 3600
    await incr(_guild_count_key(guild_id, day), 1, ex=ttl)
    await incr(_user_count_key(guild_id, user_id, day), 1, ex=ttl)

    # Store event (trim to last ~200 per guild)
    r = await get_redis_or_none()
    if r is None:
        return
    event = {
        "created_at": now.timestamp(),
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "user_id": int(user_id),
        "message": (message or "")[:2000],
        "attachments": attachments or [],
    }
    key = _guild_events_key(guild_id)
    pipe = r.pipeline()
    pipe.lpush(key, _j(event))
    pipe.ltrim(key, 0, 199)
    pipe.expire(key, 90 * 24 * 3600)  # keep 90 days
    await pipe.execute()


async def count_feedback_guild_since(*, guild_id: int, since_utc: datetime) -> int:
    since_utc = (since_utc or _now_utc()).astimezone(timezone.utc)
    now = _now_utc()
    days = []
    cur = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        days.append(_day_key(cur))
        cur += timedelta(days=1)
    r = await get_redis_or_none()
    if r is None:
        return 0
    vals = await r.mget([_guild_count_key(guild_id, d) for d in days])
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


async def count_feedback_user_since(*, guild_id: int, user_id: int, since_utc: datetime) -> int:
    """Count feedback submissions by a specific user in a guild since a UTC datetime."""

    since_utc = (since_utc or _now_utc()).astimezone(timezone.utc)
    now = _now_utc()

    days: list[str] = []
    cur = since_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end:
        days.append(_day_key(cur))
        cur += timedelta(days=1)

    r = await get_redis_or_none()
    if r is None:
        return 0
    vals = await r.mget([_user_count_key(guild_id, user_id, d) for d in days])
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


# Backwards-compatible aliases expected by older command modules
async def count_feedback_since(*, guild_id: int, user_id: int, since_utc: datetime) -> int:
    """Alias used by commands/slash/feedback.py (per-user count)."""

    return await count_feedback_user_since(guild_id=guild_id, user_id=user_id, since_utc=since_utc)


async def list_recent_feedback(*, guild_id: int, limit: int = 20) -> list[FeedbackItem]:
    limit = max(1, min(int(limit or 20), 50))
    r = await get_redis_or_none()
    if r is None:
        return []
    raw = await r.lrange(_guild_events_key(guild_id), 0, limit - 1)
    out: list[FeedbackItem] = []
    for item in raw or []:
        d = _unj(item, default={}) or {}
        ts = float(d.get("created_at") or 0)
        out.append(
            FeedbackItem(
                created_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else _now_utc(),
                guild_id=int(d.get("guild_id") or guild_id),
                channel_id=int(d.get("channel_id") or 0),
                user_id=int(d.get("user_id") or 0),
                message=str(d.get("message") or ""),
                attachments=list(d.get("attachments") or []),
            )
        )
    return out
