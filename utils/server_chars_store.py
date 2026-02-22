from __future__ import annotations

"""Per-guild "server-only" characters (not in any pack).

Stored in Redis guild settings as JSON.
Key: server_only_characters -> list[dict]

Each character dict uses an internal style_id namespaced by guild:
  internal_id = f"server{guild_id}_{public_id}"

We keep `public_id` so UI can show a clean id.
"""

from typing import Any

from utils.storage import get_guild_setting, set_guild_setting
from utils.packs_store import normalize_style_id


KEY = "server_only_characters"


def make_internal_id(guild_id: int, public_id: str) -> str:
    pid = normalize_style_id(public_id)
    return f"server{int(guild_id)}_{pid}" if pid else ""


async def list_server_chars(guild_id: int) -> list[dict[str, Any]]:
    raw = await get_guild_setting(int(guild_id), KEY, default=[])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for c in raw:
        if isinstance(c, dict):
            out.append(dict(c))
    return out


async def upsert_server_char(guild_id: int, char: dict[str, Any]) -> bool:
    chars = await list_server_chars(guild_id)
    sid = str(char.get("style_id") or char.get("id") or "").strip()
    if not sid:
        return False
    replaced = False
    for i, existing in enumerate(chars):
        if not isinstance(existing, dict):
            continue
        if str(existing.get("style_id") or existing.get("id") or "").strip() == sid:
            chars[i] = dict(char)
            replaced = True
            break
    if not replaced:
        chars.append(dict(char))
    try:
        await set_guild_setting(int(guild_id), KEY, chars)
        return True
    except Exception:
        return False


async def remove_server_char(guild_id: int, style_id: str) -> bool:
    sid = str(style_id or "").strip()
    if not sid:
        return False
    chars = await list_server_chars(guild_id)
    new = [c for c in chars if str(c.get("style_id") or c.get("id") or "").strip() != sid]
    await set_guild_setting(int(guild_id), KEY, new)
    return len(new) != len(chars)


def to_pack_payload(guild_id: int, chars: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert server-only characters into a pseudo pack payload for the registry."""
    gid = int(guild_id)
    return {
        "type": "pack",
        "pack_id": f"server_{gid}",
        "name": f"This Server ({gid})",
        "description": "Characters that are only available in this server (no pack).",
        "characters": list(chars or []),
    }
