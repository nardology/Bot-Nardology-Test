from __future__ import annotations

"""Pack/character status badges.

This module centralizes the loud COMMUNITY / OFFICIAL / EXCLUSIVE tags so they
can be displayed consistently across commands.

Business rules:
- Built-in packs are always OFFICIAL.
- Custom packs default to COMMUNITY unless the bot owner marks them OFFICIAL.
- Shop-exclusive/limited packs/characters are OFFICIAL + EXCLUSIVE.
- Phase 5: Trusted creators (high trust score / auto_approve) get VERIFIED badge.
"""

from typing import Any

from utils.packs_builtin import get_builtin_pack
from utils.packs_store import get_custom_pack, normalize_pack_id
from utils.shop_store import is_limited_style
from utils.verification import get_trust_score

COMMUNITY_BADGE = "🎨🌱COMMUNITY🌱🎨"
OFFICIAL_BADGE = "⭐✅OFFICIAL✅⭐"
EXCLUSIVE_BADGE = "🕒EXCLUSIVE🕒"
VERIFIED_BADGE = "✓ Verified Creator"


def badges_for_pack_payload(pack: dict[str, Any] | None) -> str:
    """Return badge string for a pack payload (sync).

    This is used where we already have pack metadata in memory.
    """
    if not isinstance(pack, dict):
        return COMMUNITY_BADGE

    pid = normalize_pack_id(str(pack.get("pack_id") or ""))
    if pid and get_builtin_pack(pid):
        # Built-in packs are always official.
        base = OFFICIAL_BADGE
    else:
        base = OFFICIAL_BADGE if bool(pack.get("official", False)) else COMMUNITY_BADGE

    if bool(pack.get("exclusive", False)):
        return f"{base} {EXCLUSIVE_BADGE}"
    return base


async def badges_for_pack_id(pack_id: str) -> str:
    """Return badge string for a pack id (async lookup).

    Phase 5: Appends VERIFIED badge when pack creator has high trust (auto_approve).
    """
    pid = normalize_pack_id(pack_id)
    if not pid:
        return COMMUNITY_BADGE

    # Built-in
    if get_builtin_pack(pid):
        return OFFICIAL_BADGE

    # Server pseudo-pack (server-only characters) is a community/server-made concept.
    if pid.startswith("server_"):
        return COMMUNITY_BADGE

    p = await get_custom_pack(pid)
    base = badges_for_pack_payload(p)
    # Phase 5: Verified creator badge (trusted creator = high trust score / auto_approve)
    if isinstance(p, dict):
        creator_uid = int(p.get("created_by_user") or 0)
        creator_gid = int(p.get("created_by_guild") or 0)
        if creator_uid and creator_gid:
            try:
                trust = await get_trust_score(guild_id=creator_gid, user_id=creator_uid)
                if trust.get("auto_approve") or int(trust.get("approved") or 0) >= 5:
                    base = f"{base} {VERIFIED_BADGE}".strip()
            except Exception:
                pass
    return base


async def badges_for_style_id(style_id: str) -> str:
    """Return badge string for a style id.

    Uses the style's pack_id (from registry) then resolves pack flags.
    """
    try:
        from utils.character_registry import get_style

        s = get_style((style_id or "").strip().lower())
        if not s:
            return COMMUNITY_BADGE
        pid = str(getattr(s, "pack_id", "") or "")
        badge = await badges_for_pack_id(pid)
        # A shop may sell a single limited character even if its pack isn't exclusive.
        if await is_limited_style(style_id):
            if EXCLUSIVE_BADGE not in badge:
                badge = f"{badge} {EXCLUSIVE_BADGE}".strip()
        return badge
    except Exception:
        return COMMUNITY_BADGE
