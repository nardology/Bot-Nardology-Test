"""Character streak tracking system.

Tracks daily streaks for talking to specific characters.
A user maintains a streak with a character by talking to them at least once per day.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta

from utils.backpressure import get_redis_or_none
from utils.analytics import utc_day_str

log = logging.getLogger("bot.character_streak")

# Redis keys
STREAK_KEY_PREFIX = "char_streak"
LAST_TALK_KEY_PREFIX = "char_streak:last_talk"


def _now() -> int:
    return int(time.time())


def _utc_day() -> str:
    return utc_day_str()


def _streak_key(user_id: int, style_id: str) -> str:
    """Redis key for character streak."""
    return f"{STREAK_KEY_PREFIX}:{int(user_id)}:{str(style_id).lower()}"


def _last_talk_key(user_id: int, style_id: str) -> str:
    """Redis key for last talk date."""
    return f"{LAST_TALK_KEY_PREFIX}:{int(user_id)}:{str(style_id).lower()}"


async def record_character_talk(
    *, user_id: int, style_id: str, guild_id: int | None = None
) -> tuple[int, bool]:
    """Record a talk with a character and update streak.
    
    Args:
        user_id: User ID
        style_id: Character style ID
        guild_id: If provided (and not 0), also update server leaderboard for character streak
    
    Returns:
        (new_streak, streak_continued) tuple
        streak_continued: True if streak was maintained, False if broken or new
    """
    r = await get_redis_or_none()
    if r is None:
        return (0, False)

    try:
        today = _utc_day()
        streak_key = _streak_key(user_id, style_id)
        last_talk_key = _last_talk_key(user_id, style_id)
        
        # Get last talk date
        last_talk_raw = await r.get(last_talk_key)
        last_talk = last_talk_raw.decode("utf-8", errors="ignore") if isinstance(last_talk_raw, (bytes, bytearray)) else str(last_talk_raw or "")
        
        # Get current streak
        streak_raw = await r.get(streak_key)
        current_streak = int(streak_raw) if streak_raw else 0
        
        streak_continued = False
        new_streak = 1
        
        if last_talk == today:
            # Already talked today, no change
            new_streak = current_streak
            streak_continued = True
        elif last_talk:
            # Check if yesterday (streak continues)
            try:
                last_date = datetime.strptime(last_talk, "%Y%m%d").date()
                today_date = datetime.strptime(today, "%Y%m%d").date()
                days_diff = (today_date - last_date).days
                
                if days_diff == 1:
                    # Yesterday - streak continues!
                    new_streak = current_streak + 1
                    streak_continued = True
                else:
                    # Streak broken
                    new_streak = 1
                    streak_continued = False
            except Exception:
                # Invalid date, start fresh
                new_streak = 1
                streak_continued = False
        else:
            # First time talking to this character
            new_streak = 1
            streak_continued = False
        
        # Update streak and last talk date
        await r.set(streak_key, str(new_streak), ex=86400 * 90)  # 90 day TTL
        await r.set(last_talk_key, today, ex=86400 * 90)
        
        # Update leaderboard (global + server when guild_id provided)
        from utils.leaderboard import update_all_periods, CATEGORY_CHARACTER_STREAK, GLOBAL_GUILD_ID
        await update_all_periods(
            category=CATEGORY_CHARACTER_STREAK,
            guild_id=GLOBAL_GUILD_ID,
            user_id=user_id,
            value=new_streak,
        )
        if guild_id is not None and int(guild_id) != GLOBAL_GUILD_ID:
            await update_all_periods(
                category=CATEGORY_CHARACTER_STREAK,
                guild_id=int(guild_id),
                user_id=user_id,
                value=new_streak,
            )
        
        return (new_streak, streak_continued)
    except Exception:
        log.exception("Failed to record character talk streak")
        return (0, False)


async def get_character_streak(*, user_id: int, style_id: str) -> int:
    """Get current streak for a character."""
    r = await get_redis_or_none()
    if r is None:
        return 0

    try:
        streak_key = _streak_key(user_id, style_id)
        streak_raw = await r.get(streak_key)
        return int(streak_raw) if streak_raw else 0
    except Exception:
        return 0


async def get_all_character_streaks(*, user_id: int) -> dict[str, int]:
    """Get all character streaks for a user.
    
    Returns:
        Dict mapping style_id -> streak
    """
    r = await get_redis_or_none()
    if r is None:
        return {}

    try:
        pattern = f"{STREAK_KEY_PREFIX}:{int(user_id)}:*"
        cursor = 0
        streaks: dict[str, int] = {}
        
        for _ in range(100):  # Limit scans
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            for key_raw in keys or []:
                key = key_raw.decode("utf-8", errors="ignore") if isinstance(key_raw, (bytes, bytearray)) else str(key_raw)
                # Extract style_id from key: char_streak:user_id:style_id
                parts = key.split(":", 2)
                if len(parts) >= 3:
                    style_id = parts[2]
                    streak_raw = await r.get(key)
                    streak = int(streak_raw) if streak_raw else 0
                    streaks[style_id] = streak
            if cursor == 0:
                break
        
        return streaks
    except Exception:
        log.exception("Failed to get all character streaks")
        return {}


async def get_max_character_streak(*, user_id: int) -> int:
    """Get the maximum streak across all characters for a user."""
    streaks = await get_all_character_streaks(user_id=user_id)
    return max(streaks.values()) if streaks else 0


async def is_character_streak_alive(*, user_id: int, style_id: str) -> bool:
    """True if the user's character streak is still alive (last talk was today or yesterday UTC)."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        last_talk_raw = await r.get(_last_talk_key(user_id, style_id))
        if not last_talk_raw:
            return False
        last_talk = last_talk_raw.decode("utf-8", errors="ignore") if isinstance(last_talk_raw, (bytes, bytearray)) else str(last_talk_raw or "")
        if not last_talk:
            return False
        today = _utc_day()
        if last_talk == today:
            return True
        try:
            last_date = datetime.strptime(last_talk, "%Y%m%d").date()
            today_date = datetime.strptime(today, "%Y%m%d").date()
            return (today_date - last_date).days == 1
        except Exception:
            return False
    except Exception:
        return False


async def delete_character_streak(*, user_id: int, style_id: str) -> int:
    """Delete a character streak entirely (e.g. when removing from inventory).

    Returns the streak value that was deleted (0 if none existed).
    """
    r = await get_redis_or_none()
    if r is None:
        return 0
    try:
        streak_key = _streak_key(user_id, style_id)
        last_talk_key = _last_talk_key(user_id, style_id)

        # Read the old value before deleting
        streak_raw = await r.get(streak_key)
        old_streak = int(streak_raw) if streak_raw else 0

        await r.delete(streak_key)
        await r.delete(last_talk_key)

        if old_streak > 0:
            log.info(
                "Deleted character streak: user=%s style=%s (was %d days)",
                user_id, style_id, old_streak,
            )
        return old_streak
    except Exception:
        log.exception("Failed to delete character streak for user=%s style=%s", user_id, style_id)
        return 0


async def get_active_character_streaks_with_status(*, user_id: int) -> dict[str, tuple[int, str, bool]]:
    """Get all character streaks for a user with alive status.

    Returns:
        Dict mapping style_id -> (streak, last_talk_day, alive)
        alive is True if last_talk was today or yesterday.
    """
    r = await get_redis_or_none()
    if r is None:
        return {}
    try:
        today = _utc_day()
        today_date = datetime.strptime(today, "%Y%m%d").date()

        # Scan streak keys
        pattern = f"{STREAK_KEY_PREFIX}:{int(user_id)}:*"
        cursor = 0
        result: dict[str, tuple[int, str, bool]] = {}

        for _ in range(100):
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            for key_raw in keys or []:
                key = key_raw.decode("utf-8", errors="ignore") if isinstance(key_raw, (bytes, bytearray)) else str(key_raw)
                parts = key.split(":", 2)
                if len(parts) < 3:
                    continue
                style_id = parts[2]
                streak_raw = await r.get(key)
                streak = int(streak_raw) if streak_raw else 0
                if streak <= 0:
                    continue
                # Get last talk date
                lt_raw = await r.get(_last_talk_key(user_id, style_id))
                last_talk = ""
                if lt_raw:
                    last_talk = lt_raw.decode("utf-8", errors="ignore") if isinstance(lt_raw, (bytes, bytearray)) else str(lt_raw or "")
                alive = False
                if last_talk:
                    if last_talk == today:
                        alive = True
                    else:
                        try:
                            last_date = datetime.strptime(last_talk, "%Y%m%d").date()
                            alive = (today_date - last_date).days == 1
                        except Exception:
                            pass
                result[style_id] = (streak, last_talk, alive)
            if cursor == 0:
                break
        return result
    except Exception:
        log.exception("Failed to get active character streaks with status")
        return {}
