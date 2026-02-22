from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from utils.db import get_sessionmaker
from utils.analytics import utc_day_str

from utils.models import PointsWallet, PointsLedger

# Global guild ID for global daily streaks
GLOBAL_GUILD_ID = 0

# Lazy import to avoid circular dependencies
_leaderboard_update = None

async def _update_points_leaderboard(guild_id: int, user_id: int, balance: int) -> None:
    """Update points leaderboard (global always; also server when guild_id is a real guild)."""
    try:
        from utils.leaderboard import update_all_periods, CATEGORY_POINTS, GLOBAL_GUILD_ID as GLB_GID
        await update_all_periods(
            category=CATEGORY_POINTS,
            guild_id=GLB_GID,
            user_id=user_id,
            value=float(balance),
        )
        if guild_id and int(guild_id) != GLB_GID:
            await update_all_periods(
                category=CATEGORY_POINTS,
                guild_id=int(guild_id),
                user_id=user_id,
                value=float(balance),
            )
    except Exception:
        pass

try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore

logger = logging.getLogger("bot.points")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _day_utc(dt: datetime | None = None) -> str:
    ts = int((dt or _now_utc()).timestamp())
    return utc_day_str(ts)


@dataclass(frozen=True)
class DailyResult:
    awarded: int
    balance: int
    streak: int
    claimed_today: bool
    next_claim_in_seconds: int
    first_bonus_awarded: int

    # Streak-restore offer (only present when the user broke their streak recently)
    restore_available: bool = False
    restore_cost: int = 500
    restore_to_streak: int = 0
    restore_deadline_day_utc: str = ""


def _daily_amount_for_streak(streak: int) -> int:
    """Economy knobs.

    Base + streak bonus (capped) + small milestone bumps.
    Tuned for a slow-but-steady free progression.
    """
    base = 30
    bonus = min(max(0, int(streak) - 1), 10) * 2  # +2/day up to +20
    milestone = 0
    if streak >= 7:
        milestone += 20
    if streak >= 10:
        milestone += 30
    return base + bonus + milestone


async def _get_or_create_wallet(*, guild_id: int, user_id: int) -> PointsWallet:
    """Get or create wallet. Points are now GLOBAL (guild_id=0)."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    # Points are global - always use GLOBAL_GUILD_ID
    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(PointsWallet)
            .where(PointsWallet.guild_id == GLOBAL_GUILD_ID)
            .where(PointsWallet.user_id == int(user_id))
            .limit(1)
        )
        row = res.scalar_one_or_none()
        if row is not None:
            return row

        row = PointsWallet(guild_id=GLOBAL_GUILD_ID, user_id=int(user_id))
        session.add(row)
        await session.commit()
        return row


async def get_balance(*, guild_id: int, user_id: int) -> int:
    """Get points balance. guild_id is ignored (points are global)."""
    w = await _get_or_create_wallet(guild_id=GLOBAL_GUILD_ID, user_id=user_id)
    return int(getattr(w, "balance", 0) or 0)


async def get_claim_status(*, guild_id: int, user_id: int) -> tuple[bool, int, int]:
    """Return (claimed_today, seconds_until_next_claim, streak).
    
    Uses global wallet for streak tracking.
    """
    # Use global wallet for streak
    w = await _get_or_create_wallet(guild_id=GLOBAL_GUILD_ID, user_id=user_id)
    today = _day_utc()
    claimed_today = (w.last_claim_day_utc or "") == today
    if not claimed_today:
        return False, 0, int(w.streak or 0)

    # Next claim is at next UTC midnight
    now = _now_utc()
    next_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return True, max(0, int((next_midnight - now).total_seconds())), int(w.streak or 0)


async def claim_daily(*, guild_id: int, user_id: int) -> DailyResult:
    """Claim daily points.

    Rules:
    - Points and streaks are GLOBAL (guild_id=0)
    - guild_id parameter is ignored (for backward compatibility)
    - Streak increases by 1 if claimed on consecutive days, else resets to 1
    - One-time first-claim bonus
    - Append ledger row for audit
    """
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    Session = get_sessionmaker()
    async with Session() as session:
        # Get global wallet (points and streaks are global)
        res = await session.execute(
            select(PointsWallet)
            .where(PointsWallet.guild_id == GLOBAL_GUILD_ID)
            .where(PointsWallet.user_id == int(user_id))
            .with_for_update()
            .limit(1)
        )
        w = res.scalar_one_or_none()
        if w is None:
            w = PointsWallet(guild_id=GLOBAL_GUILD_ID, user_id=int(user_id))
            session.add(w)
            await session.flush()

        today = _day_utc()
        now = _now_utc()

        # Already claimed today -- return early, no double-claim.
        if (w.last_claim_day_utc or "") == today:
            next_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
            # Check restore availability for the response
            restore_available = False
            restore_to_streak = 0
            restore_deadline = (getattr(w, "streak_restore_deadline_day_utc", "") or "").strip()
            saved = int(getattr(w, "streak_saved", 0) or 0)
            if saved > 0 and restore_deadline and today <= restore_deadline:
                restore_available = True
                restore_to_streak = saved + 1
            return DailyResult(
                awarded=0,
                balance=int(w.balance or 0),
                streak=int(w.streak or 0),
                claimed_today=True,
                next_claim_in_seconds=max(0, int((next_midnight - now).total_seconds())),
                first_bonus_awarded=0,
                restore_available=bool(restore_available),
                restore_cost=500,
                restore_to_streak=int(restore_to_streak),
                restore_deadline_day_utc=str(restore_deadline or ""),
            )

        # Streak logic
        prev = (w.last_claim_day_utc or "").strip()
        if prev:
            try:
                prev_dt = datetime.strptime(prev, "%Y%m%d").replace(tzinfo=timezone.utc)
                prev_expected = _day_utc(prev_dt + timedelta(days=1))
                if prev_expected == today:
                    w.streak = int(w.streak or 0) + 1
                    # Streak is continuing normally — clear any leftover
                    # restore offer from a prior break so the restore button
                    # doesn't appear when the streak is alive.
                    w.streak_saved = 0
                    w.streak_restore_deadline_day_utc = ""
                else:
                    # Streak broken: save previous streak for a limited time so the
                    # user can pay to restore it.
                    try:
                        prev_streak = int(w.streak or 0)
                        if prev_streak > 0:
                            w.streak_saved = prev_streak
                            deadline_dt = datetime.strptime(today, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=7)
                            w.streak_restore_deadline_day_utc = _day_utc(deadline_dt)
                    except Exception:
                        pass
                    w.streak = 1
            except Exception:
                w.streak = 1
        else:
            w.streak = 1

        awarded = _daily_amount_for_streak(int(w.streak or 1))

        # One-time first claim bonus
        first_bonus = 0
        if not bool(getattr(w, "first_claimed", False)):
            first_bonus = 100
            w.first_claimed = True
            awarded += first_bonus

        w.balance = int(w.balance or 0) + int(awarded)
        w.last_claim_day_utc = today
        w.updated_at = now
        
        # Update leaderboard for points and streak (global + server when in a guild)
        await _update_points_leaderboard(guild_id, user_id, int(w.balance))
        try:
            from utils.leaderboard import update_all_periods, CATEGORY_STREAK, GLOBAL_GUILD_ID as GLB_GID
            await update_all_periods(
                category=CATEGORY_STREAK,
                guild_id=GLB_GID,
                user_id=user_id,
                value=float(w.streak),
            )
            if guild_id and int(guild_id) != GLB_GID:
                await update_all_periods(
                    category=CATEGORY_STREAK,
                    guild_id=int(guild_id),
                    user_id=user_id,
                    value=float(w.streak),
                )
        except Exception:
            pass

        # Ledger (record in global ledger)
        session.add(
            PointsLedger(
                guild_id=GLOBAL_GUILD_ID,  # Points are global
                user_id=int(user_id),
                delta=int(awarded),
                reason="daily_claim",
                meta_json=json.dumps(
                    {
                        "day_utc": today,
                        "streak": int(w.streak or 0),
                        "first_bonus": int(first_bonus),
                    },
                    separators=(",", ":"),
                ),
            )
        )

        await session.commit()

        next_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)

        # Streak restore offer (if the streak was broken recently)
        restore_available = False
        restore_to_streak = 0
        restore_deadline = (getattr(w, "streak_restore_deadline_day_utc", "") or "").strip()
        saved = int(getattr(w, "streak_saved", 0) or 0)
        if saved > 0 and restore_deadline and today <= restore_deadline:
            # If they claimed today after a break, they can pay to continue the old streak.
            restore_available = True
            restore_to_streak = saved + 1
        return DailyResult(
            awarded=int(awarded),
            balance=int(w.balance or 0),  # Global balance
            streak=int(w.streak or 0),  # Global streak
            claimed_today=True,
            next_claim_in_seconds=max(0, int((next_midnight - now).total_seconds())),
            first_bonus_awarded=int(first_bonus),
            restore_available=bool(restore_available),
            restore_cost=500,
            restore_to_streak=int(restore_to_streak),
            restore_deadline_day_utc=str(restore_deadline or ""),
        )


async def spend_points(*, guild_id: int, user_id: int, cost: int, reason: str, meta: dict | None = None) -> tuple[bool, int]:
    """Attempt to spend points. guild_id is ignored (points are global). Returns (ok, new_balance)."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")
    cost = max(0, int(cost or 0))
    if cost <= 0:
        return True, await get_balance(guild_id=GLOBAL_GUILD_ID, user_id=user_id)

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(PointsWallet)
            .where(PointsWallet.guild_id == GLOBAL_GUILD_ID)
            .where(PointsWallet.user_id == int(user_id))
            .with_for_update()
            .limit(1)
        )
        w = res.scalar_one_or_none()
        if w is None:
            w = PointsWallet(guild_id=GLOBAL_GUILD_ID, user_id=int(user_id))
            session.add(w)
            await session.flush()

        bal = int(w.balance or 0)
        if bal < cost:
            return False, bal

        w.balance = bal - cost
        w.updated_at = _now_utc()
        session.add(
            PointsLedger(
                guild_id=GLOBAL_GUILD_ID,  # Points are global
                user_id=int(user_id),
                delta=-int(cost),
                reason=(reason or "spend"),
                meta_json=json.dumps(meta or {}, separators=(",", ":")),
            )
        )
        await session.commit()
        # Update leaderboard (global + server)
        await _update_points_leaderboard(guild_id, user_id, int(w.balance))
        return True, int(w.balance or 0)


async def adjust_points(*, guild_id: int, user_id: int, delta: int, reason: str, meta: dict | None = None) -> int:
    """Owner/admin helper: add or remove points. guild_id is ignored (points are global).

    Returns the new balance.
    """
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    uid = int(user_id)
    d = int(delta or 0)
    if d == 0:
        return await get_balance(guild_id=GLOBAL_GUILD_ID, user_id=uid)

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(PointsWallet)
            .where(PointsWallet.guild_id == GLOBAL_GUILD_ID)
            .where(PointsWallet.user_id == uid)
            .with_for_update()
            .limit(1)
        )
        w = res.scalar_one_or_none()
        if w is None:
            w = PointsWallet(guild_id=GLOBAL_GUILD_ID, user_id=uid)
            session.add(w)
            await session.flush()

        bal = int(w.balance or 0)
        new_bal = bal + d
        if new_bal < 0:
            new_bal = 0

        w.balance = new_bal
        w.updated_at = _now_utc()
        session.add(
            PointsLedger(
                guild_id=GLOBAL_GUILD_ID,
                user_id=uid,
                delta=int(new_bal - bal),
                reason=(reason or "adjust"),
                meta_json=json.dumps(meta or {}, separators=(",", ":")),
            )
        )
        await session.commit()
        return int(w.balance or 0)



def _parse_booster_kind(kind: str | None) -> tuple[str | None, int]:
    """Parse booster kind encoding without DB migrations.
    Example: 'lucky@3' -> ('lucky', 3)
    """
    if not kind:
        return None, 0
    if "@" in kind:
        base, _, tail = kind.partition("@")
        try:
            n = int(tail)
        except ValueError:
            n = 1
        return base, max(1, n)
    return kind, 1

def _format_booster_kind(base: str, stacks: int) -> str:
    stacks = max(1, int(stacks))
    return f"{base}@{stacks}" if stacks > 1 else base
async def set_booster(
    *,
    guild_id: int,
    user_id: int,
    kind: str,
    duration_s: int,
    stack: bool = False,
    extend_expiry: bool = True,
) -> None:
    """Set (or optionally stack) an active booster for a user.

    If stack=True and the existing booster is the same base kind and is active,
    we increment stacks and extend expiry by duration_s.

    NOTE: Some earlier iterations referenced SessionLocal/get_or_create_wallet.
    This implementation is self-contained and matches the rest of points_store.py,
    which uses utils.db.get_sessionmaker().
    """
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    now = _now_utc()
    exp = now + timedelta(seconds=int(duration_s))

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(PointsWallet)
            .where(PointsWallet.guild_id == int(guild_id))
            .where(PointsWallet.user_id == int(user_id))
            .limit(1)
        )
        w = res.scalar_one_or_none()
        if w is None:
            w = PointsWallet(guild_id=int(guild_id), user_id=int(user_id))
            session.add(w)
            await session.commit()

        if stack and getattr(w, "booster_kind", None) and getattr(w, "booster_expires_at", None):
            try:
                if w.booster_expires_at and w.booster_expires_at > now:
                    base, stacks = _parse_booster_kind(w.booster_kind)
                    new_base, _ = _parse_booster_kind(kind)
                    if base and base == new_base and stack:
                        stacks = max(1, stacks) + 1
                        # By default stacking extends expiry. Some boosters want "fixed
                        # expiry" stacks (all stacks reset when the first purchase expires).
                        if extend_expiry:
                            exp = max(w.booster_expires_at, now) + timedelta(seconds=int(duration_s))
                        else:
                            exp = w.booster_expires_at
                        w.booster_kind = _format_booster_kind(base, stacks)
                        w.booster_expires_at = exp
                        await session.commit()
                        return
            except Exception:
                # fall through to overwrite
                pass

        w.booster_kind = kind
        w.booster_expires_at = exp
        await session.commit()

async def get_active_booster(*, guild_id: int, user_id: int) -> tuple[str, datetime | None]:
    w = await _get_or_create_wallet(guild_id=guild_id, user_id=user_id)
    kind = (getattr(w, "booster_kind", "") or "").strip().lower()
    exp = getattr(w, "booster_expires_at", None)
    if not kind or not exp:
        return "", None
    try:
        if exp <= _now_utc():
            return "", None
    except Exception:
        return "", None
    base, stacks = _parse_booster_kind(kind)
    return (base or ''), exp

async def get_booster_stack(*, guild_id: int, user_id: int, kind: str) -> tuple[int, datetime | None]:
    """Return (stacks, expires_at) for the given booster base kind."""
    w = await _get_or_create_wallet(guild_id=guild_id, user_id=user_id)
    raw = (getattr(w, "booster_kind", "") or "").strip().lower()
    exp = getattr(w, "booster_expires_at", None)
    base, stacks = _parse_booster_kind(raw)
    if not base or base != kind.strip().lower() or not exp:
        return 0, None
    try:
        if exp <= _now_utc():
            return 0, None
    except Exception:
        return 0, None
    return stacks, exp




def build_roadmap_preview(*, current_streak: int, days: int = 10) -> list[int]:
    """Return awarded amounts for the next N days if the user keeps the streak."""
    s = max(1, int(current_streak or 1))
    out: list[int] = []
    for i in range(days):
        out.append(_daily_amount_for_streak(s + i))
    return out


async def restore_daily_streak(*, guild_id: int, user_id: int, cost: int = 500) -> tuple[bool, str, int, int]:
    """Pay points to restore a broken daily streak (within the saved window).

    Returns: (ok, message, new_balance, new_streak)
    """
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    cost = int(cost or 0)
    if cost <= 0:
        cost = 500

    today = _day_utc()
    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(PointsWallet)
            .where(PointsWallet.guild_id == GLOBAL_GUILD_ID)
            .where(PointsWallet.user_id == int(user_id))
            .with_for_update()
            .limit(1)
        )
        w = res.scalar_one_or_none()
        if w is None:
            return False, "You don't have an active streak to restore.", 0, 0

        deadline = (getattr(w, "streak_restore_deadline_day_utc", "") or "").strip()
        saved = int(getattr(w, "streak_saved", 0) or 0)
        if saved <= 0 or not deadline or today > deadline:
            return False, "No restore is available right now.", int(w.balance or 0), int(w.streak or 0)

        bal = int(w.balance or 0)
        if bal < cost:
            return False, f"You need {cost} points to restore your streak.", bal, int(w.streak or 0)

        # Apply restore: continue the previous streak as if today counted.
        target_streak = saved + 1
        w.balance = bal - cost
        w.streak = max(int(w.streak or 0), target_streak)

        # Clear offer
        w.streak_saved = 0
        w.streak_restore_deadline_day_utc = ""

        # Ledger for audit
        session.add(
            PointsLedger(
                guild_id=GLOBAL_GUILD_ID,
                user_id=int(user_id),
                delta=-cost,
                reason="streak_restore",
                meta_json=json.dumps({"restored_to": int(w.streak or 0)}, separators=(",", ":")),
            )
        )

        await session.commit()
        return True, "✅ Streak restored!", int(w.balance or 0), int(w.streak or 0)


async def is_streak_alive(user_id: int) -> bool:
    """True if the user's daily streak is still alive (last claim was today or yesterday UTC)."""
    if select is None:
        return False
    w = await _get_or_create_wallet(guild_id=GLOBAL_GUILD_ID, user_id=user_id)
    last = (getattr(w, "last_claim_day_utc", "") or "").strip()
    if not last:
        return False
    today = _day_utc()
    if last == today:
        return True
    try:
        yesterday = _day_utc(_now_utc() - timedelta(days=1))
        return last == yesterday
    except Exception:
        return False


async def get_last_claim_day(user_id: int) -> str:
    """Return last_claim_day_utc for a user (empty string if never claimed)."""
    if select is None:
        return ""
    w = await _get_or_create_wallet(guild_id=GLOBAL_GUILD_ID, user_id=user_id)
    return (getattr(w, "last_claim_day_utc", "") or "").strip()


async def get_eligible_reminder_user_ids(*, limit: int = 50000) -> list[int]:
    """User IDs whose daily streak is alive or just broke today (last_claim = today or yesterday).

    This prevents reminders from being sent to users whose streak has been broken for days.
    """
    if select is None:
        return []
    from sqlalchemy import or_
    today = _day_utc()
    yesterday = _day_utc(_now_utc() - timedelta(days=1))
    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(PointsWallet.user_id)
            .where(PointsWallet.guild_id == GLOBAL_GUILD_ID)
            .where(
                or_(
                    PointsWallet.last_claim_day_utc == today,
                    PointsWallet.last_claim_day_utc == yesterday,
                )
            )
            .distinct()
            .limit(limit)
        )
        rows = res.scalars().all()
        return [int(r) for r in rows]
