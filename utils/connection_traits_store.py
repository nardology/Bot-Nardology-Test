"""Persistence and validation for connection traits (per user + character)."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from utils.connection_traits_catalog import (
    get_trait,
    hobby_slot_price_after_expansions,
    list_enabled_traits,
    speech_style_price_after_expansions,
)
from utils.character_store import add_points, load_state, owns_style

logger = logging.getLogger("bot.connection_traits")

try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore


def _utc_week_id() -> str:
    """ISO year-week, e.g. 202611 for week 11 of 2026."""
    dt = datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}{int(w):02d}"


def _word_count(text: str) -> int:
    if not (text or "").strip():
        return 0
    return len(re.findall(r"\S+", text.strip()))


def _parse_json(s: str | None) -> dict[str, Any]:
    if not s or not str(s).strip():
        return {}
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


async def load_profile(*, user_id: int, style_id: str) -> dict[str, Any]:
    """Return {purchased: dict, payload: dict} for this pair."""
    if select is None:
        return {"purchased": {}, "payload": {}}
    style_id = (style_id or "").strip().lower()
    uid = int(user_id)
    try:
        from utils.db import get_sessionmaker
        from utils.models import CharacterConnectionProfile

        Session = get_sessionmaker()
        async with Session() as session:
            res = await session.execute(
                select(CharacterConnectionProfile)
                .where(CharacterConnectionProfile.user_id == uid)
                .where(CharacterConnectionProfile.style_id == style_id)
                .limit(1)
            )
            row = res.scalar_one_or_none()
            if row is None:
                return {"purchased": {}, "payload": {}}
            return {
                "purchased": _parse_json(getattr(row, "purchased_traits_json", None)),
                "payload": _parse_json(getattr(row, "payload_json", None)),
            }
    except Exception:
        logger.debug("load_profile failed", exc_info=True)
        return {"purchased": {}, "payload": {}}


async def save_profile(
    *,
    user_id: int,
    style_id: str,
    purchased: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    if select is None:
        raise RuntimeError("sqlalchemy not available")
    style_id = (style_id or "").strip().lower()
    uid = int(user_id)
    from utils.db import get_sessionmaker
    from utils.models import CharacterConnectionProfile

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterConnectionProfile)
            .where(CharacterConnectionProfile.user_id == uid)
            .where(CharacterConnectionProfile.style_id == style_id)
            .limit(1)
        )
        existing = res.scalar_one_or_none()
        p_tr = _parse_json(getattr(existing, "purchased_traits_json", None)) if existing else {}
        p_ld = _parse_json(getattr(existing, "payload_json", None)) if existing else {}
        if purchased is not None:
            p_tr = purchased
        if payload is not None:
            p_ld = payload

        now = datetime.now(timezone.utc)
        if existing is None:
            session.add(
                CharacterConnectionProfile(
                    user_id=uid,
                    style_id=style_id,
                    purchased_traits_json=json.dumps(p_tr, separators=(",", ":")),
                    payload_json=json.dumps(p_ld, separators=(",", ":")),
                    updated_at=now,
                )
            )
        else:
            existing.purchased_traits_json = json.dumps(p_tr, separators=(",", ":"))
            existing.payload_json = json.dumps(p_ld, separators=(",", ":"))
            existing.updated_at = now
        await session.commit()


async def list_profiles_with_trait(trait_id: str) -> list[tuple[int, str]]:
    """Return (user_id, style_id) pairs that have purchased *trait_id* (best-effort)."""
    if select is None:
        return []
    tid = (trait_id or "").strip().lower()
    if not tid:
        return []
    try:
        from utils.db import get_sessionmaker
        from utils.models import CharacterConnectionProfile

        Session = get_sessionmaker()
        async with Session() as session:
            res = await session.execute(select(CharacterConnectionProfile))
            rows = res.scalars().all()
            out: list[tuple[int, str]] = []
            for row in rows or []:
                p = _parse_json(getattr(row, "purchased_traits_json", None))
                if has_trait(p, tid):
                    out.append((int(row.user_id), str(row.style_id)))
            return out
    except Exception:
        logger.debug("list_profiles_with_trait failed", exc_info=True)
        return []


async def get_shard_balance(user_id: int) -> int:
    st = await load_state(user_id)
    return int(getattr(st, "points", 0) or 0)


async def spend_shards(*, user_id: int, amount: int, reason: str = "connection_trait") -> tuple[bool, int]:
    """Spend shards (character_user_state.points). Returns (ok, new_balance)."""
    amount = int(amount or 0)
    if amount <= 0:
        return True, await get_shard_balance(user_id)
    bal = await get_shard_balance(user_id)
    if bal < amount:
        return False, bal
    new_b = await add_points(user_id, -amount)
    return True, int(new_b or 0)


def has_trait(purchased: dict[str, Any], trait_id: str) -> bool:
    tid = (trait_id or "").strip().lower()
    v = purchased.get(tid)
    if v is None:
        return False
    if isinstance(v, dict):
        return bool(v.get("active", True))
    return True


def price_for_trait_purchase(
    trait_id: str,
    purchased: dict[str, Any],
    *,
    kind: str | None = None,
) -> tuple[int, str]:
    """Return (cost, error_message). kind: None | hobby_slot | speech_expand | remember_name_edit."""
    tid = (trait_id or "").strip().lower()
    tdef = get_trait(tid)
    if tdef is None or not tdef.enabled:
        return 0, "That trait is not available."

    if kind == "remember_name_edit":
        if not has_trait(purchased, "remember_name"):
            return 0, "Buy **Remember your name** first."
        return 15, ""

    if tid == "hobbies" and kind == "hobby_slot":
        if not has_trait(purchased, "hobbies"):
            return 0, "Buy **Hobbies** first."
        exp = int((purchased.get("hobbies") or {}).get("slot_expansions") or 0)
        return hobby_slot_price_after_expansions(exp), ""

    if tid == "speech_style" and kind == "speech_expand":
        if not has_trait(purchased, "speech_style"):
            return 0, "Buy **Speech style** first."
        exp = int((purchased.get("speech_style") or {}).get("word_expansions") or 0)
        return speech_style_price_after_expansions(exp), ""

    if has_trait(purchased, tid):
        return 0, "You already own this trait (use upgrades where available)."

    return int(tdef.base_shard_cost), ""


async def purchase_trait(
    *,
    user_id: int,
    style_id: str,
    trait_id: str,
    kind: str | None = None,
) -> tuple[bool, str]:
    """Buy a trait or an expansion. Validates ownership of character."""
    style_id = (style_id or "").strip().lower()
    tid = (trait_id or "").strip().lower()
    if not await owns_style(user_id, style_id):
        return False, "You don't own that character."

    data = await load_profile(user_id=user_id, style_id=style_id)
    purchased = dict(data.get("purchased") or {})

    cost, err = price_for_trait_purchase(tid, purchased, kind=kind)
    if err:
        return False, err
    if cost <= 0:
        return False, "Invalid price."

    ok, _ = await spend_shards(user_id=user_id, amount=cost, reason=f"trait:{tid}:{kind or 'base'}")
    if not ok:
        return False, f"Not enough shards. Need **{cost}**."

    now = datetime.now(timezone.utc).isoformat()

    if tid == "remember_name" and kind == "remember_name_edit":
        meta = dict(purchased.get("remember_name") or {})
        meta["edits"] = int(meta.get("edits") or 0) + 1
        purchased["remember_name"] = meta
        await save_profile(user_id=user_id, style_id=style_id, purchased=purchased)
        return True, f"Paid **{cost}** shards for a name edit."

    if tid == "hobbies" and kind == "hobby_slot":
        meta = dict(purchased.get("hobbies") or {})
        meta["slot_expansions"] = int(meta.get("slot_expansions") or 0) + 1
        purchased["hobbies"] = {**meta, "purchased_at": meta.get("purchased_at") or now}
        await save_profile(user_id=user_id, style_id=style_id, purchased=purchased)
        return True, f"Added **+1** hobby slot (**{cost}** shards)."

    if tid == "speech_style" and kind == "speech_expand":
        meta = dict(purchased.get("speech_style") or {})
        meta["word_expansions"] = int(meta.get("word_expansions") or 0) + 1
        purchased["speech_style"] = {**meta, "purchased_at": meta.get("purchased_at") or now}
        await save_profile(user_id=user_id, style_id=style_id, purchased=purchased)
        return True, f"Extended speech style word limit (**{cost}** shards)."

    # First-time trait purchase
    if tid == "hobbies":
        purchased["hobbies"] = {"purchased_at": now, "active": True, "slot_expansions": 0}
    elif tid == "speech_style":
        purchased["speech_style"] = {"purchased_at": now, "active": True, "word_expansions": 0}
    else:
        purchased[tid] = {"purchased_at": now, "active": True}

    payload = dict(data.get("payload") or {})
    if tid in ("weekly_life", "daily_status"):
        payload.setdefault("week_id", _utc_week_id())
    await save_profile(user_id=user_id, style_id=style_id, purchased=purchased, payload=payload)
    return True, f"Unlocked **{get_trait(tid).title if get_trait(tid) else tid}** for **{cost}** shards."


async def update_payload_fields(
    *,
    user_id: int,
    style_id: str,
    fields: dict[str, Any],
) -> tuple[bool, str]:
    """Merge validated fields into payload (caller enforces gates)."""
    if not await owns_style(user_id, style_id):
        return False, "You don't own that character."
    data = await load_profile(user_id=user_id, style_id=style_id)
    payload = dict(data.get("payload") or {})
    purchased = dict(data.get("purchased") or {})

    for k, v in fields.items():
        if k == "display_name":
            if not has_trait(purchased, "remember_name"):
                return False, "Trait **Remember your name** required."
            s = (v or "").strip()
            if _word_count(s) > 10:
                return False, "Name must be **10 words or fewer**."
            payload["display_name"] = s
        elif k == "hobbies":
            if not has_trait(purchased, "hobbies"):
                return False, "Trait **Hobbies** required."
            slots = 3 + int((purchased.get("hobbies") or {}).get("slot_expansions") or 0)
            if not isinstance(v, list):
                return False, "Hobbies must be a list."
            hs = [str(x).strip() for x in v if str(x).strip()][:slots]
            for h in hs:
                if _word_count(h) > 50:
                    return False, "Each hobby must be **50 words or fewer**."
            payload["hobbies"] = hs
        elif k == "speech_style":
            if not has_trait(purchased, "speech_style"):
                return False, "Trait **Speech style** required."
            exp = int((purchased.get("speech_style") or {}).get("word_expansions") or 0)
            max_words = 150 + 100 * exp
            s = (v or "").strip()
            if _word_count(s) > max_words:
                return False, f"Speech style must be **{max_words}** words or fewer."
            payload["speech_style"] = s
        elif k == "weekly_status":
            if not has_trait(purchased, "weekly_life") and not has_trait(purchased, "daily_status"):
                return False, "Trait **Weekly life** or **Daily status** required."
            s = (v or "").strip()
            if _word_count(s) > 250:
                return False, "Weekly status must be **250 words or fewer**."
            payload["weekly_status"] = s
            payload["week_id"] = _utc_week_id()
        elif k == "daily_today":
            if not has_trait(purchased, "daily_status"):
                return False, "Trait **Daily status** required."
            s = (v or "").strip()
            if _word_count(s) > 100:
                return False, "Daily entry must be **100 words or fewer**."
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            dailies = dict(payload.get("daily_by_day") or {})
            dailies[day] = s
            payload["daily_by_day"] = dailies
        else:
            payload[k] = v

    await save_profile(user_id=user_id, style_id=style_id, payload=payload)
    return True, "Saved."


def build_prompt_context(
    *,
    payload: dict[str, Any],
    purchased: dict[str, Any],
    memory_tier: str = "none",
    max_chars: int = 3500,
) -> str:
    """Bounded text block for /talk injection."""
    parts: list[str] = []
    if has_trait(purchased, "remember_name") and (payload.get("display_name") or "").strip():
        preferred_name = payload["display_name"].strip()
        parts.append(f"User prefers to be called: {preferred_name}")
        # Strong instruction to reduce model drift/forgetfulness on direct name questions.
        parts.append(
            "If the user asks what their name is (or how to address them), "
            f"answer with: {preferred_name}. Do not say you do not know."
        )
    if has_trait(purchased, "hobbies") and payload.get("hobbies"):
        parts.append("User hobbies/interests: " + " | ".join(payload["hobbies"][:12]))
    if has_trait(purchased, "speech_style") and (payload.get("speech_style") or "").strip():
        parts.append("How the user wants you to speak to them: " + (payload["speech_style"] or "").strip())
    if has_trait(purchased, "weekly_life") or has_trait(purchased, "daily_status"):
        if (payload.get("weekly_status") or "").strip():
            parts.append("This week's life context (user-provided): " + (payload["weekly_status"] or "").strip())
    if has_trait(purchased, "daily_status") and payload.get("daily_by_day"):
        # last 7 days only
        keys = sorted((payload.get("daily_by_day") or {}).keys())[-7:]
        for d in keys:
            line = (payload["daily_by_day"] or {}).get(d)
            if line:
                parts.append(f"Daily note {d}: {line}")

    if has_trait(purchased, "memory_semi") or memory_tier in {"semi", "permanent"}:
        # older weekly/daily retained in payload under archived keys — MVP uses same payload
        pass

    out = "\n".join(parts).strip()
    if len(out) > max_chars:
        out = out[: max_chars - 3] + "..."
    return out


__all__ = [
    "load_profile",
    "save_profile",
    "get_shard_balance",
    "spend_shards",
    "purchase_trait",
    "update_payload_fields",
    "has_trait",
    "price_for_trait_purchase",
    "build_prompt_context",
    "_utc_week_id",
    "_word_count",
]
