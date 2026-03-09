# utils/roll_ready_dm.py
"""Roll-ready DMs: notify users when their next character roll is available.

Users can opt out via /points roll_reminders off (same style as streak reminders).
"""
from __future__ import annotations

import logging
import time

import discord

from utils.backpressure import get_redis_or_none
from core.kai_mascot import embed_kaihappy

logger = logging.getLogger("bot.roll_ready_dm")

PENDING_KEY = "roll_ready_dm:pending"
OPT_OUT_KEY_PREFIX = "roll_ready_dm:opt_out:"


async def get_roll_ready_dm_enabled(user_id: int) -> bool:
    """True if we should send roll-ready DMs; False if user opted out."""
    r = await get_redis_or_none()
    if r is None:
        return True
    key = f"{OPT_OUT_KEY_PREFIX}{int(user_id)}"
    try:
        val = await r.get(key)
        if val is None:
            return True
        s = val.decode("utf-8", errors="ignore") if isinstance(val, (bytes, bytearray)) else str(val)
        return s.strip().lower() not in ("1", "true", "yes", "on")
    except Exception:
        logger.exception("get_roll_ready_dm_enabled failed")
        return True


async def set_roll_ready_dm_enabled(user_id: int, enabled: bool) -> None:
    """Set whether to send roll-ready DMs. enabled=True means send; False = opt out."""
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{OPT_OUT_KEY_PREFIX}{int(user_id)}"
    try:
        if enabled:
            await r.delete(key)
        else:
            await r.set(key, "1")
    except Exception:
        logger.exception("set_roll_ready_dm_enabled failed")


async def schedule_roll_ready_dm(user_id: int, retry_after_seconds: int) -> None:
    """Schedule a DM when the user's next roll is ready (in retry_after_seconds)."""
    if retry_after_seconds <= 0:
        return
    r = await get_redis_or_none()
    if r is None:
        return
    when = int(time.time()) + retry_after_seconds
    try:
        await r.zadd(PENDING_KEY, {str(user_id): when})
    except Exception:
        logger.exception("schedule_roll_ready_dm failed")


async def get_pending_roll_ready_user_ids() -> list[int]:
    """Return user IDs whose scheduled roll-ready time has passed."""
    r = await get_redis_or_none()
    if r is None:
        return []
    now = int(time.time())
    try:
        raw = await r.zrangebyscore(PENDING_KEY, 0, now)
        return [int(m) for m in raw if m]
    except Exception:
        logger.exception("get_pending_roll_ready_user_ids failed")
        return []


async def clear_scheduled_roll_ready_dm(user_id: int) -> None:
    """Remove user from the pending roll-ready set (after DM sent or cancelled)."""
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.zrem(PENDING_KEY, str(user_id))
    except Exception:
        logger.exception("clear_scheduled_roll_ready_dm failed")


async def send_roll_ready_dm(bot: discord.Client, user_id: int) -> bool:
    """Send the 'your next roll is ready' DM. Returns True if sent. Clears schedule."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for roll-ready DM failed for %s", user_id)
        await clear_scheduled_roll_ready_dm(user_id)
        return False
    if not user:
        await clear_scheduled_roll_ready_dm(user_id)
        return False
    text = (
        "Your next character roll is ready! 🎲 "
        "Use **/character roll** in a server to roll for a new character."
    )
    embed = embed_kaihappy(text, title="Roll ready!")
    try:
        await user.send(embed=embed)
        await clear_scheduled_roll_ready_dm(user_id)
        return True
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (roll-ready)", user_id)
        await clear_scheduled_roll_ready_dm(user_id)
        return False
    except Exception:
        logger.exception("Roll-ready DM failed for user %s", user_id)
        await clear_scheduled_roll_ready_dm(user_id)
        return False
