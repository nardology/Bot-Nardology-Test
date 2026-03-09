from __future__ import annotations

"""Simple daily/weekly/monthly quests for the points economy.

Design goals:
- Zero AI cost (quests reward points for using existing commands)
- Durable (Postgres) but safe if DB hiccups (quests should never block core commands)
- One-time completion per period (daily/weekly/monthly)
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import discord

from utils.db import get_sessionmaker
from utils.analytics import utc_day_str
from utils.models import PointsWallet, PointsLedger, QuestProgress, QuestClaim

try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore

logger = logging.getLogger("bot.quests")


RARITY_ORDER = {
    "common": 0,
    "rare": 1,
    "epic": 2,
    "legendary": 3,
    "mythic": 4,
}


@dataclass(frozen=True)
class QuestDef:
    quest_id: str
    period: str  # daily|weekly|monthly
    name: str
    description: str
    points: int
    goal: int
    event: str  # talk|roll
    min_rarity: str = ""  # optional: only count rolls >= this rarity


QUESTS: list[QuestDef] = [
    # Daily
    QuestDef(
        quest_id="daily_talk_3",
        period="daily",
        name="Talk to the AI (x3)",
        description="Use /talk 3 times today.",
        points=50,
        goal=3,
        event="talk",
    ),
    QuestDef(
        quest_id="daily_roll_3",
        period="daily",
        name="Roll 3 characters",
        description="Roll 3 characters today.",
        points=60,
        goal=3,
        event="roll",
    ),
    QuestDef(
        quest_id="daily_roll_1",
        period="daily",
        name="Roll a character",
        description="Roll 1 character today.",
        points=30,
        goal=1,
        event="roll",
    ),
    # Weekly
    QuestDef(
        quest_id="weekly_roll_rare",
        period="weekly",
        name="Roll a Rare+ character",
        description="Roll at least one Rare (or better) this week.",
        points=150,
        goal=1,
        event="roll",
        min_rarity="rare",
    ),
    QuestDef(
        quest_id="weekly_roll_10",
        period="weekly",
        name="Roll 10 characters",
        description="Roll 10 characters this week.",
        points=200,
        goal=10,
        event="roll",
    ),
    QuestDef(
        quest_id="weekly_talk_10",
        period="weekly",
        name="Talk to the AI (x10)",
        description="Use /talk 10 times this week.",
        points=150,
        goal=10,
        event="talk",
    ),
    # Monthly (simple)
    QuestDef(
        quest_id="monthly_roll_30",
        period="monthly",
        name="Roll 30 characters",
        description="Roll 30 characters this month.",
        points=600,
        goal=30,
        event="roll",
    ),
    QuestDef(
        quest_id="monthly_talk_50",
        period="monthly",
        name="Talk to the AI (x50)",
        description="Use /talk 50 times this month.",
        points=600,
        goal=50,
        event="talk",
    ),
]


# Stable quest numbering for UI.
# We number quests in the order they appear in QUESTS.
_QUEST_NUMBERS: dict[str, int] = {q.quest_id: i + 1 for i, q in enumerate(QUESTS)}


def quest_number(quest_id: str) -> int:
    """Return a stable 1-based quest number for display/buttons."""
    return int(_QUEST_NUMBERS.get((quest_id or "").strip(), 0) or 0)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _daily_key(now: datetime) -> str:
    return utc_day_str(int(now.timestamp()))  # YYYYMMDD


def _weekly_key(now: datetime) -> str:
    # Monday as start of week (UTC)
    d = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y%m%d")


def _monthly_key(now: datetime) -> str:
    return f"{now.year:04d}{now.month:02d}"  # YYYYMM


def _period_key(period: str, now: datetime) -> str:
    p = (period or "daily").strip().lower()
    if p == "weekly":
        return _weekly_key(now)
    if p == "monthly":
        return _monthly_key(now)
    return _daily_key(now)


def _counts_roll(meta: dict | None, *, min_rarity: str) -> bool:
    if not min_rarity:
        return True
    rarity = str((meta or {}).get("rarity") or "").strip().lower()
    if not rarity:
        return False
    return RARITY_ORDER.get(rarity, -1) >= RARITY_ORDER.get(min_rarity, 999)


@dataclass(frozen=True)
class QuestReadyToClaim:
    quest_id: str
    name: str
    points: int
    period: str
    period_key: str


async def apply_quest_event(*, guild_id: int, user_id: int, event: str, meta: dict | None = None) -> list[QuestReadyToClaim]:
    """Update quest progress for an event.

    Returns quests that became COMPLETED and are now READY TO CLAIM.

    This function is designed to be called from command handlers.
    It should never raise in a way that blocks the user response.
    """
    if select is None:
        return []

    gid = int(guild_id)
    uid = int(user_id)
    ev = (event or "").strip().lower()
    if not ev:
        return []

    now = _now_utc()
    relevant = [q for q in QUESTS if q.event == ev]
    if not relevant:
        return []

    Session = get_sessionmaker()
    completions: list[QuestReadyToClaim] = []

    async with Session() as session:
        try:
            # Ensure wallet row exists (so /points displays don't look broken)
            wres = await session.execute(
                select(PointsWallet)
                .where(PointsWallet.guild_id == gid)
                .where(PointsWallet.user_id == uid)
                .with_for_update()
                .limit(1)
            )
            wallet = wres.scalar_one_or_none()
            if wallet is None:
                wallet = PointsWallet(guild_id=gid, user_id=uid)
                session.add(wallet)
                await session.flush()

            # Process each quest
            for q in relevant:
                pkey = _period_key(q.period, now)

                # Lock progress row
                qres = await session.execute(
                    select(QuestProgress)
                    .where(QuestProgress.guild_id == gid)
                    .where(QuestProgress.user_id == uid)
                    .where(QuestProgress.period == q.period)
                    .where(QuestProgress.quest_id == q.quest_id)
                    .with_for_update()
                    .limit(1)
                )
                row = qres.scalar_one_or_none()
                if row is None:
                    row = QuestProgress(
                        guild_id=gid,
                        user_id=uid,
                        period=q.period,
                        period_key=pkey,
                        quest_id=q.quest_id,
                        progress=0,
                        completed=False,
                    )
                    session.add(row)
                    await session.flush()

                # Reset if period changed
                if (row.period_key or "") != pkey:
                    row.period_key = pkey
                    row.progress = 0
                    row.completed = False

                if bool(row.completed):
                    continue

                # Count this event?
                if q.event == "roll" and not _counts_roll(meta, min_rarity=q.min_rarity):
                    continue

                row.progress = int(row.progress or 0) + 1
                row.updated_at = now

                if int(row.progress or 0) >= int(q.goal or 1):
                    row.completed = True

                    completions.append(
                        QuestReadyToClaim(
                            quest_id=q.quest_id,
                            name=q.name,
                            points=int(q.points or 0),
                            period=q.period,
                            period_key=pkey,
                        )
                    )

            await session.commit()
        except Exception:
            logger.exception("apply_quest_event failed")
            try:
                await session.rollback()
            except Exception:
                pass
            return []

    return completions


def _quest_by_id(quest_id: str) -> QuestDef | None:
    qid = (quest_id or "").strip()
    for q in QUESTS:
        if q.quest_id == qid:
            return q
    return None


async def get_claimable_quest_ids(*, guild_id: int, user_id: int) -> list[str]:
    """Return quest_ids that are completed for the current period and not yet claimed."""
    if select is None:
        return []

    gid = int(guild_id)
    uid = int(user_id)
    now = _now_utc()

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            pres = await session.execute(
                select(QuestProgress)
                .where(QuestProgress.guild_id == gid)
                .where(QuestProgress.user_id == uid)
            )
            prog_rows = pres.scalars().all()

            cres = await session.execute(
                select(QuestClaim)
                .where(QuestClaim.guild_id == gid)
                .where(QuestClaim.user_id == uid)
            )
            claim_rows = cres.scalars().all()
        except Exception:
            logger.exception("get_claimable_quest_ids failed")
            return []

    # Build set of claimed keys
    claimed: set[tuple[str, str, str]] = set()
    for c in claim_rows:
        claimed.add((str(c.period or ""), str(c.period_key or ""), str(c.quest_id or "")))

    out: list[str] = []
    for r in prog_rows:
        period = str(r.period or "daily")
        qid = str(r.quest_id or "")
        qdef = _quest_by_id(qid)
        if qdef is None:
            continue
        pkey_now = _period_key(period, now)
        if (r.period_key or "") != pkey_now:
            continue
        if not bool(r.completed):
            continue
        if (period, pkey_now, qid) in claimed:
            continue
        out.append(qid)

    # Keep stable order (same order as QUESTS)
    order = {q.quest_id: i for i, q in enumerate(QUESTS)}
    out.sort(key=lambda qid: order.get(qid, 9999))
    return out


async def claim_quest_reward(*, guild_id: int, user_id: int, quest_id: str) -> tuple[bool, str, int, int]:
    """Claim a single quest reward.

    Returns: (ok, message, awarded_points, new_balance)
    """
    if select is None:
        return False, "Quests are unavailable.", 0, 0

    gid = int(guild_id)
    uid = int(user_id)
    qid = (quest_id or "").strip()
    qdef = _quest_by_id(qid)
    if qdef is None:
        return False, "Unknown quest.", 0, 0

    now = _now_utc()
    pkey = _period_key(qdef.period, now)

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            # Lock wallet
            wres = await session.execute(
                select(PointsWallet)
                .where(PointsWallet.guild_id == gid)
                .where(PointsWallet.user_id == uid)
                .with_for_update()
                .limit(1)
            )
            wallet = wres.scalar_one_or_none()
            if wallet is None:
                wallet = PointsWallet(guild_id=gid, user_id=uid)
                session.add(wallet)
                await session.flush()

            # Lock quest progress
            qres = await session.execute(
                select(QuestProgress)
                .where(QuestProgress.guild_id == gid)
                .where(QuestProgress.user_id == uid)
                .where(QuestProgress.period == qdef.period)
                .where(QuestProgress.quest_id == qid)
                .with_for_update()
                .limit(1)
            )
            row = qres.scalar_one_or_none()
            if row is None or (row.period_key or "") != pkey:
                return False, "That quest isn't active right now.", 0, int(wallet.balance or 0)
            if not bool(row.completed):
                return False, "That quest isn't completed yet.", 0, int(wallet.balance or 0)

            # Already claimed?
            cres = await session.execute(
                select(QuestClaim)
                .where(QuestClaim.guild_id == gid)
                .where(QuestClaim.user_id == uid)
                .where(QuestClaim.period == qdef.period)
                .where(QuestClaim.period_key == pkey)
                .where(QuestClaim.quest_id == qid)
                .limit(1)
            )
            existing = cres.scalar_one_or_none()
            if existing is not None:
                return False, "Already claimed.", 0, int(wallet.balance or 0)

            # Award
            delta = int(qdef.points or 0)
            wallet.balance = int(wallet.balance or 0) + delta
            wallet.updated_at = now
            session.add(
                PointsLedger(
                    guild_id=gid,
                    user_id=uid,
                    delta=delta,
                    reason="quest_claim",
                    meta_json=json.dumps(
                        {"quest_id": qid, "period": qdef.period, "period_key": pkey},
                        separators=(",", ":"),
                    ),
                )
            )
            session.add(
                QuestClaim(
                    guild_id=gid,
                    user_id=uid,
                    period=qdef.period,
                    period_key=pkey,
                    quest_id=qid,
                    claimed_at=now,
                )
            )
            await session.commit()
            return True, "Claimed!", delta, int(wallet.balance or 0)
        except Exception:
            logger.exception("claim_quest_reward failed")
            try:
                await session.rollback()
            except Exception:
                pass
            return False, "Claim failed (DB error).", 0, 0


async def claim_all_rewards(*, guild_id: int, user_id: int) -> tuple[int, int, list[QuestReadyToClaim]]:
    """Claim all currently-claimable quest rewards."""
    qids = await get_claimable_quest_ids(guild_id=guild_id, user_id=user_id)
    awarded_total = 0
    new_balance = 0
    claimed_defs: list[QuestReadyToClaim] = []
    for qid in qids:
        ok, _msg, awarded, bal = await claim_quest_reward(guild_id=guild_id, user_id=user_id, quest_id=qid)
        if ok and awarded > 0:
            qdef = _quest_by_id(qid)
            if qdef:
                claimed_defs.append(
                    QuestReadyToClaim(
                        quest_id=qid,
                        name=qdef.name,
                        points=awarded,
                        period=qdef.period,
                        period_key=_period_key(qdef.period, _now_utc()),
                    )
                )
            awarded_total += awarded
            new_balance = bal
        else:
            new_balance = bal
    return awarded_total, new_balance, claimed_defs


async def build_quest_status_embed(*, guild_id: int, user_id: int) -> discord.Embed:
    """Build an embed showing current quest progress."""
    gid = int(guild_id)
    uid = int(user_id)
    now = _now_utc()
    if select is None:
        e = discord.Embed(title="🎯 Quests", description="Quests are unavailable (DB not configured).")
        return e

    # Load wallet balance + progress rows + claim rows
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            wres = await session.execute(
                select(PointsWallet)
                .where(PointsWallet.guild_id == gid)
                .where(PointsWallet.user_id == uid)
                .limit(1)
            )
            wallet = wres.scalar_one_or_none()
            bal = int(getattr(wallet, "balance", 0) or 0)

            # Fetch all progress rows for this user
            pres = await session.execute(
                select(QuestProgress)
                .where(QuestProgress.guild_id == gid)
                .where(QuestProgress.user_id == uid)
            )
            rows = pres.scalars().all()

            cres = await session.execute(
                select(QuestClaim)
                .where(QuestClaim.guild_id == gid)
                .where(QuestClaim.user_id == uid)
            )
            claim_rows = cres.scalars().all()
        except Exception:
            logger.exception("build_quest_status_embed failed")
            rows = []
            bal = 0
            claim_rows = []

    # Index by (period, quest_id)
    by_key: dict[tuple[str, str], QuestProgress] = {}
    for r in rows:
        by_key[(str(r.period or ""), str(r.quest_id or ""))] = r

    claimed: set[tuple[str, str, str]] = set()
    for c in claim_rows:
        claimed.add((str(c.period or ""), str(c.period_key or ""), str(c.quest_id or "")))

    e = discord.Embed(
        title="🎯 Quests",
        description=f"Balance: **{bal}** points\nComplete quests to earn more points.",
        color=0xE67E22,
    )

    for period in ("daily", "weekly", "monthly"):
        pkey = _period_key(period, now)
        lines: list[str] = []
        for q in [qq for qq in QUESTS if qq.period == period]:
            row = by_key.get((period, q.quest_id))
            prog = 0
            done = False
            is_claimed = False
            if row is not None and (row.period_key or "") == pkey:
                prog = int(row.progress or 0)
                done = bool(row.completed)
                is_claimed = (period, pkey, q.quest_id) in claimed

            if done and not is_claimed:
                mark = "🎁"
            elif done and is_claimed:
                mark = "✅"
            else:
                mark = "▫️"

            num = quest_number(q.quest_id)
            suffix = " *(READY TO CLAIM)*" if (done and not is_claimed) else ""
            lines.append(
                f"{mark} **{num}. {q.name}** — {prog}/{q.goal}  *(+{q.points})*{suffix}"
            )

        if lines:
            label = {
                "daily": "Daily (UTC)",
                "weekly": "Weekly (UTC, resets Monday)",
                "monthly": "Monthly (UTC)",
            }.get(period, period)
            e.add_field(name=label, value="\n".join(lines), inline=False)

    e.set_footer(text="Complete quests, then claim rewards here.")
    return e
