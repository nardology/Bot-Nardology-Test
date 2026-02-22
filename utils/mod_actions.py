from __future__ import annotations

"""
utils/mod_actions.py

Redis-backed global moderation state.

Goals:
- Central place for global bot disable and user bans.
- Must degrade gracefully if Redis is unavailable (never crash the bot).
"""

import json
import time
from typing import Any, Dict, Optional, Tuple

from utils.backpressure import get_redis_or_none


KEY_BOT_DISABLED = "bot:disabled"
KEY_BOT_DISABLED_META = "bot:disabled:meta"

KEY_BANNED_USERS = "bot:banned_users"              # Redis SET of user ids (strings)
KEY_BANNED_USER_META_PREFIX = "bot:banned_user:"   # bot:banned_user:<uid> -> JSON

KEY_NUKED_GUILDS = "bot:nuked_guilds"              # Redis SET of guild ids (strings)
KEY_NUKE_WARNING_PREFIX = "bot:nuke_warning:"      # bot:nuke_warning:<gid> -> JSON


def _now() -> int:
    return int(time.time())


def _as_int(x: Any) -> int | None:
    try:
        if x is None:
            return None
        return int(str(x).strip())
    except Exception:
        return None


def _json_dumps(d: Dict[str, Any]) -> str:
    return json.dumps(d, separators=(",", ":"))


def _json_loads(s: Any) -> Dict[str, Any]:
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="ignore")
        data = json.loads(str(s))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# -----------------------------
# Bot disable (global)
# -----------------------------


async def disable_bot(*, reason: str = "manual", by_user_id: int = 0) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.set(KEY_BOT_DISABLED, "1")
    except Exception:
        return

    meta = {
        "t": _now(),
        "reason": (reason or "").strip()[:400],
        "by": int(by_user_id or 0),
    }
    try:
        await r.set(KEY_BOT_DISABLED_META, _json_dumps(meta))
    except Exception:
        pass


async def enable_bot() -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.delete(KEY_BOT_DISABLED)
        await r.delete(KEY_BOT_DISABLED_META)
    except Exception:
        pass


async def is_bot_disabled() -> bool:
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        v = await r.get(KEY_BOT_DISABLED)
        return bool(int(v or 0))
    except Exception:
        return False


async def get_bot_disabled_meta() -> Tuple[Optional[int], str, Optional[int]]:
    """Returns (disabled_at_unix, reason, by_user_id) if available."""
    r = await get_redis_or_none()
    if r is None:
        return (None, "", None)
    try:
        raw = await r.get(KEY_BOT_DISABLED_META)
        if not raw:
            return (None, "", None)
        d = _json_loads(raw)
        t = _as_int(d.get("t"))
        by = _as_int(d.get("by"))
        reason = str(d.get("reason") or "")
        return (t, reason, by)
    except Exception:
        return (None, "", None)


# -----------------------------
# User bans (global)
# -----------------------------


async def ban_user(*, user_id: int, reason: str = "", by_user_id: int = 0) -> None:
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.sadd(KEY_BANNED_USERS, str(uid))
    except Exception:
        return
    meta = {
        "t": _now(),
        "reason": (reason or "").strip()[:400],
        "by": int(by_user_id or 0),
    }
    try:
        await r.set(f"{KEY_BANNED_USER_META_PREFIX}{uid}", _json_dumps(meta))
    except Exception:
        pass


async def unban_user(*, user_id: int, by_user_id: int = 0, reason: str = "") -> None:
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.srem(KEY_BANNED_USERS, str(uid))
    except Exception:
        pass
    # Keep a small audit trail (optional; best-effort)
    meta = {
        "t": _now(),
        "reason": (reason or "").strip()[:400],
        "by": int(by_user_id or 0),
        "action": "unban",
    }
    try:
        await r.set(f"{KEY_BANNED_USER_META_PREFIX}{uid}:last_unban", _json_dumps(meta), ex=86400 * 30)
    except Exception:
        pass
    try:
        await r.delete(f"{KEY_BANNED_USER_META_PREFIX}{uid}")
    except Exception:
        pass


async def is_user_banned(user_id: int) -> bool:
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        return bool(await r.sismember(KEY_BANNED_USERS, str(uid)))
    except Exception:
        return False


async def get_user_ban_reason(user_id: int) -> str:
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return ""
    try:
        raw = await r.get(f"{KEY_BANNED_USER_META_PREFIX}{uid}")
        if not raw:
            return ""
        d = _json_loads(raw)
        return str(d.get("reason") or "")
    except Exception:
        return ""


# -----------------------------
# Nuke warnings / nuked guilds
# -----------------------------


async def mark_guild_nuked(*, guild_id: int, reason: str = "", by_user_id: int = 0) -> None:
    gid = int(guild_id)
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.sadd(KEY_NUKED_GUILDS, str(gid))
    except Exception:
        return
    meta = {
        "t": _now(),
        "reason": (reason or "").strip()[:400],
        "by": int(by_user_id or 0),
    }
    try:
        await r.set(f"{KEY_NUKE_WARNING_PREFIX}{gid}:nuked_meta", _json_dumps(meta))
    except Exception:
        pass


async def set_nuke_warning(
    *,
    guild_id: int,
    days_until_nuke: int,
    reason: str,
    notes: str = "",
    by_user_id: int = 0,
    owner_id: int = 0,
) -> int:
    """
    Stores a nuke warning record and returns the deadline epoch seconds.
    No scheduler is provided; this is for manual tracking + messaging.
    """
    gid = int(guild_id)
    days_i = max(0, int(days_until_nuke or 0))
    deadline = _now() + days_i * 86400
    r = await get_redis_or_none()
    if r is None:
        return deadline
    payload = {
        "t": _now(),
        "deadline": int(deadline),
        "days": int(days_i),
        "reason": (reason or "").strip()[:400],
        "notes": (notes or "").strip()[:1000],
        "by": int(by_user_id or 0),
        "owner_id": int(owner_id or 0),
    }
    try:
        await r.set(f"{KEY_NUKE_WARNING_PREFIX}{gid}", _json_dumps(payload))
    except Exception:
        pass
    return deadline


async def get_nuke_warning(guild_id: int) -> Dict[str, Any]:
    gid = int(guild_id)
    r = await get_redis_or_none()
    if r is None:
        return {}
    try:
        raw = await r.get(f"{KEY_NUKE_WARNING_PREFIX}{gid}")
        if not raw:
            return {}
        return _json_loads(raw)
    except Exception:
        return {}

