# utils/talk_memory.py
from __future__ import annotations

import time
from typing import Any, Dict, List

from utils.storage import get_guild_setting, set_guild_setting

TALK_MEMORY_KEY = "talk_memory_v1"

# Low-budget defaults
DEFAULT_TTL_SECONDS = 7 * 24 * 3600      # 7 days
DEFAULT_MAX_ITEMS = 4                     # 2 exchanges: user, assistant, user, assistant
MAX_ITEM_CHARS = 280                      # keep prompt small

# Internal marker for old schema
_LEGACY_STYLE_ID = "__legacy__"


def _now() -> int:
    return int(time.time())


def _trim_text(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    s = " ".join(s.split())  # collapse whitespace
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _ensure_shape(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {}


def _normalize_style_id(style_id: str | None) -> str:
    s = (style_id or "").strip().lower()
    return s or _LEGACY_STYLE_ID


def _is_entry_dict(obj: Any) -> bool:
    # entry shape: {"updated_at": int, "items": list}
    return isinstance(obj, dict) and isinstance(obj.get("items"), list)


def _get_user_style_map(data: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    """
    Returns the per-style map for this user.
    Backward compatible:
      old: data[user_id] = {"updated_at":..., "items":[...]}
      new: data[user_id] = {"fun": {...}, "dragon": {...}}
    We migrate old -> new under __legacy__.
    """
    uid = str(user_id)
    raw_user = data.get(uid)

    # New schema (dict of style_id -> entry)
    if isinstance(raw_user, dict) and not _is_entry_dict(raw_user):
        return raw_user

    # Old schema (single entry dict): migrate to style map under __legacy__
    if _is_entry_dict(raw_user):
        style_map: Dict[str, Any] = {_LEGACY_STYLE_ID: raw_user}
        data[uid] = style_map
        return style_map

    # Nothing yet
    style_map = {}
    data[uid] = style_map
    return style_map


async def load_memory_lines(
    *,
    guild_id: int,
    user_id: int,
    style_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> List[str]:
    """
    Returns formatted memory lines like:
      - User: ...
      - Assistant: ...
    Auto-clears if stale.

    Memory is per (guild_id, user_id, style_id). Backward compatible with legacy per-user memory.
    """
    raw = await get_guild_setting(guild_id, TALK_MEMORY_KEY, default={})
    data = _ensure_shape(raw)

    style_key = _normalize_style_id(style_id)
    style_map = _get_user_style_map(data, user_id)

    entry = style_map.get(style_key)
    if not _is_entry_dict(entry):
        # If no style-specific memory exists, fall back to legacy memory if present
        if style_key != _LEGACY_STYLE_ID and _is_entry_dict(style_map.get(_LEGACY_STYLE_ID)):
            entry = style_map.get(_LEGACY_STYLE_ID)
        else:
            return []

    updated_at = int(entry.get("updated_at", 0) or 0)
    if updated_at and (_now() - updated_at) > int(ttl_seconds):
        # stale -> wipe this style only
        # (and if we fell back to legacy, wipe legacy)
        for k in (style_key, _LEGACY_STYLE_ID):
            if k in style_map and _is_entry_dict(style_map.get(k)):
                e = style_map.get(k)
                ua = int((e or {}).get("updated_at", 0) or 0)
                if ua and (_now() - ua) > int(ttl_seconds):
                    style_map.pop(k, None)

        # clean user bucket if empty
        if not style_map:
            data.pop(str(user_id), None)

        await set_guild_setting(guild_id, TALK_MEMORY_KEY, data)
        return []

    items = entry.get("items")
    if not isinstance(items, list):
        return []

    lines: List[str] = []
    max_items = max(1, int(max_items or DEFAULT_MAX_ITEMS))
    for it in items[-max_items:]:
        if not isinstance(it, dict):
            continue
        role = str(it.get("role", "") or "")
        content = str(it.get("content", "") or "")
        if not content:
            continue

        if role == "user":
            lines.append(f"- User: {content}")
        elif role == "assistant":
            lines.append(f"- Assistant: {content}")

    return lines


async def append_memory_exchange(
    *,
    guild_id: int,
    user_id: int,
    style_id: str | None = None,
    user_text: str,
    assistant_text: str,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> None:
    """
    Append an exchange to per-style memory.
    If the only existing memory is legacy and this is a real style_id, we "migrate" by writing to that style.
    """
    raw = await get_guild_setting(guild_id, TALK_MEMORY_KEY, default={})
    data = _ensure_shape(raw)

    style_key = _normalize_style_id(style_id)
    style_map = _get_user_style_map(data, user_id)

    entry = style_map.get(style_key)
    if not _is_entry_dict(entry):
        entry = {"updated_at": 0, "items": []}

    items = entry.get("items")
    if not isinstance(items, list):
        items = []

    items.append({"role": "user", "content": _trim_text(user_text, MAX_ITEM_CHARS), "t": _now()})
    items.append({"role": "assistant", "content": _trim_text(assistant_text, MAX_ITEM_CHARS), "t": _now()})

    # Trim to most recent items (each exchange is 2 items)
    max_items = max(1, int(max_items or DEFAULT_MAX_ITEMS))
    entry["items"] = items[-max_items:]
    entry["updated_at"] = _now()

    style_map[style_key] = entry
    data[str(user_id)] = style_map

    await set_guild_setting(guild_id, TALK_MEMORY_KEY, data)


async def clear_memory(
    *,
    guild_id: int,
    user_id: int,
    style_id: str | None = None,
) -> bool:
    """
    If style_id is None: clear ALL styles for this user in this guild.
    If style_id is provided: clear ONLY that style (and legacy, if style_id was blank).
    Returns True if something was removed.
    """
    raw = await get_guild_setting(guild_id, TALK_MEMORY_KEY, default={})
    data = _ensure_shape(raw)

    uid = str(user_id)
    raw_user = data.get(uid)
    if raw_user is None:
        return False

    # Clear all
    if style_id is None:
        existed = uid in data
        data.pop(uid, None)
        await set_guild_setting(guild_id, TALK_MEMORY_KEY, data)
        return existed

    # Clear one style
    style_key = _normalize_style_id(style_id)

    # If old schema, treat it as legacy and clear it
    if _is_entry_dict(raw_user):
        existed = True
        data.pop(uid, None)
        await set_guild_setting(guild_id, TALK_MEMORY_KEY, data)
        return existed

    # New schema (style map)
    if not isinstance(raw_user, dict):
        return False

    existed = False
    if style_key in raw_user:
        raw_user.pop(style_key, None)
        existed = True

    # If they asked to clear legacy explicitly, also clear legacy
    if style_key == _LEGACY_STYLE_ID and _LEGACY_STYLE_ID in raw_user:
        raw_user.pop(_LEGACY_STYLE_ID, None)
        existed = True

    # Clean up if empty
    if not raw_user:
        data.pop(uid, None)
    else:
        data[uid] = raw_user

    await set_guild_setting(guild_id, TALK_MEMORY_KEY, data)
    return existed
