# utils/say_penalties.py
from __future__ import annotations

import time
from typing import Any, Dict

from utils.storage import get_guild_setting, set_guild_setting

PENALTY_KEY = "say_penalties"

STRIKE_WINDOW_SECONDS = 600        # 10 minutes
STRIKES_BEFORE_PENALTY = 3
BASE_PENALTY_SECONDS = 600         # 10 minutes
MAX_PENALTY_SECONDS = 6 * 60 * 60  # 6 hours
STALE_ENTRY_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _now() -> int:
    return int(time.time())


async def _get_penalties(guild_id: int) -> Dict[str, Any]:
    penalties = await get_guild_setting(guild_id, PENALTY_KEY, default={})
    return penalties if isinstance(penalties, dict) else {}


async def _save_penalties(guild_id: int, penalties: Dict[str, Any]) -> None:
    await set_guild_setting(guild_id, PENALTY_KEY, penalties)


def _cleanup_stale(penalties: Dict[str, Any], now: int) -> None:
    to_del = []
    for user_id, entry in penalties.items():
        try:
            last = int(entry.get("last_seen", 0))
        except Exception:
            last = 0
        if last and (now - last) > STALE_ENTRY_TTL_SECONDS:
            to_del.append(user_id)
    for uid in to_del:
        penalties.pop(uid, None)


async def is_user_penalized(guild_id: int, user_id: int) -> tuple[bool, int]:
    now = _now()
    penalties = await _get_penalties(guild_id)
    _cleanup_stale(penalties, now)

    entry = penalties.get(str(user_id))
    if not isinstance(entry, dict):
        return (False, 0)

    until = int(entry.get("penalty_until", 0) or 0)
    if now >= until:
        return (False, 0)

    return (True, max(0, until - now))



async def record_cooldown_strike(guild_id: int, user_id: int) -> Dict[str, Any]:
    now = _now()
    penalties = await _get_penalties(guild_id)
    _cleanup_stale(penalties, now)

    key = str(user_id)
    entry = penalties.get(key)
    if not isinstance(entry, dict):
        entry = {
            "strikes": 0,
            "strike_window_start": now,
            "penalty_seconds": BASE_PENALTY_SECONDS,
            "penalty_until": 0,
            "last_seen": now,
        }

    if now - int(entry.get("strike_window_start", now)) > STRIKE_WINDOW_SECONDS:
        entry["strikes"] = 0
        entry["strike_window_start"] = now

    entry["strikes"] = int(entry.get("strikes", 0)) + 1
    entry["last_seen"] = now

    penalty_until = int(entry.get("penalty_until", 0) or 0)
    penalty_seconds = int(entry.get("penalty_seconds", BASE_PENALTY_SECONDS) or BASE_PENALTY_SECONDS)

    if entry["strikes"] >= STRIKES_BEFORE_PENALTY:
        penalty_until = now + penalty_seconds
        entry["penalty_until"] = penalty_until

        penalty_seconds = min(penalty_seconds * 2, MAX_PENALTY_SECONDS)
        entry["penalty_seconds"] = penalty_seconds

        entry["strikes"] = 0
        entry["strike_window_start"] = now

    penalties[key] = entry
    await _save_penalties(guild_id, penalties)
    return entry
