from __future__ import annotations

"""Global pack + custom character storage.

Design goals:
- Packs are globally visible across servers.
- Only *premium* servers can create/delete packs or add/remove characters.
- Any server owner/admin can enable/disable which packs are rollable in their server.

Storage:
- Redis JSON blobs (simple + durable across deploys).
  - Set: packs:global -> pack_ids
  - String: pack:{pack_id} -> JSON dict
"""

import json
import re
from typing import Any

from utils.backpressure import get_redis_or_none
from utils.storage import list_add, list_remove, list_members


PACK_INDEX_KEY = "packs:global"
GUILD_ENABLED_KEY = "enabled_packs"
FEATURED_PACKS_KEY = "packs:featured"


def _upvotes_key(pack_id: str) -> str:
    return f"pack:upvotes:{normalize_pack_id(pack_id)}"


def normalize_pack_id(pack_id: str) -> str:
    s = (pack_id or "").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_-")
    return s[:48] if s else ""


def normalize_style_id(style_id: str) -> str:
    s = (style_id or "").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_-")
    return s[:64] if s else ""


def _pack_key(pack_id: str) -> str:
    return f"pack:{pack_id}"


async def list_custom_packs(
    *,
    limit: int | None = None,
    include_internal: bool = False,
    include_shop_only: bool = False,
) -> list[dict[str, Any]]:
    """List custom packs. By default excludes internal_shop and shop_only packs (browse/enable)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        ids = await r.smembers(PACK_INDEX_KEY)
        out: list[dict[str, Any]] = []
        for raw in ids or []:
            pid = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
            pid = normalize_pack_id(pid)
            if not pid:
                continue
            p = await get_custom_pack(pid)
            if not p:
                continue
            # Internal/system packs (e.g. shop "packless" singles) should not show up in user-facing
            # commands and autocompletes unless explicitly requested.
            if not include_internal and bool(p.get("internal_shop")):
                continue
            # Shop-only (limited) packs are only for shop pack_roll; hide from /packs browse and enable.
            if not include_shop_only and bool(p.get("shop_only")):
                continue
            out.append(p)
        # stable order
        out.sort(key=lambda d: str(d.get("pack_id") or ""))
        if isinstance(limit, int) and limit > 0:
            return out[: int(limit)]
        return out
    except Exception:
        return []


async def get_custom_pack(pack_id: str) -> dict[str, Any] | None:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return None
    r = await get_redis_or_none()
    if r is None:
        return None
    try:
        raw = await r.get(_pack_key(pid))
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        d = json.loads(str(raw))
        if not isinstance(d, dict):
            return None
        d["pack_id"] = pid
        return d
    except Exception:
        return None


async def upsert_custom_pack(payload: dict[str, Any]) -> bool:
    """Create/update a pack payload (must include pack_id)."""
    pid = normalize_pack_id(str(payload.get("pack_id") or ""))
    if not pid:
        return False
    payload = dict(payload)
    payload["pack_id"] = pid
    payload.setdefault("type", "pack")
    payload.setdefault("characters", [])
    if not isinstance(payload.get("characters"), list):
        payload["characters"] = []

    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        await r.set(_pack_key(pid), json.dumps(payload, separators=(",", ":")))
        await r.sadd(PACK_INDEX_KEY, pid)
        return True
    except Exception:
        return False


async def delete_custom_pack(pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        await r.delete(_pack_key(pid))
        await r.srem(PACK_INDEX_KEY, pid)
        return True
    except Exception:
        return False


async def add_character_to_pack(pack_id: str, char: dict[str, Any]) -> tuple[bool, str]:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False, "Invalid pack id."
    d = await get_custom_pack(pid)
    if not d:
        return False, "Pack not found."

    style_id = normalize_style_id(str(char.get("id") or char.get("style_id") or ""))
    if not style_id:
        return False, "Invalid character id."

    char = dict(char)
    char["id"] = style_id
    char["style_id"] = style_id
    char["pack_id"] = pid
    char.setdefault("type", "character")
    char.setdefault("rollable", True)

    chars = list(d.get("characters") or [])
    # replace if exists
    replaced = False
    for i, existing in enumerate(chars):
        if isinstance(existing, dict) and normalize_style_id(str(existing.get("id") or existing.get("style_id") or "")) == style_id:
            chars[i] = char
            replaced = True
            break
    if not replaced:
        chars.append(char)

    d["characters"] = chars
    ok = await upsert_custom_pack(d)
    return (ok, "ok" if ok else "Failed saving pack.")


async def remove_character_from_pack(pack_id: str, style_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    sid = normalize_style_id(style_id)
    if not pid or not sid:
        return False
    d = await get_custom_pack(pid)
    if not d:
        return False
    chars = [c for c in (d.get("characters") or []) if not (isinstance(c, dict) and normalize_style_id(str(c.get("id") or c.get("style_id") or "")) == sid)]
    d["characters"] = chars
    return await upsert_custom_pack(d)


# -----------------------------
# Per-guild enabled packs
# -----------------------------

async def get_enabled_pack_ids(guild_id: int) -> set[str]:
    """Return enabled pack ids for a guild.

    Behavior:
    - If the guild has never configured enabled packs, we default to
      {"nardologybot", "server_<guild_id>"} for backwards compatibility.
    - If the guild *has* configured enabled packs, we respect that list exactly.

    This matters because the per-guild "server_<guild_id>" pseudo pack (server-only
    characters) should be *disablable*. Previously it was force-added, which made
    /packs disable appear to succeed while the pack remained enabled.
    """
    try:
        s = await list_members(int(guild_id), GUILD_ENABLED_KEY)
        norm = {normalize_pack_id(x) for x in (s or set()) if normalize_pack_id(x)}
        if norm:
            return norm
    except Exception:
        pass
    # Default pack + server-only pseudo pack.
    return {"nardologybot", f"server_{int(guild_id)}"}


async def enable_pack_for_guild(guild_id: int, pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    gid = int(guild_id)
    # If the server has never configured packs, materialize defaults first so
    # "the first enable" doesn't accidentally remove the implicit defaults.
    try:
        existing = await list_members(gid, GUILD_ENABLED_KEY)
        if not existing:
            # Backwards-compatible defaults.
            await list_add(gid, GUILD_ENABLED_KEY, "nardologybot")
            await list_add(gid, GUILD_ENABLED_KEY, f"server_{gid}")
    except Exception:
        pass
    # Treat "already enabled" as success.
    try:
        await list_add(gid, GUILD_ENABLED_KEY, pid)
        return True
    except Exception:
        return False


async def disable_pack_for_guild(guild_id: int, pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    gid = int(guild_id)
    # If the server has never configured packs, defaults are implicit.
    # Materialize them first so disabling a default pack actually works.
    try:
        existing = await list_members(gid, GUILD_ENABLED_KEY)
        if not existing:
            await list_add(gid, GUILD_ENABLED_KEY, "nardologybot")
            await list_add(gid, GUILD_ENABLED_KEY, f"server_{gid}")
    except Exception:
        pass
    return await list_remove(gid, GUILD_ENABLED_KEY, pid)


# -----------------------------
# Phase 4: Featured packs + upvotes
# -----------------------------


async def get_featured_pack_ids() -> set[str]:
    """Return set of featured pack IDs (curated by owner)."""
    r = await get_redis_or_none()
    if r is None:
        return set()
    try:
        members = await r.smembers(FEATURED_PACKS_KEY)
        out: set[str] = set()
        for m in members or []:
            s = m.decode("utf-8", "ignore") if isinstance(m, (bytes, bytearray)) else str(m)
            pid = normalize_pack_id(s)
            if pid:
                out.add(pid)
        return out
    except Exception:
        return set()


async def add_featured_pack(pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        await r.sadd(FEATURED_PACKS_KEY, pid)
        return True
    except Exception:
        return False


async def remove_featured_pack(pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        await r.srem(FEATURED_PACKS_KEY, pid)
        return True
    except Exception:
        return False


async def get_pack_upvote_count(pack_id: str) -> int:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return 0
    r = await get_redis_or_none()
    if r is None:
        return 0
    try:
        return int(await r.scard(_upvotes_key(pid)) or 0)
    except Exception:
        return 0


async def has_user_upvoted(user_id: int, pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        return bool(await r.sismember(_upvotes_key(pid), str(int(user_id))))
    except Exception:
        return False


async def upvote_pack(user_id: int, pack_id: str) -> tuple[bool, int, str]:
    """Upvote a pack. One vote per user. Returns (ok, new_count, message)."""
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False, 0, "Invalid pack."
    r = await get_redis_or_none()
    if r is None:
        return False, 0, "Storage unavailable."
    try:
        key = _upvotes_key(pid)
        await r.sadd(key, str(int(user_id)))
        await r.expire(key, 86400 * 365)
        count = int(await r.scard(key) or 0)
        return True, count, f"✅ Upvoted! **{count}** upvotes."
    except Exception:
        return False, 0, "Upvote failed."


async def list_packs_for_marketplace(
    *,
    sort_by: str = "featured",
    limit: int = 25,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    """List packs for marketplace view: featured first, then by sort."""
    featured = await get_featured_pack_ids()
    custom = await list_custom_packs(limit=500, include_internal=False)
    custom = [p for p in custom if not (isinstance(p, dict) and bool(p.get("private", False)) and not include_private)]

    for p in custom:
        pid = str(p.get("pack_id") or "")
        p["_upvotes"] = await get_pack_upvote_count(pid)
        p["_featured"] = pid in featured

    if sort_by == "featured":
        custom.sort(key=lambda x: (not x.get("_featured", False), -int(x.get("_upvotes", 0))))
    elif sort_by == "popular":
        custom.sort(key=lambda x: -int(x.get("_upvotes", 0)))
    else:
        custom.sort(key=lambda x: str(x.get("pack_id", "")))

    return custom[:limit]


async def get_creator_leaderboard(*, limit: int = 10) -> list[dict[str, Any]]:
    """Top creators by total upvotes across their packs."""
    custom = await list_custom_packs(limit=500, include_internal=False)
    custom = [p for p in custom if not (isinstance(p, dict) and bool(p.get("private", False)))]

    by_creator: dict[str, dict[str, Any]] = {}
    for p in custom:
        if not isinstance(p, dict):
            continue
        uid = p.get("created_by_user") or 0
        gid = p.get("created_by_guild") or 0
        key = f"user:{uid}" if uid else f"guild:{gid}"
        if key not in by_creator:
            by_creator[key] = {"key": key, "user_id": uid, "guild_id": gid, "packs": 0, "chars": 0, "upvotes": 0}
        by_creator[key]["packs"] += 1
        chars = p.get("characters") or []
        by_creator[key]["chars"] += len(chars) if isinstance(chars, list) else 0
        pid = str(p.get("pack_id") or "")
        by_creator[key]["upvotes"] += await get_pack_upvote_count(pid)

    sorted_creators = sorted(
        by_creator.values(),
        key=lambda x: (-int(x["upvotes"]), -int(x["chars"]), -int(x["packs"])),
    )
    return sorted_creators[:limit]


async def is_pack_official(pack_id: str) -> bool:
    """Returns True if pack is built-in or has official=True."""
    from utils.packs_builtin import get_builtin_pack
    if get_builtin_pack(pack_id):
        return True
    p = await get_custom_pack(pack_id)
    return isinstance(p, dict) and bool(p.get("official", False))
