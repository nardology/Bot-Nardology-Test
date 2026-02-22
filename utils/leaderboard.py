"""Leaderboard system using Redis sorted sets.

Supports both server (guild) and global leaderboards.
Categories: points, rolls, talk, bond, characters, streak, activity, character_streak
Periods: alltime, daily, weekly, monthly
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

from utils.backpressure import get_redis_or_none
from utils.analytics import utc_day_str

log = logging.getLogger("bot.leaderboard")

# Global guild ID for global leaderboards
GLOBAL_GUILD_ID = 0

# Leaderboard categories
CATEGORY_POINTS = "points"
CATEGORY_ROLLS = "rolls"
CATEGORY_TALK = "talk"
CATEGORY_BOND = "bond"
CATEGORY_CHARACTERS = "characters"
CATEGORY_STREAK = "streak"
CATEGORY_ACTIVITY = "activity"
CATEGORY_CHARACTER_STREAK = "character_streak"

# Periods
PERIOD_ALLTIME = "alltime"
PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"

# Privacy: users who opt out
OPT_OUT_KEY = "leaderboard:opt_out"


def _now() -> int:
    return int(time.time())


def _utc_day() -> str:
    return utc_day_str()


def _utc_week() -> str:
    """Returns YYYYMMDD for Monday of current week."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y%m%d")


def _utc_month() -> str:
    """Returns YYYYMM for current month."""
    return datetime.now(timezone.utc).strftime("%Y%m")


def _leaderboard_key(category: str, guild_id: int, period: str) -> str:
    """Generate Redis key for leaderboard."""
    if guild_id == GLOBAL_GUILD_ID:
        return f"leaderboard:global:{category}:{period}"
    return f"leaderboard:guild:{guild_id}:{category}:{period}"


def _member_key(guild_id: int, user_id: int) -> str:
    """Generate member key for sorted set."""
    if guild_id == GLOBAL_GUILD_ID:
        return str(user_id)
    return f"{guild_id}:{user_id}"


def _parse_member(member: str) -> Tuple[int, int]:
    """Parse member key back to (guild_id, user_id)."""
    if ":" in member:
        parts = member.split(":", 1)
        return (int(parts[0]), int(parts[1]))
    # Global leaderboard
    return (GLOBAL_GUILD_ID, int(member))


async def is_opted_out(user_id: int) -> bool:
    """Check if user has opted out of leaderboards."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        return bool(await r.sismember(OPT_OUT_KEY, str(int(user_id))))
    except Exception:
        return False


async def set_opt_out(user_id: int, opt_out: bool) -> bool:
    """Set user's opt-out status."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        if opt_out:
            await r.sadd(OPT_OUT_KEY, str(int(user_id)))
        else:
            await r.srem(OPT_OUT_KEY, str(int(user_id)))
        return True
    except Exception:
        return False


async def update_leaderboard(
    *,
    category: str,
    guild_id: int,
    user_id: int,
    value: float,
    period: str = PERIOD_ALLTIME,
) -> bool:
    """Update leaderboard score for a user.
    
    Args:
        category: Leaderboard category (points, rolls, talk, etc.)
        guild_id: Guild ID (0 for global)
        user_id: User ID
        value: Score to add/set
        period: Time period (alltime, daily, weekly, monthly)
    
    Returns:
        True if successful, False otherwise
    """
    # Skip if user opted out
    if await is_opted_out(user_id):
        return False

    r = await get_redis_or_none()
    if r is None:
        return False

    try:
        key = _leaderboard_key(category, guild_id, period)
        member = _member_key(guild_id, user_id)
        
        # Use ZADD (set) for "current value" categories; ZINCRBY (increment) for cumulative counts
        set_categories = (
            CATEGORY_POINTS,      # current balance
            CATEGORY_BOND,        # total bond XP across characters
            CATEGORY_CHARACTERS,  # current character count
            CATEGORY_STREAK,      # current daily streak
            CATEGORY_CHARACTER_STREAK,  # current character streak
        )
        if category in set_categories:
            await r.zadd(key, {member: value})
        else:
            # Increment: rolls, talk, activity (counts)
            await r.zincrby(key, value, member)
        
        # Set TTL based on period
        if period == PERIOD_DAILY:
            await r.expire(key, 86400 * 2)  # 2 days
        elif period == PERIOD_WEEKLY:
            await r.expire(key, 86400 * 8)  # 8 days
        elif period == PERIOD_MONTHLY:
            await r.expire(key, 86400 * 32)  # 32 days
        # alltime has no expiration
        
        return True
    except Exception:
        log.exception("Failed to update leaderboard %s", category)
        return False


async def update_all_periods(
    *,
    category: str,
    guild_id: int,
    user_id: int,
    value: float,
) -> None:
    """Update all time periods for a category."""
    await update_leaderboard(category=category, guild_id=guild_id, user_id=user_id, value=value, period=PERIOD_ALLTIME)
    
    # Also update current period
    day = _utc_day()
    week = _utc_week()
    month = _utc_month()
    
    # Daily: only if it's a new day (track separately)
    # For now, we'll update all periods
    await update_leaderboard(category=category, guild_id=guild_id, user_id=user_id, value=value, period=PERIOD_DAILY)
    await update_leaderboard(category=category, guild_id=guild_id, user_id=user_id, value=value, period=PERIOD_WEEKLY)
    await update_leaderboard(category=category, guild_id=guild_id, user_id=user_id, value=value, period=PERIOD_MONTHLY)


async def get_leaderboard(
    *,
    category: str,
    guild_id: int,
    period: str = PERIOD_ALLTIME,
    limit: int = 10,
    offset: int = 0,
) -> List[Tuple[int, int, float]]:
    """Get leaderboard rankings.
    
    Args:
        category: Leaderboard category
        guild_id: Guild ID (0 for global)
        period: Time period
        limit: Number of results
        offset: Offset for pagination
    
    Returns:
        List of (guild_id, user_id, score) tuples, sorted descending
    """
    r = await get_redis_or_none()
    if r is None:
        return []

    try:
        key = _leaderboard_key(category, guild_id, period)
        # Get top N with scores (descending)
        results = await r.zrevrange(key, offset, offset + limit - 1, withscores=True)
        
        out: List[Tuple[int, int, float]] = []
        for member_raw, score in results or []:
            if isinstance(member_raw, (bytes, bytearray)):
                member = member_raw.decode("utf-8", errors="ignore")
            else:
                member = str(member_raw)
            
            gid, uid = _parse_member(member)
            
            # Skip opted-out users
            if await is_opted_out(uid):
                continue
            
            out.append((gid, uid, float(score)))
        
        return out
    except Exception:
        log.exception("Failed to get leaderboard %s", category)
        return []


async def get_user_rank(
    *,
    category: str,
    guild_id: int,
    user_id: int,
    period: str = PERIOD_ALLTIME,
) -> Optional[Tuple[int, float]]:
    """Get user's rank and score.
    
    Args:
        category: Leaderboard category
        guild_id: Guild ID (0 for global)
        user_id: User ID
        period: Time period
    
    Returns:
        (rank, score) tuple, or None if not found
        Rank is 0-indexed (0 = first place)
    """
    # Skip if opted out
    if await is_opted_out(user_id):
        return None

    r = await get_redis_or_none()
    if r is None:
        return None

    try:
        key = _leaderboard_key(category, guild_id, period)
        member = _member_key(guild_id, user_id)
        
        # Get rank (0-indexed, descending)
        rank = await r.zrevrank(key, member)
        if rank is None:
            return None
        
        # Get score
        score = await r.zscore(key, member)
        if score is None:
            return None
        
        return (int(rank), float(score))
    except Exception:
        log.exception("Failed to get user rank %s", category)
        return None


async def get_user_score(
    *,
    category: str,
    guild_id: int,
    user_id: int,
    period: str = PERIOD_ALLTIME,
) -> float:
    """Get user's current score."""
    rank_data = await get_user_rank(category=category, guild_id=guild_id, user_id=user_id, period=period)
    if rank_data is None:
        return 0.0
    return rank_data[1]


async def reset_period(period: str) -> None:
    """Reset a time period (for daily/weekly/monthly resets).
    
    This should be called periodically to clear old period data.
    """
    r = await get_redis_or_none()
    if r is None:
        return

    try:
        # Find all keys for this period
        pattern = f"leaderboard:*:*:{period}"
        cursor = 0
        deleted = 0
        
        for _ in range(100):  # Limit scans
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            if keys:
                await r.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        
        log.info("Reset %d leaderboard keys for period %s", deleted, period)
    except Exception:
        log.exception("Failed to reset period %s", period)


async def reset_all_leaderboard_data() -> int:
    """Delete all leaderboard Redis keys (scores + opt-out set). Use for a full reset.
    Returns number of keys deleted.
    """
    r = await get_redis_or_none()
    if r is None:
        return 0
    deleted = 0
    try:
        cursor = 0
        for _ in range(500):
            cursor, keys = await r.scan(cursor, match="leaderboard:*", count=200)
            if keys:
                await r.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        log.info("Reset all leaderboard data: %d keys deleted", deleted)
    except Exception:
        log.exception("Failed to reset all leaderboard data")
    return deleted
