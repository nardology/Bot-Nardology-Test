from __future__ import annotations

"""Character persistence (Postgres).

Fast migration goal:
  - Durable state (currency, active character, inventory, custom characters) lives in Postgres.
  - Ephemeral pity JSON blob used by older UI remains in Redis (safe to lose).

We keep the same public function signatures so commands do not break.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import json
import time

from utils.backpressure import get_redis_or_none
from utils.character_registry import BASE_STYLE_IDS, disable_style, get_style, merge_pack_payload
from utils.packs_store import list_custom_packs, normalize_style_id
from utils.db import get_sessionmaker
from utils.models import CharacterCustomStyle, CharacterOwnedStyle, CharacterUserState

try:
    from sqlalchemy import delete, select  # type: ignore
except Exception:  # pragma: no cover
    delete = None  # type: ignore
    select = None  # type: ignore


ROLLS_PER_DAY_FREE = 1
ROLLS_PER_DAY_PRO = 3

# Roll cooldown: sliding window (default 5 hours). Set ROLL_WINDOW_SECONDS=18000 for 5h, 0 for calendar-day.
# When > 0, user gets ROLLS_PER_DAY_PRO/ROLLS_PER_DAY_FREE per window (Pro 3, Free 1).
def _roll_window_seconds() -> int:
    import os
    raw = (os.environ.get("ROLL_WINDOW_SECONDS") or "").strip()
    if raw == "":
        return 18000  # default 5 hours
    if raw == "0" or not raw.isdigit():
        return 0
    return max(0, int(raw))

_ROLL_WINDOW_PREFIX = "char:roll_window"


def _roll_window_key(user_id: int) -> str:
    return f"{_ROLL_WINDOW_PREFIX}:{int(user_id)}"


def roll_window_seconds() -> int:
    """Public: roll window in seconds (0 = calendar day). Used for UI (e.g. 'per 5h')."""
    return _roll_window_seconds()


async def clear_roll_window(*, user_id: int) -> None:
    """Clear the sliding-window roll state in Redis so the user gets a fresh window (e.g. for /character reset)."""
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.delete(_roll_window_key(user_id))
    except Exception:
        pass


_PITY_KEY_PREFIX = "char:pity"
_PITY_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Bonus/onboarding rolls live in Redis (ephemeral and safe to lose).
_BONUS_ROLL_KEY = "char:bonus_rolls"
_ONBOARDED_KEY = "char:onboarded"


def _bonus_key(user_id: int) -> str:
    return f"{_BONUS_ROLL_KEY}:{int(user_id)}"


def _onboarded_key(user_id: int) -> str:
    return f"{_ONBOARDED_KEY}:{int(user_id)}"


def _pity_key(guild_id: int, user_id: int) -> str:
    return f"{_PITY_KEY_PREFIX}:{int(guild_id)}:{int(user_id)}"


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _norm(style_id: str | None) -> str:
    return (style_id or "").strip().lower()


def _norm_style_id(style_id: str | None) -> str:
    """Back-compat alias. We renamed 'style' -> 'character' but some owner utilities still call this."""
    return _norm(style_id)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CharacterState:
    user_id: int
    active_style_id: str = ""
    points: int = 0
    roll_day: str = ""
    roll_used: int = 0
    pity_mythic: int = 0
    pity_legendary: int = 0
    owned_custom: list[str] = field(default_factory=list)


@dataclass
class CustomStyleProfile:
    user_id: int
    style_id: str
    name: str
    prompt: str
    created_at: float


async def _get_or_create_user_state_row(user_id: int) -> CharacterUserState:
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        row = await session.get(CharacterUserState, int(user_id))
        if row is None:
            row = CharacterUserState(
                user_id=int(user_id),
                active_style_id="",
                points=0,
                roll_day="",
                roll_used=0,
                pity_mythic=0,
                pity_legendary=0,
                updated_at=_now_utc(),
            )
            session.add(row)
            await session.commit()
        return row


async def load_state(user_id: int) -> CharacterState:
    """Load durable character state from Postgres."""
    if select is None:
        # Shouldn't happen in this migration (requirements include SQLAlchemy),
        # but keep a clear failure mode.
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        row = await session.get(CharacterUserState, int(user_id))
        if row is None:
            # Don't eagerly insert for every random user unless needed.
            row = CharacterUserState(user_id=int(user_id))

        # inventory = owned registry styles + custom styles
        owned_rows = await session.execute(
            select(CharacterOwnedStyle.style_id).where(CharacterOwnedStyle.user_id == int(user_id))
        )
        custom_rows = await session.execute(
            select(CharacterCustomStyle.style_id).where(CharacterCustomStyle.user_id == int(user_id))
        )

        styles: set[str] = {"fun"}
        for sid in owned_rows.scalars().all() or []:
            s = _norm(str(sid))
            if s:
                styles.add(s)
        for sid in custom_rows.scalars().all() or []:
            s = _norm(str(sid))
            if s:
                styles.add(s)

        return CharacterState(
            user_id=int(user_id),
            active_style_id=_norm(getattr(row, "active_style_id", "") or ""),
            points=int(getattr(row, "points", 0) or 0),
            roll_day=str(getattr(row, "roll_day", "") or ""),
            roll_used=int(getattr(row, "roll_used", 0) or 0),
            pity_mythic=int(getattr(row, "pity_mythic", 0) or 0),
            pity_legendary=int(getattr(row, "pity_legendary", 0) or 0),
            owned_custom=sorted(styles),
        )


async def _save_state(st: CharacterState) -> None:
    """Upsert user state row."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        row = await session.get(CharacterUserState, int(st.user_id))
        if row is None:
            row = CharacterUserState(user_id=int(st.user_id))
            session.add(row)

        row.active_style_id = _norm(st.active_style_id)
        row.points = int(st.points or 0)
        row.roll_day = str(st.roll_day or "")
        row.roll_used = int(st.roll_used or 0)
        row.pity_mythic = int(st.pity_mythic or 0)
        row.pity_legendary = int(st.pity_legendary or 0)
        row.updated_at = _now_utc()

        await session.commit()


# ----------------------------
# Inventory helpers
# ----------------------------

async def get_all_owned_style_ids(user_id: int) -> set[str]:
    if select is None:
        raise RuntimeError("sqlalchemy not available")
    Session = get_sessionmaker()
    async with Session() as session:
        owned_rows = await session.execute(
            select(CharacterOwnedStyle.style_id).where(CharacterOwnedStyle.user_id == int(user_id))
        )
        custom_rows = await session.execute(
            select(CharacterCustomStyle.style_id).where(CharacterCustomStyle.user_id == int(user_id))
        )
        out: set[str] = {"fun"}
        for sid in owned_rows.scalars().all() or []:
            s = _norm(str(sid))
            if s:
                out.add(s)
        for sid in custom_rows.scalars().all() or []:
            s = _norm(str(sid))
            if s:
                out.add(s)
        return out


async def owns_style(user_id: int, style_id: str) -> bool:
    """Check both CharacterOwnedStyle and CharacterCustomStyle so shop/roll characters are found."""
    style_id = _norm(style_id)
    if not style_id:
        return False
    if style_id == "fun":
        return True

    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        # Registry-owned (including shop pack_roll/character_grant) are in CharacterOwnedStyle.
        res_owned = await session.execute(
            select(CharacterOwnedStyle.id)
            .where(CharacterOwnedStyle.user_id == int(user_id))
            .where(CharacterOwnedStyle.style_id == style_id)
            .limit(1)
        )
        if res_owned.scalar_one_or_none() is not None:
            return True
        # Custom profiles are in CharacterCustomStyle.
        res_custom = await session.execute(
            select(CharacterCustomStyle.id)
            .where(CharacterCustomStyle.user_id == int(user_id))
            .where(CharacterCustomStyle.style_id == style_id)
            .limit(1)
        )
        return res_custom.scalar_one_or_none() is not None


async def append_owned_style(user_id: int, style_id: str, *, guild_id: int | None = None) -> None:
    style_id = _norm(style_id)
    if not style_id or style_id == "fun":
        return

    if select is None:
        raise RuntimeError("sqlalchemy not available")

    # Only registry styles go in the owned table.
    if not get_style(style_id):
        return

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterOwnedStyle.id)
            .where(CharacterOwnedStyle.user_id == int(user_id))
            .where(CharacterOwnedStyle.style_id == style_id)
            .limit(1)
        )
        if res.scalar_one_or_none() is None:
            session.add(CharacterOwnedStyle(user_id=int(user_id), style_id=style_id))
            await session.commit()
            
            # Update leaderboard for character count (global + server when guild_id provided)
            try:
                from utils.leaderboard import update_all_periods, CATEGORY_CHARACTERS, GLOBAL_GUILD_ID
                owned_set = await get_all_owned_style_ids(user_id)
                count = float(len(owned_set))
                await update_all_periods(
                    category=CATEGORY_CHARACTERS,
                    guild_id=GLOBAL_GUILD_ID,
                    user_id=user_id,
                    value=count,
                )
                if guild_id is not None and int(guild_id) != GLOBAL_GUILD_ID:
                    await update_all_periods(
                        category=CATEGORY_CHARACTERS,
                        guild_id=int(guild_id),
                        user_id=user_id,
                        value=count,
                    )
            except Exception:
                pass


async def remove_owned_style(user_id: int, style_id: str) -> None:
    style_id = _norm(style_id)
    if not style_id:
        return

    if delete is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        await session.execute(
            delete(CharacterOwnedStyle)
            .where(CharacterOwnedStyle.user_id == int(user_id))
            .where(CharacterOwnedStyle.style_id == style_id)
        )
        await session.commit()


# ----------------------------
# Active selection
# ----------------------------

async def set_active_style(user_id: int, style_id: str | None) -> tuple[bool, str]:
    style_id_norm = _norm(style_id)

    # Clear selection
    if not style_id_norm:
        st = await load_state(user_id)
        st.active_style_id = ""
        await _save_state(st)
        return True, "Cleared selection."

    if not await owns_style(user_id, style_id_norm):
        return False, "You don't own that style."

    st = await load_state(user_id)
    st.active_style_id = style_id_norm
    await _save_state(st)
    return True, ""


async def clear_active_style(user_id: int) -> None:
    st = await load_state(user_id)
    st.active_style_id = ""
    await _save_state(st)


# ----------------------------
# Currency
# ----------------------------

async def add_points(user_id: int, amount: int) -> int:
    amount = int(amount or 0)
    st = await load_state(user_id)
    st.points = max(0, int(st.points or 0) + amount)
    await _save_state(st)
    return int(st.points)


async def purchase_style(user_id: int, style_id: str) -> tuple[bool, str]:
    style_id = _norm(style_id)
    stdef = get_style(style_id)
    if not stdef:
        return False, "Unknown style."
    if style_id == "fun":
        return False, "You already have fun."
    if await owns_style(user_id, style_id):
        return False, "You already own that style."

    cost = int(getattr(stdef, "cost_points", 0) or 0)
    st = await load_state(user_id)
    if int(st.points or 0) < cost:
        return False, f"Not enough points. Need **{cost}**."

    st.points = int(st.points) - cost
    await _save_state(st)
    await append_owned_style(user_id, style_id)
    return True, ""


# ----------------------------
# Rolls (single source of truth)
# ----------------------------

async def can_roll(*, user_id: int, tier: str) -> tuple[bool, int, int]:
    """Return (allowed, remaining_today, per_day_limit)."""
    tier = (tier or "free").strip().lower()
    per_day = ROLLS_PER_DAY_PRO if tier == "pro" else ROLLS_PER_DAY_FREE
    bonus = await get_bonus_rolls(user_id=user_id)
    window_s = _roll_window_seconds()

    if window_s > 0:
        # Sliding window: Pro gets ROLLS_PER_DAY_PRO per window, Free gets ROLLS_PER_DAY_FREE (e.g. 3 vs 1 per 5h).
        r = await get_redis_or_none()
        if r is None:
            return (bonus > 0), max(0, int(bonus or 0)), per_day
        now = int(time.time())
        max_rolls = per_day
        key = _roll_window_key(user_id)
        try:
            raw = await r.get(key)
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                start_ts = int(data.get("s") or 0)
                used = int(data.get("u") or 0)
                if now - start_ts >= window_s:
                    start_ts = now
                    used = 0
                remaining_window = max(0, max_rolls - used)
            else:
                remaining_window = max_rolls
        except Exception:
            remaining_window = max_rolls
        remaining_total = remaining_window + max(0, int(bonus or 0))
        return (remaining_total > 0), remaining_total, per_day
    # Calendar-day logic
    st = await load_state(user_id)
    today = _utc_day()
    if st.roll_day != today:
        st.roll_day = today
        st.roll_used = 0
        await _save_state(st)

    remaining = max(0, per_day - int(st.roll_used or 0))
    remaining_total = remaining + max(0, int(bonus or 0))
    return (remaining_total > 0), remaining_total, per_day


async def get_bonus_rolls(*, user_id: int) -> int:
    """Returns current bonus rolls for the user (Redis)."""
    r = await get_redis_or_none()
    if r is None:
        return 0
    try:
        v = await r.get(_bonus_key(user_id))
        return int(v or 0)
    except Exception:
        return 0


async def grant_bonus_rolls(*, user_id: int, amount: int = 1, ttl_days: int = 7) -> int:
    """Increment bonus rolls (Redis) and set a TTL."""
    amount = max(1, int(amount))
    ttl_s = max(3600, int(ttl_days) * 86400)
    r = await get_redis_or_none()
    if r is None:
        return 0
    key = _bonus_key(user_id)
    try:
        val = int(await r.incrby(key, amount))
        await r.expire(key, ttl_s)
        return val
    except Exception:
        return 0


async def consume_bonus_roll(*, user_id: int) -> bool:
    """Consume 1 bonus roll if present. Returns True if consumed."""
    r = await get_redis_or_none()
    if r is None:
        return False
    key = _bonus_key(user_id)
    # small atomic pattern using a Lua script (avoid negative)
    lua = """
    local k = KEYS[1]
    local v = tonumber(redis.call('GET', k) or '0')
    if v <= 0 then return 0 end
    v = v - 1
    redis.call('SET', k, v)
    return 1
    """
    try:
        res = await r.eval(lua, 1, key)
        return bool(int(res or 0))
    except Exception:
        return False


async def grant_onboarding_roll(*, user_id: int) -> bool:
    """One-time onboarding: grant +1 bonus roll. Returns True if granted."""
    r = await get_redis_or_none()
    if r is None:
        return False
    ok_key = _onboarded_key(user_id)
    try:
        # SETNX so it only grants once. Keep a long TTL so Redis size is bounded.
        created = await r.set(ok_key, "1", nx=True, ex=86400 * 365)
        if not created:
            return False
        await grant_bonus_rolls(user_id=user_id, amount=1, ttl_days=30)
        return True
    except Exception:
        return False


async def consume_roll(*, user_id: int) -> None:
    """Consume a roll: uses bonus roll if available, else window roll or daily roll_used."""
    if await consume_bonus_roll(user_id=user_id):
        return
    window_s = _roll_window_seconds()
    if window_s > 0:
        r = await get_redis_or_none()
        if r is not None:
            try:
                now = int(time.time())
                key = _roll_window_key(user_id)
                raw = await r.get(key)
                if raw:
                    data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                    start_ts = int(data.get("s") or 0)
                    used = int(data.get("u") or 0)
                    if now - start_ts >= window_s:
                        start_ts = now
                        used = 0
                else:
                    start_ts = now
                    used = 0
                used += 1
                await r.set(key, json.dumps({"s": start_ts, "u": used}), ex=window_s + 86400)
            except Exception:
                pass
        return
    await increment_roll_used(user_id=user_id)


async def can_roll_is_pro(*, user_id: int, is_pro: bool) -> tuple[bool, int, int]:
    return await can_roll(user_id=user_id, tier=("pro" if is_pro else "free"))


async def get_roll_retry_after_seconds(*, user_id: int, tier: str) -> int:
    """When using roll window and user is out of rolls, return seconds until window resets. Else 0."""
    window_s = _roll_window_seconds()
    if window_s <= 0:
        return 0
    r = await get_redis_or_none()
    if r is None:
        return 0
    now = int(time.time())
    key = _roll_window_key(user_id)
    try:
        raw = await r.get(key)
        if not raw:
            return 0
        data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
        start_ts = int(data.get("s") or 0)
        used = int(data.get("u") or 0)
        max_rolls = ROLLS_PER_DAY_PRO if (tier or "").strip().lower() == "pro" else ROLLS_PER_DAY_FREE
        if used < max_rolls:
            return 0
        # Window resets at start_ts + window_s
        reset_at = start_ts + window_s
        return max(0, reset_at - now)
    except Exception:
        return 0


async def increment_roll_used(*, user_id: int) -> None:
    st = await load_state(user_id)
    today = _utc_day()
    if st.roll_day != today:
        st.roll_day = today
        st.roll_used = 0
    st.roll_used = int(st.roll_used or 0) + 1
    await _save_state(st)


async def get_pity(*, user_id: int) -> tuple[int, int]:
    st = await load_state(user_id)
    return int(st.pity_mythic or 0), int(st.pity_legendary or 0)


async def set_pity(*, user_id: int, pity_mythic: int, pity_legendary: int) -> None:
    st = await load_state(user_id)
    st.pity_mythic = max(0, int(pity_mythic or 0))
    st.pity_legendary = max(0, int(pity_legendary or 0))
    await _save_state(st)


# ----------------------------
# Custom styles
# ----------------------------

async def upsert_custom_style_profile(*, user_id: int, style_id: str, name: str, prompt: str) -> None:
    style_id = _norm(style_id)
    if not style_id:
        raise ValueError("style_id required")

    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterCustomStyle).where(
                CharacterCustomStyle.user_id == int(user_id),
                CharacterCustomStyle.style_id == style_id,
            )
        )
        row = res.scalar_one_or_none()
        now = _now_utc()
        if row is None:
            row = CharacterCustomStyle(
                user_id=int(user_id),
                style_id=style_id,
                name=(name or "").strip()[:50],
                prompt=(prompt or "")[:1500],
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.name = (name or "").strip()[:50]
            row.prompt = (prompt or "")[:1500]
            row.updated_at = now
        await session.commit()


async def get_custom_style_profile(*, user_id: int, style_id: str) -> CustomStyleProfile | None:
    style_id = _norm(style_id)
    if not style_id:
        return None

    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterCustomStyle).where(
                CharacterCustomStyle.user_id == int(user_id),
                CharacterCustomStyle.style_id == style_id,
            )
        )
        row = res.scalar_one_or_none()
        if row is None:
            return None
        created = getattr(row, "created_at", None) or _now_utc()
        return CustomStyleProfile(
            user_id=int(user_id),
            style_id=style_id,
            name=(getattr(row, "name", "") or style_id),
            prompt=(getattr(row, "prompt", "") or ""),
            created_at=float(created.timestamp()),
        )


async def list_custom_style_profiles(*, user_id: int, limit: int = 25) -> list[CustomStyleProfile]:
    limit = max(1, min(int(limit or 25), 50))
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterCustomStyle)
            .where(CharacterCustomStyle.user_id == int(user_id))
            .order_by(CharacterCustomStyle.created_at.desc())
            .limit(limit)
        )
        rows = res.scalars().all() or []
        out: list[CustomStyleProfile] = []
        for row in rows:
            created = getattr(row, "created_at", None) or _now_utc()
            out.append(
                CustomStyleProfile(
                    user_id=int(user_id),
                    style_id=_norm(getattr(row, "style_id", "")),
                    name=(getattr(row, "name", "") or ""),
                    prompt=(getattr(row, "prompt", "") or ""),
                    created_at=float(created.timestamp()),
                )
            )
        return out


async def delete_custom_style_profile(*, user_id: int, style_id: str) -> bool:
    style_id = _norm(style_id)
    if not style_id:
        return False
    if style_id in {str(s).strip().lower() for s in (BASE_STYLE_IDS or [])}:
        return False
    if delete is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            delete(CharacterCustomStyle)
            .where(CharacterCustomStyle.user_id == int(user_id))
            .where(CharacterCustomStyle.style_id == style_id)
        )
        await session.commit()
        return bool(res.rowcount and int(res.rowcount) > 0)


# ---------------------------------------------------------------------------
# Compatibility shim: apply_pity_after_roll (kept in Redis, OK to lose)
# ---------------------------------------------------------------------------

async def _redis_get_json_best_effort(key: str) -> Dict[str, Any]:
    try:
        r = await get_redis_or_none()
        if r is None:
            return {}
        raw = await r.get(key)
        if not raw:
            return {}
        if isinstance(raw, (bytes, bytearray)):
            s = raw.decode("utf-8", errors="ignore").strip()
        else:
            s = str(raw).strip()
        if not s:
            return {}
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


async def _redis_set_json_best_effort(key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
    try:
        r = await get_redis_or_none()
        if r is None:
            return
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if hasattr(r, "setex"):
            await r.setex(key, ttl_seconds, data)
        else:
            await r.set(key, data)
            if hasattr(r, "expire"):
                await r.expire(key, ttl_seconds)
    except Exception:
        return


async def apply_pity_after_roll(
    *,
    guild_id: int,
    user_id: int,
    rolled_rarity: str,
    won_featured: Optional[bool] = None,
) -> Dict[str, Any]:
    key = _pity_key(guild_id, user_id)
    state = await _redis_get_json_best_effort(key)

    pity = int(state.get("pity", 0) or 0)
    guaranteed_next = bool(state.get("guaranteed_next", False))

    high_rarities = {"5", "5star", "5★", "legendary", "mythic", "ssr", "ur"}
    is_high = str(rolled_rarity or "").strip().lower() in high_rarities

    now = int(time.time())
    state["last_roll_ts"] = now

    if is_high:
        state["pity"] = 0
        if won_featured is False:
            state["guaranteed_next"] = True
        else:
            state["guaranteed_next"] = False
    else:
        pity += 1
        state["pity"] = pity
        if pity >= 60:
            state["guaranteed_next"] = True
        else:
            state["guaranteed_next"] = guaranteed_next

    await _redis_set_json_best_effort(key, state, _PITY_TTL_SECONDS)
    return state


# ---------------------------------------------------------------------------
# Compatibility helpers (used by commands/slash/character.py, etc.)
# ---------------------------------------------------------------------------

def compute_limits(*, is_pro: bool) -> tuple[int, int]:
    """Return (rolls_per_day, inventory_slots).

    Rolls/day are controlled elsewhere (can_roll / ROLLS_PER_DAY_*).
    The slot count is used for UI + inventory enforcement.

    You asked for a price-conscious cap:
      - Free: 3
      - Pro: 10
    Base characters (fun/serious) do NOT count toward the cap.
    """

    slots = 10 if is_pro else 3
    rolls = ROLLS_PER_DAY_PRO if is_pro else ROLLS_PER_DAY_FREE
    return rolls, slots


def _count_inventory_nonbase(style_ids: set[str] | list[str]) -> int:
    base = {str(s).strip().lower() for s in (BASE_STYLE_IDS or [])}
    return len({str(s).strip().lower() for s in (style_ids or []) if str(s).strip().lower() and str(s).strip().lower() not in base})


async def remove_style_from_inventory(*, user_id: int, style_id: str) -> tuple[bool, str, int]:
    """Remove a character from a user's inventory.

    - Refuses to remove base characters (fun/serious/etc.).
    - Works for BOTH registry-owned styles and custom profiles.
    - Safe if the style doesn't exist: returns (False, msg, 0).

    Returns (ok, message, old_streak) where old_streak is the character streak
    that was deleted (0 if none existed).
    """
    style_id = _norm(style_id)
    base = {str(s).strip().lower() for s in (BASE_STYLE_IDS or [])}
    if not style_id:
        return False, "Pick a character.", 0
    if style_id in base:
        return False, "That base character can’t be removed.", 0

    if not await owns_style(user_id, style_id):
        return False, "You don’t have that character.", 0

    if delete is None:
        raise RuntimeError("sqlalchemy not available")

    removed_any = False
    Session = get_sessionmaker()
    async with Session() as session:
        # Registry owned styles
        res1 = await session.execute(
            delete(CharacterOwnedStyle)
            .where(CharacterOwnedStyle.user_id == int(user_id))
            .where(CharacterOwnedStyle.style_id == style_id)
        )

        # Custom styles
        res2 = await session.execute(
            delete(CharacterCustomStyle)
            .where(CharacterCustomStyle.user_id == int(user_id))
            .where(CharacterCustomStyle.style_id == style_id)
        )
        await session.commit()

        if (getattr(res1, "rowcount", 0) or 0) > 0 or (getattr(res2, "rowcount", 0) or 0) > 0:
            removed_any = True

    # Clear active selection if it pointed at the removed style
    st = await load_state(user_id=user_id)
    if _norm(getattr(st, "active_style_id", "")) == style_id:
        await clear_active_style(user_id)

    # Clean up character streak data so the reminder loop doesn’t keep
    # sending "streak ended" DMs for a character the user no longer owns.
    old_streak = 0
    if removed_any:
        try:
            from utils.character_streak import delete_character_streak
            old_streak = await delete_character_streak(user_id=user_id, style_id=style_id)
        except Exception:
            pass  # best-effort

    return (True, "Removed from your collection.", old_streak) if removed_any else (False, "Nothing was removed.", 0)


async def add_style_to_inventory(
    *, user_id: int, style_id: str, is_pro: bool | None = None, guild_id: int | None = None
) -> tuple[bool, str]:
    style_id = _norm(style_id)
    if not style_id or style_id == "fun":
        return False, "Invalid character."

    is_registry = bool(get_style(style_id))
    if not is_registry:
        # Lazy-load custom/shop characters after restart.
        # Shop "packless" singles live in a hidden internal pack and may not be merged
        # into the in-memory registry at startup.
        try:
            target = normalize_style_id(style_id)
            packs = await list_custom_packs(limit=600, include_internal=True, include_shop_only=True)
            for p in packs or []:
                if not isinstance(p, dict):
                    continue
                chars = p.get("characters") or []
                if not isinstance(chars, list):
                    continue
                found = False
                for c in chars:
                    if not isinstance(c, dict):
                        continue
                    sid = normalize_style_id(str(c.get("id") or c.get("style_id") or ""))
                    if sid == target:
                        found = True
                        break
                if found:
                    try:
                        merge_pack_payload(p)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        if not get_style(style_id):
            return False, "Unknown character."

    if await owns_style(user_id, style_id):
        return False, "You already own this character."

    # Slot enforcement: base characters do NOT count.
    # If tier is unknown, default to FREE to stay conservative.
    st = await load_state(user_id)
    owned_now = set([_norm(x) for x in (st.owned_custom or [])])
    _rolls_cfg, slots = compute_limits(is_pro=bool(is_pro))
    # Apply paid inventory upgrades (+5 slots each).
    try:
        upgrades = int(await get_inventory_upgrades(user_id) or 0)
    except Exception:
        upgrades = 0
    slots = int(slots) + (upgrades * 5)
    if _count_inventory_nonbase(owned_now) >= int(slots):
        return False, "Your collection is full."

    await append_owned_style(user_id, style_id, guild_id=guild_id)
    return True, "Added to your collection."


async def award_dupe_shards(*, user_id: int, amount: int) -> None:
    await add_points(user_id, int(amount or 0))


async def replace_style_in_inventory(*, user_id: int, old_style_id: str, new_style_id: str) -> tuple[bool, str]:
    old_style_id = _norm(old_style_id)
    new_style_id = _norm(new_style_id)

    if not old_style_id:
        return False, "Pick a character to replace."
    if not new_style_id:
        return False, "Invalid new character."

    if not await owns_style(user_id, old_style_id):
        return False, "You don’t own that character."

    # Remove old from owned/custom
    Session = get_sessionmaker()
    async with Session() as session:
        if delete is None:
            raise RuntimeError("sqlalchemy not available")
        await session.execute(
            delete(CharacterOwnedStyle)
            .where(CharacterOwnedStyle.user_id == int(user_id))
            .where(CharacterOwnedStyle.style_id == old_style_id)
        )
        await session.execute(
            delete(CharacterCustomStyle)
            .where(CharacterCustomStyle.user_id == int(user_id))
            .where(CharacterCustomStyle.style_id == old_style_id)
        )
        await session.commit()

    # Add new
    if get_style(new_style_id):
        await append_owned_style(user_id, new_style_id)
    else:
        # cannot replace with non-registry in this fast migration
        return False, "Unknown new character."

    # Clear active if needed
    st = await load_state(user_id=user_id)
    if _norm(getattr(st, "active_style_id", "")) == old_style_id:
        await clear_active_style(user_id)

    # Clean up character streak for the old character
    try:
        from utils.character_streak import delete_character_streak
        await delete_character_streak(user_id=user_id, style_id=old_style_id)
    except Exception:
        pass  # best-effort

    return True, "Replaced character successfully."


async def give_style_to_user(user_id: int, style_id: str) -> tuple[bool, str]:
    """Owner utility: grant a style to a user by ID."""
    sid = _norm_style_id(style_id)
    if not sid:
        return False, "Missing character id"
    if not get_style(sid):
        return False, f"Unknown character id: {sid}"

    await append_owned_style(user_id, sid)
    # Force state refresh so it shows up immediately.
    try:
        await refresh_user_state(user_id)
    except Exception:
        pass
    return True, f"Granted {sid}"



async def nuke_style_globally(style_id: str) -> tuple[bool, str]:
    """Remove a custom character everywhere it might exist.

    This is used by owner /character remove and similar admin tools.

    Current storage model:
    - Custom packs live in Redis via utils.packs_store (PACK:* keys) and contain
      a list of character dicts under payload["characters"].
    - There is no longer a SQLAlchemy CustomPack table, so we must edit the Redis
      payloads directly.

    Returns:
        (ok, message)
    """
    style_id = normalize_style_id(style_id)
    if not style_id:
        return False, "Invalid character id."

    # 1) Remove the character JSON file from disk if present.
    try:
        p = character_file_path(style_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass

    # 2) Remove it from the style registry (if it exists there).
    try:
        remove_style_from_registry(style_id)
    except Exception:
        pass

    # 3) Remove it from all custom packs stored in Redis.
    removed_from = 0
    try:
        from utils import packs_store  # local import to avoid circular deps

        packs = await packs_store.list_custom_packs()
        for pack in packs or []:
            if not isinstance(pack, dict):
                continue
            pid = str(pack.get("pack_id") or "").strip()
            if not pid:
                continue
            chars = pack.get("characters") or []
            if not isinstance(chars, list) or not chars:
                continue
            new_chars = []
            changed = False
            for c in chars:
                if isinstance(c, dict) and normalize_style_id(str(c.get("id") or c.get("style_id") or "")) == style_id:
                    changed = True
                    continue
                new_chars.append(c)
            if changed:
                pack["characters"] = new_chars
                ok = await packs_store.upsert_custom_pack(pack)
                if ok:
                    removed_from += 1
    except Exception:
        # best-effort: if Redis is down, we still removed file/registry
        pass

    return True, f"Removed. Updated {removed_from} pack(s)."

async def get_inventory_upgrades(user_id: int) -> int:
    """Number of permanent inventory upgrades purchased (each is +5 slots)."""
    Session = get_sessionmaker()
    async with Session() as session:
        st = await session.get(CharacterUserState, int(user_id))
        if not st:
            return 0
        return int(getattr(st, "inventory_upgrades", 0) or 0)


async def increment_inventory_upgrades(user_id: int, *, delta: int = 1) -> int:
    """Increment inventory upgrades and return new total."""
    Session = get_sessionmaker()
    async with Session() as session:
        st = await session.get(CharacterUserState, int(user_id), with_for_update=True)
        if not st:
            # create a default row
            st = CharacterUserState(user_id=int(user_id))
            session.add(st)
            await session.flush()
        cur = int(getattr(st, "inventory_upgrades", 0) or 0)
        cur = max(0, cur + int(delta))
        st.inventory_upgrades = cur
        await session.commit()
        return cur


