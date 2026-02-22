# utils/AI_penalties.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict

from utils.storage import get_guild_setting, set_guild_setting

PENALTY_KEY = "ai_penalties"

STRIKE_WINDOW_SECONDS = 600        # 10 minutes
STRIKES_BEFORE_PENALTY = 3
BASE_PENALTY_SECONDS = 600         # 10 minutes
MAX_PENALTY_SECONDS = 6 * 60 * 60  # 6 hours
STALE_ENTRY_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


@dataclass
class PenaltyState:
    is_penalized: bool
    penalty_until: int
    strikes: int
    strike_window_start: int
    penalty_seconds: int


def _now() -> int:
    return int(time.time())


async def _get_penalties(guild_id: int) -> Dict[str, Any]:
    penalties = await get_guild_setting(guild_id, PENALTY_KEY, default={})
    return penalties if isinstance(penalties, dict) else {}


async def _save_penalties(guild_id: int, penalties: Dict[str, Any]) -> None:
    await set_guild_setting(guild_id, PENALTY_KEY, penalties)


def _cleanup_stale(penalties: Dict[str, Any], now: int) -> bool:
    """Remove entries not seen in a long time. Returns True if anything was removed."""
    to_del = []
    for user_id, entry in penalties.items():
        if not isinstance(entry, dict):
            to_del.append(user_id)
            continue
        try:
            last = int(entry.get("last_seen", 0) or 0)
        except Exception:
            last = 0
        if last and (now - last) > STALE_ENTRY_TTL_SECONDS:
            to_del.append(user_id)

    for uid in to_del:
        penalties.pop(uid, None)

    return bool(to_del)


async def is_user_penalized(guild_id: int, user_id: int) -> tuple[bool, int]:
    now = _now()
    penalties = await _get_penalties(guild_id)

    # cleanup stale sometimes; persist if we removed anything
    if _cleanup_stale(penalties, now):
        await _save_penalties(guild_id, penalties)

    entry = penalties.get(str(user_id))
    if not isinstance(entry, dict):
        return (False, 0)

    until = int(entry.get("penalty_until", 0) or 0)
    if now >= until:
        return (False, 0)

    return (True, max(0, until - now))


async def record_cooldown_strike(guild_id: int, user_id: int) -> PenaltyState:
    now = _now()
    penalties = await _get_penalties(guild_id)

    if _cleanup_stale(penalties, now):
        # not strictly required, but keeps DB clean
        await _save_penalties(guild_id, penalties)

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

    # Reset strike window if expired
    try:
        window_start = int(entry.get("strike_window_start", now) or now)
    except Exception:
        window_start = now

    if now - window_start > STRIKE_WINDOW_SECONDS:
        entry["strikes"] = 0
        entry["strike_window_start"] = now

    # Add a strike
    entry["strikes"] = int(entry.get("strikes", 0) or 0) + 1
    entry["last_seen"] = now

    # Apply penalty if too many strikes
    penalty_until = int(entry.get("penalty_until", 0) or 0)
    penalty_seconds = int(entry.get("penalty_seconds", BASE_PENALTY_SECONDS) or BASE_PENALTY_SECONDS)

    if int(entry["strikes"]) >= STRIKES_BEFORE_PENALTY:
        penalty_until = now + penalty_seconds
        entry["penalty_until"] = penalty_until

        # exponential backoff with cap
        penalty_seconds = min(penalty_seconds * 2, MAX_PENALTY_SECONDS)
        entry["penalty_seconds"] = penalty_seconds

        # reset strikes after penalty applied
        entry["strikes"] = 0
        entry["strike_window_start"] = now

    penalties[key] = entry
    await _save_penalties(guild_id, penalties)

    # Return state for callers
    is_penalized = now < int(entry.get("penalty_until", 0) or 0)

    return PenaltyState(
        is_penalized=is_penalized,
        penalty_until=int(entry.get("penalty_until", 0) or 0),
        strikes=int(entry.get("strikes", 0) or 0),
        strike_window_start=int(entry.get("strike_window_start", now) or now),
        penalty_seconds=int(entry.get("penalty_seconds", BASE_PENALTY_SECONDS) or BASE_PENALTY_SECONDS),
    )

