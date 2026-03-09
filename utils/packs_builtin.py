from __future__ import annotations

"""Built-in pack metadata.

We treat the existing "server characters" (the ones shipped with the bot)
as a single pack: `nardologybot`.

Custom packs created via /packs are stored in Redis (see utils.packs_store).
"""

from typing import Any

from utils.character_registry import STYLE_DEFS, get_shop_item_defs


BUILTIN_PACKS: dict[str, dict[str, Any]] = {
    "nardologybot": {
        "type": "pack",
        "pack_id": "nardologybot",
        "name": "NardologyBot Pack",
        "description": "The default pack shipped with the bot (all built-in rollable characters).",
        "created_by": "builtin",
    },
}


def list_builtin_packs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pid, meta in BUILTIN_PACKS.items():
        meta = dict(meta)
        meta["pack_id"] = pid
        # derive characters from registry
        chars = []
        shop_defs = get_shop_item_defs()
        for s in STYLE_DEFS.values():
            if (getattr(s, "pack_id", "") or "") != pid:
                continue
            is_rollable = getattr(s, "rollable", False)
            si = shop_defs.get(s.style_id)
            is_shop_exclusive = bool(si and si.get("exclusive"))
            if not is_rollable and not is_shop_exclusive:
                continue
            entry: dict[str, Any] = {
                "id": s.style_id,
                "display_name": s.display_name,
                "rarity": s.rarity,
                "description": s.description,
                "image_url": s.image_url,
            }
            if si:
                entry["shop_item"] = si
            chars.append(entry)
        chars.sort(key=lambda c: (str(c.get("rarity") or ""), str(c.get("id") or "")))
        meta["characters"] = chars
        out.append(meta)
    out.sort(key=lambda d: str(d.get("pack_id") or ""))
    return out


def get_builtin_pack(pack_id: str) -> dict[str, Any] | None:
    pid = (pack_id or "").strip().lower()
    for p in list_builtin_packs():
        if str(p.get("pack_id") or "").lower() == pid:
            return p
    return None
