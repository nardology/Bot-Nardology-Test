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


async def update_points_leaderboard(guild_id: int, user_id: int, balance: int) -> None:
    """Public helper to refresh points leaderboard after balance changes (e.g. quest claims)."""
    await _update_points_leaderboard(guild_id, user_id, balance)

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


# Engagement streak milestone constants
STREAK_7_BONUS_POINTS = 500
STREAK_30_BONUS_POINTS = 2000
RANDOM_BONUS_BASE_CHANCE_PCT = 3
RANDOM_BONUS_INCREMENT_PCT = 5
RANDOM_BONUS_POINTS = 500
COMEBACK_BONUS_POINTS = 100  # when streak breaks after 14+ days
COMEBACK_BONUS_MIN_STREAK = 14
WEEKLY_ACTIVITY_BONUS_POINTS = 50


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

    # Engagement milestones (set when claimed today)
    milestone_7_awarded: int = 0
    milestone_30_awarded: int = 0
    random_bonus_awarded: int = 0
    random_bonus_chance_pct: int = 0
    random_bonus_near_miss: bool = False  # True when we rolled and they didn't win
    random_bonus_next_chance_pct: int = 0  # chance next claim (for near-miss message)
    streak_75_triggered: bool = False
    character_reward_available: tuple[int, ...] = ()  # e.g. (10,) or (10, 15, 25) for unclaimed tiers
    comeback_awarded: int = 0  # points for returning after a long broken streak
    weekly_activity_awarded: int = 0


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

    Uses global wallet for streak tracking. Daily reset is at midnight UTC (global clock), not 24h from last claim.
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
    - Daily and streak reset at midnight UTC (global clock), not 24h from last claim
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
            # Character reward availability (for already-claimed view)
            _str = int(w.streak or 0)
            _avail: list[int] = []
            if _str >= 10 and not bool(getattr(w, "streak_10_character_claimed", False)):
                _avail.append(10)
            if _str >= 15 and not bool(getattr(w, "streak_15_character_claimed", False)):
                _avail.append(15)
            if _str >= 25 and not bool(getattr(w, "streak_25_character_claimed", False)):
                _avail.append(25)
            return DailyResult(
                awarded=0,
                balance=int(w.balance or 0),
                streak=_str,
                claimed_today=True,
                next_claim_in_seconds=max(0, int((next_midnight - now).total_seconds())),
                first_bonus_awarded=0,
                restore_available=bool(restore_available),
                restore_cost=500,
                restore_to_streak=int(restore_to_streak),
                restore_deadline_day_utc=str(restore_deadline or ""),
                character_reward_available=tuple(_avail),
            )

        # Streak logic
        import random as _rnd
        prev = (w.last_claim_day_utc or "").strip()
        if prev:
            try:
                prev_dt = datetime.strptime(prev, "%Y%m%d").replace(tzinfo=timezone.utc)
                prev_expected = _day_utc(prev_dt + timedelta(days=1))
                if prev_expected == today:
                    w.streak = int(w.streak or 0) + 1
                    w.streak_saved = 0
                    w.streak_restore_deadline_day_utc = ""
                else:
                    try:
                        prev_streak = int(w.streak or 0)
                        if prev_streak > 0:
                            w.streak_saved = prev_streak
                            deadline_dt = datetime.strptime(today, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=7)
                            w.streak_restore_deadline_day_utc = _day_utc(deadline_dt)
                    except Exception:
                        pass
                    w.streak = 1
                    # Reset engagement flags so they can earn milestones again
                    w.streak_7_bonus_given = False
                    w.streak_10_character_claimed = False
                    w.streak_15_character_claimed = False
                    w.streak_25_character_claimed = False
            except Exception:
                w.streak = 1
        else:
            w.streak = 1

        streak_val = int(w.streak or 1)
        awarded = _daily_amount_for_streak(streak_val)
        milestone_7_awarded = 0
        milestone_30_awarded = 0
        random_bonus_awarded = 0
        random_bonus_chance_pct = 0
        random_bonus_near_miss = False
        random_bonus_next_chance_pct = 0
        streak_75_triggered = False
        comeback_awarded = 0
        weekly_activity_awarded = 0

        # Comeback bonus: just broke a long streak (14+), welcome back
        if streak_val == 1 and int(w.streak_saved or 0) >= COMEBACK_BONUS_MIN_STREAK:
            comeback_awarded = COMEBACK_BONUS_POINTS
            awarded += comeback_awarded

        # One-time first claim bonus
        first_bonus = 0
        if not bool(getattr(w, "first_claimed", False)):
            first_bonus = 100
            w.first_claimed = True
            awarded += first_bonus

        # Day 7: extra 500 points (once per streak run)
        if streak_val == 7 and not bool(getattr(w, "streak_7_bonus_given", False)):
            milestone_7_awarded = STREAK_7_BONUS_POINTS
            awarded += milestone_7_awarded
            w.streak_7_bonus_given = True

        # Every 30 days: extra 2000 points
        last_30 = int(getattr(w, "streak_last_30_bonus_at", 0) or 0)
        if streak_val >= 30 and streak_val % 30 == 0 and streak_val > last_30:
            milestone_30_awarded = STREAK_30_BONUS_POINTS
            awarded += milestone_30_awarded
            w.streak_last_30_bonus_at = streak_val

        # Random daily bonus: 3% base + 5% per consecutive day, resets when won
        rnd_last = (getattr(w, "random_bonus_last_reward_day_utc", "") or "").strip()
        if rnd_last != today:
            rnd_days = int(getattr(w, "random_bonus_consecutive_days", 0) or 0)
            random_bonus_chance_pct = min(100, RANDOM_BONUS_BASE_CHANCE_PCT + rnd_days * RANDOM_BONUS_INCREMENT_PCT)
            if random_bonus_chance_pct >= 100 or _rnd.randint(1, 100) <= random_bonus_chance_pct:
                random_bonus_awarded = RANDOM_BONUS_POINTS
                awarded += random_bonus_awarded
                w.random_bonus_last_reward_day_utc = today
                w.random_bonus_consecutive_days = 0
            else:
                w.random_bonus_consecutive_days = rnd_days + 1
                random_bonus_near_miss = True
                random_bonus_next_chance_pct = min(100, RANDOM_BONUS_BASE_CHANCE_PCT + (rnd_days + 1) * RANDOM_BONUS_INCREMENT_PCT)

        # Streak badges (30/60/90) for /inspect profile
        if streak_val >= 30:
            w.streak_badge_30 = True
        if streak_val >= 60:
            w.streak_badge_60 = True
        if streak_val >= 90:
            w.streak_badge_90 = True

        # Weekly activity bonus: claimed daily + had at least one talk/roll day this week (before today)
        try:
            from utils.analytics import count_user_activity_days
            _d = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            _monday = _d - timedelta(days=_d.weekday())
            monday_utc = _monday.strftime("%Y%m%d")
            week_days: list[str] = []
            _cur = _monday
            while _cur.date() <= now.date():
                week_days.append(_cur.strftime("%Y%m%d"))
                _cur = _cur + timedelta(days=1)
            # Exclude today so we require at least one talk/roll on a previous day this week
            week_days = [d for d in week_days if d != today]
            last_week_bonus = (getattr(w, "weekly_activity_bonus_week_utc", "") or "").strip()
            if week_days and last_week_bonus != monday_utc:
                n = await count_user_activity_days(user_id=int(user_id), day_utc_list=week_days)
                if n >= 1:
                    weekly_activity_awarded = WEEKLY_ACTIVITY_BONUS_POINTS
                    awarded += weekly_activity_awarded
                    w.weekly_activity_bonus_week_utc = monday_utc
        except Exception:
            pass

        # Day 75: trigger owner + user DM (caller sends DMs)
        if streak_val == 75 and not bool(getattr(w, "streak_75_notification_sent", False)):
            w.streak_75_notification_sent = True
            streak_75_triggered = True

        w.balance = int(w.balance or 0) + int(awarded)
        w.last_claim_day_utc = today
        w.updated_at = now

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

        meta_extra: dict = {
            "day_utc": today,
            "streak": streak_val,
            "first_bonus": first_bonus,
            "milestone_7": milestone_7_awarded,
            "milestone_30": milestone_30_awarded,
            "random_bonus": random_bonus_awarded,
            "comeback": comeback_awarded,
            "weekly_activity": weekly_activity_awarded,
        }
        session.add(
            PointsLedger(
                guild_id=GLOBAL_GUILD_ID,
                user_id=int(user_id),
                delta=int(awarded),
                reason="daily_claim",
                meta_json=json.dumps(meta_extra, separators=(",", ":")),
            )
        )

        await session.commit()

        next_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
        restore_deadline = (getattr(w, "streak_restore_deadline_day_utc", "") or "").strip()
        saved = int(getattr(w, "streak_saved", 0) or 0)
        restore_available = saved > 0 and bool(restore_deadline) and today <= restore_deadline
        restore_to_streak = saved + 1 if restore_available else 0

        char_avail: list[int] = []
        if streak_val >= 10 and not bool(getattr(w, "streak_10_character_claimed", False)):
            char_avail.append(10)
        if streak_val >= 15 and not bool(getattr(w, "streak_15_character_claimed", False)):
            char_avail.append(15)
        if streak_val >= 25 and not bool(getattr(w, "streak_25_character_claimed", False)):
            char_avail.append(25)

        # Record today as activity so claiming counts for retention / next week's bonus
        try:
            from utils.analytics import _touch_active
            await _touch_active(day_utc=today, guild_id=guild_id, user_id=int(user_id))
        except Exception:
            pass

        return DailyResult(
            awarded=int(awarded),
            balance=int(w.balance or 0),
            streak=streak_val,
            claimed_today=True,
            next_claim_in_seconds=max(0, int((next_midnight - now).total_seconds())),
            first_bonus_awarded=int(first_bonus),
            restore_available=restore_available,
            restore_cost=500,
            restore_to_streak=int(restore_to_streak),
            restore_deadline_day_utc=str(restore_deadline or ""),
            milestone_7_awarded=milestone_7_awarded,
            milestone_30_awarded=milestone_30_awarded,
            random_bonus_awarded=random_bonus_awarded,
            random_bonus_chance_pct=random_bonus_chance_pct,
            random_bonus_near_miss=random_bonus_near_miss,
            random_bonus_next_chance_pct=random_bonus_next_chance_pct,
            streak_75_triggered=streak_75_triggered,
            character_reward_available=tuple(char_avail),
            comeback_awarded=comeback_awarded,
            weekly_activity_awarded=weekly_activity_awarded,
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


@dataclass(frozen=True)
class StreakRewardProgress:
    """Read-only snapshot for streak rewards UI."""
    streak: int
    milestone_7_claimed: bool
    next_30_at: int  # e.g. 30, 60, 90
    character_10_available: bool
    character_15_available: bool
    character_25_available: bool
    character_10_claimed: bool
    character_15_claimed: bool
    character_25_claimed: bool
    streak_75_reached: bool
    random_bonus_consecutive_days: int
    random_bonus_chance_pct: int


async def get_streak_reward_progress(*, user_id: int) -> StreakRewardProgress | None:
    """Return current streak reward progress for the user (for /points streak or daily progress)."""
    if select is None:
        return None
    w = await _get_or_create_wallet(guild_id=GLOBAL_GUILD_ID, user_id=user_id)
    streak = int(w.streak or 0)
    last_30 = int(getattr(w, "streak_last_30_bonus_at", 0) or 0)
    next_30 = 30 if streak < 30 else (last_30 + 30)
    rnd_days = int(getattr(w, "random_bonus_consecutive_days", 0) or 0)
    rnd_chance = min(100, RANDOM_BONUS_BASE_CHANCE_PCT + rnd_days * RANDOM_BONUS_INCREMENT_PCT)
    return StreakRewardProgress(
        streak=streak,
        milestone_7_claimed=bool(getattr(w, "streak_7_bonus_given", False)),
        next_30_at=next_30,
        character_10_available=streak >= 10 and not bool(getattr(w, "streak_10_character_claimed", False)),
        character_15_available=streak >= 15 and not bool(getattr(w, "streak_15_character_claimed", False)),
        character_25_available=streak >= 25 and not bool(getattr(w, "streak_25_character_claimed", False)),
        character_10_claimed=bool(getattr(w, "streak_10_character_claimed", False)),
        character_15_claimed=bool(getattr(w, "streak_15_character_claimed", False)),
        character_25_claimed=bool(getattr(w, "streak_25_character_claimed", False)),
        streak_75_reached=bool(getattr(w, "streak_75_notification_sent", False)),
        random_bonus_consecutive_days=rnd_days,
        random_bonus_chance_pct=rnd_chance,
    )


async def claim_streak_character_reward(*, user_id: int, tier: int, style_id: str) -> tuple[bool, str]:
    """Grant a built-in character for streak milestone (10=uncommon, 15=rare, 25=legendary). Returns (ok, message)."""
    if select is None:
        return False, "Database unavailable."
    if tier not in (10, 15, 25):
        return False, "Invalid reward tier."
    uid = int(user_id)
    sid = (style_id or "").strip().lower()
    if not sid:
        return False, "No character selected."

    from utils.character_registry import list_builtin_by_rarity, get_style
    from utils.character_store import add_style_to_inventory

    rarity_map = {10: "uncommon", 15: "rare", 25: "legendary"}
    rarity = rarity_map[tier]
    allowed = list_builtin_by_rarity(rarity)
    allowed_ids = {s.style_id.lower() for s in allowed}
    if sid not in allowed_ids:
        return False, "That character isn't available for this reward."

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
            return False, "Wallet not found."
        attr = f"streak_{tier}_character_claimed"
        if bool(getattr(w, attr, False)):
            return False, "You already claimed this reward."
        streak = int(w.streak or 0)
        if streak < tier:
            return False, f"You need a {tier}-day streak to claim this reward."

        setattr(w, attr, True)
        w.updated_at = _now_utc()
        await session.commit()

    ok, msg = await add_style_to_inventory(user_id=uid, style_id=sid, is_pro=None, guild_id=None)
    if not ok:
        return False, msg or "Could not add character."
    return True, f"Added **{get_style(sid).display_name if get_style(sid) else style_id}** to your collection!"


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
