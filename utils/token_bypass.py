# utils/token_bypass.py
"""Owner-managed list of user IDs that bypass AI output token limits (for testing/auditing)."""

from __future__ import annotations

import config
from utils.redis_kv import sadd, srem, smembers_str

REDIS_KEY = "token_limit_bypass_user_ids"


async def get_token_bypass_user_ids() -> set[int]:
    """Return set of user IDs that have token limit bypass (Redis only; BOT_OWNER_IDS checked at call site)."""
    raw = await smembers_str(REDIS_KEY)
    out: set[int] = set()
    for s in raw:
        try:
            out.add(int(s))
        except ValueError:
            continue
    return out


async def has_token_bypass(user_id: int) -> bool:
    """True if user is in BOT_OWNER_IDS or in the Redis bypass set."""
    if config.BOT_OWNER_IDS and user_id in config.BOT_OWNER_IDS:
        return True
    ids = await get_token_bypass_user_ids()
    return user_id in ids


async def add_token_bypass(user_id: int) -> None:
    """Add a user ID to the bypass set (owner-only via command)."""
    await sadd(REDIS_KEY, str(user_id))


async def remove_token_bypass(user_id: int) -> None:
    """Remove a user ID from the bypass set."""
    await srem(REDIS_KEY, str(user_id))
