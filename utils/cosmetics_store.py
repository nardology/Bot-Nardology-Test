"""Redis-backed storage for user cosmetics: owned set + selected cosmetic (one at a time)."""
from __future__ import annotations

from utils.redis_kv import sadd, smembers_str, kv_get_json, kv_set_json
from utils.cosmetics import COSMETIC_IDS


def _owned_key(user_id: int) -> str:
    return f"user:{int(user_id)}:cosmetics:owned"


def _selected_key(user_id: int) -> str:
    return f"user:{int(user_id)}:cosmetic:selected"


async def get_owned(user_id: int) -> set[str]:
    """Return set of cosmetic_ids the user has purchased."""
    uid = int(user_id)
    raw = await smembers_str(_owned_key(uid))
    return {c for c in raw if c and c in COSMETIC_IDS}


async def add_owned(user_id: int, cosmetic_id: str) -> bool:
    """Add a cosmetic to the user's owned set. Returns True if added."""
    cosmetic_id = (cosmetic_id or "").strip().lower()
    if cosmetic_id not in COSMETIC_IDS:
        return False
    uid = int(user_id)
    n = await sadd(_owned_key(uid), cosmetic_id)
    return n > 0


async def get_selected(user_id: int) -> str | None:
    """Return the user's currently selected cosmetic_id, or None."""
    uid = int(user_id)
    val = await kv_get_json(_selected_key(uid), default=None)
    if val is None or not isinstance(val, str):
        return None
    cid = val.strip().lower()
    return cid if cid in COSMETIC_IDS else None


async def set_selected(user_id: int, cosmetic_id: str | None) -> bool:
    """Set the user's displayed cosmetic. Pass None to clear. Returns True on success."""
    uid = int(user_id)
    if cosmetic_id is None or cosmetic_id == "":
        await kv_set_json(_selected_key(uid), None)
        return True
    cosmetic_id = cosmetic_id.strip().lower()
    if cosmetic_id not in COSMETIC_IDS:
        return False
    await kv_set_json(_selected_key(uid), cosmetic_id)
    return True
