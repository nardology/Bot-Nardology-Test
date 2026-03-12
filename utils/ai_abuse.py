"""utils/ai_abuse.py

Abuse detection and moderation for AI usage.

- Flags users when they exceed a daily cost threshold (config.AI_ABUSE_FLAG_USER_CENTS)
  or daily call count (config.AI_ABUSE_FLAG_USER_CALLS_PER_DAY).
- When config.AI_ABUSE_AUTO_THROTTLE is true, flagged users get free-tier /talk limits.
- Owners can list flagged users, restrict (force free-tier), and clear flags.
- DMs bot owners when a user is first flagged; admin panel can view flag log.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import config
from utils.cost_tracker import get_today_cost_cents_user
from utils.backpressure import get_redis_or_none

log = logging.getLogger("ai_abuse")

_PREFIX_FLAGGED = "ai:abuse:flagged:"
_PREFIX_RESTRICTED = "ai:abuse:restricted:"
_PREFIX_CALLS = "ai:abuse:calls:user:"
_FLAG_LOG_KEY = "ai:abuse:flag_log"
_TTL_DAYS = 90  # keep set membership for 90 days so owners can review
_TTL_CALLS_DAY = 86400 * 2
_FLAG_LOG_MAX = 200


def _key_flagged(user_id: int) -> str:
    return f"{_PREFIX_FLAGGED}{int(user_id)}"


def _key_restricted(user_id: int) -> str:
    return f"{_PREFIX_RESTRICTED}{int(user_id)}"


def _key_calls(user_id: int, day_utc: str) -> str:
    return f"{_PREFIX_CALLS}{int(user_id)}:{str(day_utc)}"


_bot_ref = None


def set_bot_for_flagged_notifications(bot) -> None:
    """Call at startup (e.g. from admin panel register) so DMs can be sent when users are flagged."""
    global _bot_ref
    _bot_ref = bot


async def _notify_owners_flagged(user_id: int, reason: str) -> None:
    """DM all BOT_OWNER_IDS that a user was auto-flagged."""
    bot = _bot_ref
    if not bot or not (config.BOT_OWNER_IDS or []):
        return
    for oid in config.BOT_OWNER_IDS or []:
        try:
            user = bot.get_user(int(oid)) or await bot.fetch_user(int(oid))
            await user.send(
                f"⚠️ **AI abuse flag** — User `{user_id}` was auto-flagged.\n"
                f"Reason: {reason}\n"
                "They are now throttled to free-tier limits. Check the admin panel **Abuse** tab or `/z_owner` for details."
            )
        except Exception as e:
            log.warning("Failed to DM owner %s about flagged user %s: %s", oid, user_id, e)


async def _append_flag_log(user_id: int, reason: str) -> None:
    """Append an entry to the flag log (for admin panel)."""
    try:
        r = await get_redis_or_none()
        if r is None:
            return
        entry = {
            "user_id": int(user_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }
        await r.lpush(_FLAG_LOG_KEY, json.dumps(entry))
        await r.ltrim(_FLAG_LOG_KEY, 0, _FLAG_LOG_MAX - 1)
        await r.expire(_FLAG_LOG_KEY, _TTL_DAYS * 24 * 3600)
    except Exception:
        pass


async def increment_talk_calls_user_today(user_id: int) -> int:
    """Increment and return today's (UTC) talk call count for this user. Used for abuse detection."""
    try:
        from utils.analytics import utc_day_str
        day = utc_day_str(int(datetime.now(timezone.utc).timestamp()))
        r = await get_redis_or_none()
        if r is None:
            return 0
        key = _key_calls(int(user_id), day)
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, _TTL_CALLS_DAY)
        return int(n)
    except Exception:
        return 0


async def get_today_talk_calls_user(user_id: int) -> int:
    """Return today's (UTC) talk call count for this user."""
    try:
        from utils.analytics import utc_day_str
        day = utc_day_str(int(datetime.now(timezone.utc).timestamp()))
        r = await get_redis_or_none()
        if r is None:
            return 0
        key = _key_calls(int(user_id), day)
        val = await r.get(key)
        return int(val or 0)
    except Exception:
        return 0


async def maybe_flag_user_after_usage(user_id: int) -> None:
    """Flag user if daily cost or daily call count exceeds thresholds. DM owners and log when first flagged."""
    try:
        uid = int(user_id)
        r = await get_redis_or_none()
        if r is None:
            return

        already = await r.get(_key_flagged(uid))
        if already:
            return

        reason_parts = []

        cost_threshold = float(getattr(config, "AI_ABUSE_FLAG_USER_CENTS", 6))
        if cost_threshold > 0:
            cents = await get_today_cost_cents_user(uid)
            if cents >= cost_threshold:
                reason_parts.append(f"daily cost ${cents/100:.2f} >= ${cost_threshold/100:.2f}")

        calls_threshold = int(getattr(config, "AI_ABUSE_FLAG_USER_CALLS_PER_DAY", 40))
        if not reason_parts and calls_threshold > 0:
            calls = await get_today_talk_calls_user(uid)
            if calls >= calls_threshold:
                reason_parts.append(f"daily calls {calls} >= {calls_threshold}")

        if not reason_parts:
            return

        reason = "; ".join(reason_parts)
        await r.set(_key_flagged(uid), "1", ex=_TTL_DAYS * 24 * 3600)
        await _append_flag_log(uid, reason)
        asyncio.create_task(_notify_owners_flagged(uid, reason))
    except Exception:
        pass


async def is_abuse_flagged(user_id: int) -> bool:
    """True if the user has been auto-flagged for high daily cost or call volume."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        val = await r.get(_key_flagged(user_id))
        return val is not None
    except Exception:
        return False


async def is_abuse_restricted(user_id: int) -> bool:
    """True if an owner has manually restricted this user (free-tier limits)."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        val = await r.get(_key_restricted(user_id))
        return val is not None
    except Exception:
        return False


async def should_throttle_user(user_id: int) -> bool:
    """True if this user should be throttled to free-tier limits (flagged + auto_throttle, or restricted)."""
    if await is_abuse_restricted(user_id):
        return True
    if not getattr(config, "AI_ABUSE_AUTO_THROTTLE", True):
        return False
    return await is_abuse_flagged(user_id)


async def set_abuse_restricted(user_id: int, restricted: bool = True) -> None:
    """Restrict a user (free-tier limits) or clear restriction. Does not clear flagged."""
    r = await get_redis_or_none()
    if r is None:
        return
    key = _key_restricted(user_id)
    if restricted:
        await r.set(key, "1", ex=_TTL_DAYS * 24 * 3600)
    else:
        await r.delete(key)


async def clear_abuse_flagged(user_id: int) -> None:
    """Remove user from the auto-flagged set (e.g. after review)."""
    r = await get_redis_or_none()
    if r is None:
        return
    await r.delete(_key_flagged(user_id))


async def clear_abuse_all(user_id: int) -> None:
    """Clear both flagged and restricted for this user."""
    r = await get_redis_or_none()
    if r is None:
        return
    await r.delete(_key_flagged(user_id))
    await r.delete(_key_restricted(user_id))


async def get_flagged_user_ids() -> list[int]:
    """Return user IDs currently in the flagged set (for owner list)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        keys = await r.keys(_PREFIX_FLAGGED + "*")
        out = []
        for k in keys or []:
            if isinstance(k, bytes):
                k = k.decode("utf-8", errors="ignore")
            try:
                uid = int(k.replace(_PREFIX_FLAGGED, ""))
                out.append(uid)
            except ValueError:
                pass
        return sorted(set(out))
    except Exception:
        return []


async def get_restricted_user_ids() -> list[int]:
    """Return user IDs currently restricted by an owner."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        keys = await r.keys(_PREFIX_RESTRICTED + "*")
        out = []
        for k in keys or []:
            if isinstance(k, bytes):
                k = k.decode("utf-8", errors="ignore")
            try:
                uid = int(k.replace(_PREFIX_RESTRICTED, ""))
                out.append(uid)
            except ValueError:
                pass
        return sorted(set(out))
    except Exception:
        return []


async def get_flag_log(limit: int = 100) -> list[dict]:
    """Return recent flag log entries for admin panel (newest first)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        raw = await r.lrange(_FLAG_LOG_KEY, 0, limit - 1)
        out = []
        for b in raw or []:
            s = b.decode("utf-8", errors="ignore") if isinstance(b, bytes) else str(b)
            try:
                out.append(json.loads(s))
            except Exception:
                pass
        return out
    except Exception:
        return []
