from __future__ import annotations

"""Shop item storage for limited/exclusive offers.

We store owner-curated shop items in Redis so seasonal packs / one-off
characters can be added/removed without redeploying.

Keys:
- Set: shop:items -> item_ids
- String: shop:{item_id} -> JSON dict
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger("bot.shop_store")

from utils.backpressure import get_redis_or_none

SHOP_INDEX_KEY = "shop:items"
SHOP_LIMITED_STYLES_KEY = "shop:limited_styles"


def normalize_item_id(item_id: str) -> str:
    s = (item_id or "").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_-")
    return s[:48] if s else ""


def _item_key(item_id: str) -> str:
    return f"shop:{item_id}"


async def list_shop_items(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return shop items. By default only active; set include_inactive=True to include deactivated."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        ids = await r.smembers(SHOP_INDEX_KEY)
        out: list[dict[str, Any]] = []
        for raw in ids or []:
            iid = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
            iid = normalize_item_id(iid)
            if not iid:
                continue
            it = await get_shop_item(iid)
            if not it:
                continue
            if not include_inactive and not bool(it.get("active", True)):
                continue
            out.append(it)
        out.sort(key=lambda d: str(d.get("item_id") or ""))
        return out
    except Exception:
        return []


async def get_shop_item(item_id: str) -> dict[str, Any] | None:
    iid = normalize_item_id(item_id)
    if not iid:
        return None
    r = await get_redis_or_none()
    if r is None:
        return None
    try:
        raw = await r.get(_item_key(iid))
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        d = json.loads(str(raw))
        if not isinstance(d, dict):
            return None
        d["item_id"] = iid
        return d
    except Exception:
        return None


async def upsert_shop_item(payload: dict[str, Any]) -> bool:
    iid = normalize_item_id(str(payload.get("item_id") or ""))
    if not iid:
        return False
    p = dict(payload)
    p["item_id"] = iid
    p.setdefault("active", True)

    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        await r.set(_item_key(iid), json.dumps(p, separators=(",", ":")))
        await r.sadd(SHOP_INDEX_KEY, iid)
        # If this item grants a limited character, remember it so we can display EXCLUSIVE on that character.
        if str(p.get("kind") or "") == "character_grant" and bool(p.get("exclusive", True)):
            sid = str(p.get("style_id") or "").strip().lower()
            if sid:
                await r.sadd(SHOP_LIMITED_STYLES_KEY, sid)
        return True
    except Exception:
        return False


async def delete_shop_item(item_id: str) -> bool:
    iid = normalize_item_id(item_id)
    if not iid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        # Best-effort: remove limited style marker if this item granted a character.
        existing = await get_shop_item(iid)
        if existing and str(existing.get("kind") or "") == "character_grant":
            sid = str(existing.get("style_id") or "").strip().lower()
            if sid:
                try:
                    await r.srem(SHOP_LIMITED_STYLES_KEY, sid)
                except Exception:
                    pass

        await r.delete(_item_key(iid))
        await r.srem(SHOP_INDEX_KEY, iid)
        return True
    except Exception:
        return False


async def is_limited_style(style_id: str) -> bool:
    sid = (style_id or "").strip().lower()
    if not sid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        return bool(await r.sismember(SHOP_LIMITED_STYLES_KEY, sid))
    except Exception:
        return False


async def set_limited_style(style_id: str, limited: bool) -> bool:
    """Owner helper: mark/unmark a style id as LIMITED/EXCLUSIVE for display."""
    sid = (style_id or "").strip().lower()
    if not sid:
        return False
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        if bool(limited):
            await r.sadd(SHOP_LIMITED_STYLES_KEY, sid)
        else:
            await r.srem(SHOP_LIMITED_STYLES_KEY, sid)
        return True
    except Exception:
        return False


async def sync_shop_items_from_registry() -> int:
    """Push every JSON-defined shop item into Redis (JSON always wins).

    Called at startup and via ``/z_owner shop_sync``.  Returns the number
    of items successfully upserted.
    """
    from utils.character_registry import get_shop_item_defs

    defs = get_shop_item_defs()
    if not defs:
        return 0

    synced = 0
    for style_id, si in defs.items():
        payload = dict(si)
        payload.setdefault("item_id", style_id)
        payload.setdefault("style_id", style_id)
        payload.setdefault("kind", "character_grant")
        payload.setdefault("active", True)
        try:
            if await upsert_shop_item(payload):
                synced += 1
            if str(payload.get("kind")) == "character_grant" and payload.get("exclusive"):
                await set_limited_style(style_id, True)
        except Exception:
            logger.warning("Failed to sync shop item %s", style_id, exc_info=True)

    logger.info("Shop sync complete: %d/%d items synced from JSON.", synced, len(defs))
    return synced
