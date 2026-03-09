from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from utils.backpressure import get_redis_or_none
from utils.db import get_sessionmaker
from utils.models import BondState

try:
    from sqlalchemy import delete, select  # type: ignore
except Exception:  # pragma: no cover
    delete = None  # type: ignore
    select = None  # type: ignore
from utils.bonds import DAILY_XP_CAP_PER_CHARACTER


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# Bonds are now global (guild_id=0 for all bonds)
GLOBAL_GUILD_ID = 0

def _bond_key(guild_id: int, user_id: int, style_id: str) -> str:
    # Always use global for bonds
    return f"bond:{GLOBAL_GUILD_ID}:{int(user_id)}:{style_id}"


def _bond_day_key(guild_id: int, user_id: int, style_id: str) -> str:
    # Always use global for bonds
    return f"bond:xp:day:{GLOBAL_GUILD_ID}:{int(user_id)}:{style_id}:{_day_key()}"


def _j(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _unj(s: str | bytes | None, default=None):
    if s is None:
        return default
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("utf-8", errors="ignore")
    try:
        return json.loads(s)
    except Exception:
        return default


@dataclass
class Bond:
    guild_id: int  # Always GLOBAL_GUILD_ID (0) for global bonds
    user_id: int
    style_id: str
    xp: int = 0
    nickname: str | None = None
    updated_at: float | None = None


async def get_bond(*, guild_id: int, user_id: int, style_id: str) -> Bond | None:
    """Get bond state. guild_id is ignored (bonds are global)."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        # Bonds are global - use GLOBAL_GUILD_ID
        res = await session.execute(
            select(BondState).where(
                BondState.guild_id == GLOBAL_GUILD_ID,
                BondState.user_id == int(user_id),
                BondState.style_id == str(style_id),
            )
        )
        row = res.scalar_one_or_none()
        if row is None:
            return None
        updated = getattr(row, "updated_at", None)
        return Bond(
            guild_id=GLOBAL_GUILD_ID,
            user_id=int(user_id),
            style_id=str(style_id),
            xp=int(getattr(row, "xp", 0) or 0),
            nickname=getattr(row, "nickname", None),
            updated_at=float(updated.timestamp()) if updated else 0.0,
        )


async def list_bonds_for_user(user_id: int) -> list[Bond]:
    """List all bond states for a user (bonds are global)."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")
    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(BondState).where(
                BondState.guild_id == GLOBAL_GUILD_ID,
                BondState.user_id == int(user_id),
            )
        )
        rows = res.scalars().all()
        out = []
        for row in rows:
            updated = getattr(row, "updated_at", None)
            out.append(
                Bond(
                    guild_id=GLOBAL_GUILD_ID,
                    user_id=int(user_id),
                    style_id=str(getattr(row, "style_id", "")),
                    xp=int(getattr(row, "xp", 0) or 0),
                    nickname=getattr(row, "nickname", None),
                    updated_at=float(updated.timestamp()) if updated else 0.0,
                )
            )
        return out


async def _save_bond(b: Bond) -> None:
    """Save bond state. Bonds are global (guild_id=0)."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")
    Session = get_sessionmaker()
    async with Session() as session:
        # Bonds are global - always use GLOBAL_GUILD_ID
        res = await session.execute(
            select(BondState).where(
                BondState.guild_id == GLOBAL_GUILD_ID,
                BondState.user_id == int(b.user_id),
                BondState.style_id == str(b.style_id),
            )
        )
        row = res.scalar_one_or_none()
        now_dt = datetime.fromtimestamp(float(b.updated_at or _now_ts()), tz=timezone.utc)
        if row is None:
            row = BondState(
                guild_id=GLOBAL_GUILD_ID,
                user_id=int(b.user_id),
                style_id=str(b.style_id),
                xp=int(b.xp or 0),
                nickname=b.nickname,
                updated_at=now_dt,
            )
            session.add(row)
        else:
            row.xp = int(b.xp or 0)
            row.nickname = b.nickname
            row.updated_at = now_dt
        await session.commit()


async def add_bond_xp(*, guild_id: int, user_id: int, style_id: str, amount: int) -> tuple[int, int, bool]:
    """Add bond XP. guild_id is ignored (bonds are global).
    
    Returns: (new_total_xp, xp_today, capped_hit)."""
    amount = max(0, int(amount or 0))
    if amount == 0:
        b = await get_bond(guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id)
        return (int(b.xp) if b else 0, 0, False)

    r = await get_redis_or_none()

    # If Redis is down/misconfigured, degrade gracefully:
    # - still award XP (durable in Postgres)
    # - skip the *daily* XP cap counter (Redis-backed)
    # This keeps the bot responsive instead of crash-looping.
    if r is None:
        b = await get_bond(guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id) or Bond(
            guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id, xp=0
        )
        applied = int(amount)
        # Best-effort max bond cap even when Redis is unavailable.
        try:
            from utils.character_registry import get_style
            from utils.bonds import next_level_xp

            s = get_style(str(style_id or "").lower())
            max_level = int(getattr(s, "max_bond_level", 0) or 0) if s is not None else 0
            if max_level > 0:
                max_xp = int(next_level_xp(int(max_level) + 1) - 1)
                cur_xp = int(b.xp or 0)
                applied = max(0, min(applied, max_xp - cur_xp))
        except Exception:
            pass

        b.xp = int(b.xp or 0) + int(applied)
        b.updated_at = _now_ts()
        await _save_bond(b)
        return int(b.xp), 0, False

    day_key = _bond_day_key(GLOBAL_GUILD_ID, user_id, style_id)
    # Increment today's xp counter, with a 2-day TTL (covers timezone edge cases)
    xp_today = int(await r.incrby(day_key, amount))
    await r.expire(day_key, 2 * 24 * 3600)

    capped_hit = xp_today > int(DAILY_XP_CAP_PER_CHARACTER)
    # If we exceeded the cap, only apply the remaining allowed amount.
    allowed_left = max(0, int(DAILY_XP_CAP_PER_CHARACTER) - (xp_today - amount))
    applied = min(amount, allowed_left)

    b = await get_bond(guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id) or Bond(
        guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id, xp=0
    )

    # Optional per-character max bond cap (stored on the character definition).
    try:
        from utils.character_registry import get_style
        from utils.bonds import next_level_xp

        s = get_style(str(style_id or "").lower())
        max_level = int(getattr(s, "max_bond_level", 0) or 0) if s is not None else 0
        if max_level > 0:
            # Max XP is just before the next level starts.
            max_xp = int(next_level_xp(int(max_level) + 1) - 1)
            cur_xp = int(b.xp or 0)
            if cur_xp >= max_xp:
                applied = 0
                capped_hit = True
            else:
                allowed = max(0, max_xp - cur_xp)
                if int(applied) > int(allowed):
                    applied = int(allowed)
                    capped_hit = True
    except Exception:
        pass

    b.xp = int(b.xp or 0) + int(applied)
    b.updated_at = _now_ts()
    await _save_bond(b)

    return int(b.xp), int(min(xp_today, DAILY_XP_CAP_PER_CHARACTER)), bool(capped_hit)


async def upsert_bond_nickname(*, guild_id: int, user_id: int, style_id: str, nickname: str | None) -> None:
    """Update bond nickname. guild_id is ignored (bonds are global)."""
    b = await get_bond(guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id) or Bond(
        guild_id=GLOBAL_GUILD_ID, user_id=user_id, style_id=style_id, xp=0
    )
    b.nickname = (nickname or "").strip() or None
    b.updated_at = _now_ts()
    await _save_bond(b)
