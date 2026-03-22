"""Global / guild-scoped monthly quest: training points from /talk + bond XP."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from utils.points_store import GLOBAL_GUILD_ID, adjust_points
from utils.db import get_sessionmaker

try:
    from sqlalchemy import select, func, update  # type: ignore
except Exception:  # pragma: no cover
    select = func = update = None  # type: ignore

logger = logging.getLogger("bot.global_quest")


@dataclass
class ActiveQuestView:
    event_id: int
    slug: str
    title: str
    description: str
    image_url: str | None
    image_url_secondary: str | None
    scope: str
    guild_id: int | None
    starts_at: datetime
    ends_at: datetime
    activated_at: datetime | None
    target_training_points: int
    status: str
    character_multipliers: dict[str, float]
    total_training: int
    guild_training: int  # for this guild only
    user_training: int  # this user sum across characters
    user_character_training: int  # selected character only
    days_left: int
    progress_pct: float


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_multipliers(raw: str | None) -> dict[str, float]:
    try:
        d = json.loads(raw or "{}")
        if not isinstance(d, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in d.items():
            try:
                out[str(k).strip().lower()] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return {}


async def get_active_events_for_guild(*, guild_id: int) -> list[Any]:
    """Active events that apply to this guild (global + guild-specific)."""
    if select is None:
        return []
    from utils.models import GlobalQuestEvent

    gid = int(guild_id)
    now = _now()
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(GlobalQuestEvent)
                .where(GlobalQuestEvent.status == "active")
                .where(GlobalQuestEvent.ends_at >= now),
            )
            rows = res.scalars().all()
            out: list[Any] = []
            for ev in rows:
                sc = (getattr(ev, "scope", "") or "").strip().lower()
                eg = getattr(ev, "guild_id", None)
                if sc == "global":
                    out.append(ev)
                elif sc == "guild" and eg is not None and int(eg) == gid:
                    out.append(ev)
            return out
        except Exception:
            logger.exception("get_active_events_for_guild failed")
            return []


async def _sum_training(*, event_id: int, guild_id: int | None = None) -> int:
    from utils.models import GlobalQuestContribution

    if select is None:
        return 0
    Session = get_sessionmaker()
    async with Session() as session:
        q = select(func.sum(GlobalQuestContribution.training_points)).where(
            GlobalQuestContribution.event_id == int(event_id)
        )
        if guild_id is not None:
            q = q.where(GlobalQuestContribution.guild_id == int(guild_id))
        row = (await session.execute(q)).scalar()
        return int(row or 0)


async def _user_training_sum(*, event_id: int, user_id: int, guild_id: int) -> int:
    from utils.models import GlobalQuestContribution

    if select is None:
        return 0
    Session = get_sessionmaker()
    async with Session() as session:
        row = (
            await session.execute(
                select(func.sum(GlobalQuestContribution.training_points)).where(
                    GlobalQuestContribution.event_id == int(event_id),
                    GlobalQuestContribution.guild_id == int(guild_id),
                    GlobalQuestContribution.user_id == int(user_id),
                )
            )
        ).scalar()
        return int(row or 0)


async def _user_style_training(
    *, event_id: int, user_id: int, guild_id: int, style_id: str
) -> int:
    from utils.models import GlobalQuestContribution

    if select is None:
        return 0
    sid = (style_id or "").strip().lower()
    Session = get_sessionmaker()
    async with Session() as session:
        row = (
            await session.execute(
                select(GlobalQuestContribution.training_points).where(
                    GlobalQuestContribution.event_id == int(event_id),
                    GlobalQuestContribution.guild_id == int(guild_id),
                    GlobalQuestContribution.user_id == int(user_id),
                    GlobalQuestContribution.style_id == sid,
                )
            )
        ).scalar_one_or_none()
        return int(row or 0)


async def build_quest_view_for_user(
    *,
    guild_id: int,
    user_id: int,
    selected_style_id: str | None,
) -> ActiveQuestView | None:
    """Single embed-friendly view: prefer guild-scoped event, else first global."""
    events = await get_active_events_for_guild(guild_id=guild_id)
    if not events:
        return None
    # Prefer guild-specific
    ev = None
    for e in events:
        if (getattr(e, "scope", "") or "").strip().lower() == "guild":
            ev = e
            break
    if ev is None:
        ev = events[0]

    eid = int(getattr(ev, "id", 0))
    scope = (getattr(ev, "scope", "") or "").strip().lower()
    eg = getattr(ev, "guild_id", None)
    total = await _sum_training(event_id=eid, guild_id=None)
    if scope == "guild" and eg is not None:
        gtrain = await _sum_training(event_id=eid, guild_id=int(eg))
    else:
        gtrain = await _sum_training(event_id=eid, guild_id=int(guild_id))

    ut = await _user_training_sum(event_id=eid, user_id=user_id, guild_id=int(guild_id))
    sid = (selected_style_id or "").strip().lower()
    uc = await _user_style_training(
        event_id=eid, user_id=user_id, guild_id=int(guild_id), style_id=sid
    ) if sid else 0

    target = max(1, int(getattr(ev, "target_training_points", 1) or 1))
    if scope == "guild" and eg is not None:
        bar_total = float(gtrain)
    else:
        bar_total = float(total)
    pct = min(100.0, 100.0 * bar_total / float(target))

    ends = getattr(ev, "ends_at", None)
    if ends and getattr(ends, "tzinfo", None) is None:
        ends = ends.replace(tzinfo=timezone.utc)
    dl = 0
    if ends:
        dl = max(0, int((ends - _now()).total_seconds() // 86400))

    mult = _parse_multipliers(getattr(ev, "character_multipliers_json", None))

    act_at = getattr(ev, "activated_at", None)
    if act_at and getattr(act_at, "tzinfo", None) is None:
        act_at = act_at.replace(tzinfo=timezone.utc)

    return ActiveQuestView(
        event_id=eid,
        slug=str(getattr(ev, "slug", "") or ""),
        title=str(getattr(ev, "title", "") or ""),
        description=str(getattr(ev, "description", "") or ""),
        image_url=getattr(ev, "image_url", None),
        image_url_secondary=getattr(ev, "image_url_secondary", None),
        scope=scope,
        guild_id=int(eg) if eg is not None else None,
        starts_at=getattr(ev, "starts_at", _now()),
        ends_at=ends or _now(),
        activated_at=act_at,
        target_training_points=target,
        status=str(getattr(ev, "status", "") or ""),
        character_multipliers=mult,
        total_training=total,
        guild_training=gtrain,
        user_training=ut,
        user_character_training=uc,
        days_left=dl,
        progress_pct=round(pct, 1),
    )


async def record_training_from_talk(
    *,
    guild_id: int,
    user_id: int,
    style_id: str,
    selected_style_id: str,
    bond_xp_gained: int,
) -> None:
    """Award training when user talks with their selected owned character."""
    sid = (style_id or "").strip().lower()
    sel = (selected_style_id or "").strip().lower()
    if not sid or not sel or sid != sel:
        return

    events = await get_active_events_for_guild(guild_id=int(guild_id))
    if not events:
        return
    # One training credit path per talk: prefer guild-scoped event over global.
    guild_first = sorted(
        events,
        key=lambda e: 0 if (getattr(e, "scope", "") or "").strip().lower() == "guild" else 1,
    )
    ev = guild_first[0]
    try:
        await _apply_training_delta(
            event=ev,
            guild_id=int(guild_id),
            user_id=int(user_id),
            style_id=sid,
            bond_xp_gained=int(bond_xp_gained),
        )
    except Exception:
        logger.debug("training event failed", exc_info=True)


async def _apply_training_delta(
    *,
    event: Any,
    guild_id: int,
    user_id: int,
    style_id: str,
    bond_xp_gained: int,
) -> None:
    from utils.models import GlobalQuestContribution

    eid = int(getattr(event, "id", 0))
    mult = _parse_multipliers(getattr(event, "character_multipliers_json", None)).get(
        style_id, 1.0
    )
    base = 1 + max(0, min(50, int(bond_xp_gained)))
    delta = max(1, int(round(base * float(mult))))

    if select is None:
        return

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(GlobalQuestContribution)
            .where(GlobalQuestContribution.event_id == eid)
            .where(GlobalQuestContribution.guild_id == guild_id)
            .where(GlobalQuestContribution.user_id == user_id)
            .where(GlobalQuestContribution.style_id == style_id)
            .limit(1)
        )
        row = res.scalar_one_or_none()
        now = _now()
        if row is None:
            row = GlobalQuestContribution(
                event_id=eid,
                guild_id=guild_id,
                user_id=user_id,
                style_id=style_id,
                training_points=int(delta),
                updated_at=now,
            )
            session.add(row)
        else:
            row.training_points = int(
                getattr(row, "training_points", 0) or 0
            ) + int(delta)
            row.updated_at = now
        await session.commit()

    await resolve_event_if_needed(event_id=eid)


async def resolve_event_if_needed(*, event_id: int) -> None:
    """Complete or fail event; apply rewards once."""
    from utils.models import GlobalQuestEvent

    if select is None:
        return

    Session = get_sessionmaker()
    async with Session() as session:
        ev = await session.get(GlobalQuestEvent, int(event_id))
        if ev is None or getattr(ev, "resolution_applied", False):
            return
        if getattr(ev, "status", "") != "active":
            return

        scope = (getattr(ev, "scope", "") or "").strip().lower()
        eg = getattr(ev, "guild_id", None)
        target = max(1, int(getattr(ev, "target_training_points", 1) or 1))
        ends = getattr(ev, "ends_at", None)
        if ends and getattr(ends, "tzinfo", None) is None:
            ends = ends.replace(tzinfo=timezone.utc)

        if scope == "guild" and eg is not None:
            total = await _sum_training(event_id=event_id, guild_id=int(eg))
        else:
            total = await _sum_training(event_id=event_id, guild_id=None)

        now = _now()
        success = total >= target
        timed_out = ends is not None and now > ends

        if not success and not timed_out:
            return

        if success:
            await session.execute(
                update(GlobalQuestEvent)
                .where(GlobalQuestEvent.id == int(event_id))
                .values(
                    status="completed_success",
                    resolution_applied=True,
                    updated_at=now,
                )
            )
            await session.commit()
            await _apply_points_to_contributors(
                event_id=event_id,
                delta=int(getattr(ev, "reward_points", 0) or 0),
                reason="global_quest_success",
            )
            if getattr(ev, "grant_success_badge", True):
                emoji = (getattr(ev, "success_badge_emoji", None) or "🏆")[:16]
                label = (getattr(ev, "success_badge_label", None) or getattr(ev, "title", "Quest"))[:120]
                badge_key = f"gq:{getattr(ev, 'slug', '')}:{event_id}"[:128]
                display = f"{emoji} {label}".strip()
                await _grant_badges_to_contributors(
                    event_id=event_id,
                    badge_key=badge_key,
                    display_text=display,
                )
            return

        if timed_out:
            await session.execute(
                update(GlobalQuestEvent)
                .where(GlobalQuestEvent.id == int(event_id))
                .values(
                    status="completed_fail",
                    resolution_applied=True,
                    updated_at=now,
                )
            )
            await session.commit()
            await _apply_points_to_contributors(
                event_id=event_id,
                delta=int(getattr(ev, "failure_points", 0) or 0),
                reason="global_quest_fail",
            )


async def _apply_points_to_contributors(
    *,
    event_id: int,
    delta: int,
    reason: str,
) -> None:
    if delta == 0:
        return
    from utils.models import GlobalQuestContribution

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(GlobalQuestContribution.user_id)
            .where(GlobalQuestContribution.event_id == int(event_id))
            .distinct()
        )
        uids = [int(r[0]) for r in res.all()]
    for uid in uids:
        try:
            await adjust_points(
                guild_id=GLOBAL_GUILD_ID,
                user_id=uid,
                delta=delta,
                reason=reason,
                meta={"event_id": event_id},
            )
        except Exception:
            logger.exception("global quest points uid=%s", uid)


async def _grant_badges_to_contributors(
    *,
    event_id: int,
    badge_key: str,
    display_text: str,
) -> None:
    from utils.models import GlobalQuestContribution, UserProfileBadge

    if select is None:
        return
    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(GlobalQuestContribution.user_id)
            .where(GlobalQuestContribution.event_id == int(event_id))
            .distinct()
        )
        uids = [int(r[0]) for r in res.all()]
        now = _now()
        for uid in uids:
            try:
                existing = await session.execute(
                    select(UserProfileBadge)
                    .where(UserProfileBadge.user_id == uid)
                    .where(UserProfileBadge.badge_key == badge_key)
                    .limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    continue
                session.add(
                    UserProfileBadge(
                        user_id=uid,
                        badge_key=badge_key,
                        display_text=display_text[:200],
                        source_event_id=int(event_id),
                        created_at=now,
                    )
                )
            except Exception:
                logger.debug("badge grant skip uid=%s", uid, exc_info=True)
        await session.commit()


async def list_user_badges(*, user_id: int, limit: int = 20) -> list[str]:
    """Display strings for /inspect."""
    from utils.models import UserProfileBadge

    if select is None:
        return []
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(UserProfileBadge.display_text)
                .where(UserProfileBadge.user_id == int(user_id))
                .order_by(UserProfileBadge.created_at.desc())
                .limit(limit)
            )
            return [str(r[0]) for r in res.all() if r[0]]
        except Exception:
            return []
