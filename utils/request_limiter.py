# utils/request_limiter.py
"""In-flight request limiter: one slash command (or button) per user at a time.

Reduces double-clicks and "I pressed 3 times" while the bot is slow to respond.
When a user already has a command in progress, we block the new one with a friendly message.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("bot.request_limiter")

_PREFIX = "request_limiter:in_flight:"
_TTL_S = 90  # max time a command can be "in flight" before key expires (safety)


async def _redis():
    from utils.backpressure import get_redis_or_none
    return await get_redis_or_none()


async def is_in_flight(user_id: int) -> bool:
    """True if this user already has a command/request in progress."""
    r = await _redis()
    if r is None:
        return False
    try:
        key = f"{_PREFIX}{int(user_id)}"
        return bool(await r.get(key))
    except Exception:
        logger.debug("is_in_flight check failed for %s", user_id, exc_info=True)
        return False


async def set_in_flight(user_id: int) -> None:
    """Mark this user as having a request in progress. TTL ensures we don't block forever if clear is missed."""
    r = await _redis()
    if r is None:
        return
    try:
        key = f"{_PREFIX}{int(user_id)}"
        await r.set(key, "1", ex=_TTL_S)
    except Exception:
        logger.debug("set_in_flight failed for %s", user_id, exc_info=True)


async def clear_in_flight(user_id: int) -> None:
    """Clear the in-flight flag when the command finishes (success or error)."""
    r = await _redis()
    if r is None:
        return
    try:
        key = f"{_PREFIX}{int(user_id)}"
        await r.delete(key)
    except Exception:
        logger.debug("clear_in_flight failed for %s", user_id, exc_info=True)
