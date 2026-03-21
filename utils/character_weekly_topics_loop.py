"""Background: pre-generate weekly character topics (Monday UTC).

Eligible users: **yesterday's** daily quest sum > 5 (see run function) and a selected character.
Idempotent per ISO week via Redis + DB unique key.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from utils.backpressure import get_redis_or_none
from utils.character_registry import get_style
from utils.character_store import load_state
from utils.character_weekly_topics import (
    current_iso_week_id,
    generate_weekly_topics_ai,
    insert_weekly_topics_row,
    is_eligible_for_weekly_topics,
    load_weekly_topics_bundle,
)
from utils.db import get_sessionmaker
from utils.models import QuestProgress
from utils.points_store import GLOBAL_GUILD_ID
from utils.quests import _daily_key, _now_utc  # noqa: WPS433

try:
    from sqlalchemy import func, select  # type: ignore
except Exception:  # pragma: no cover
    func = None  # type: ignore
    select = None  # type: ignore

logger = logging.getLogger("bot.character_weekly_topics_loop")

CHECK_INTERVAL_SECONDS = 60 * 15  # 15 min so Monday 00:30–00:45 window is hit reliably
TARGET_WEEKDAY = 0  # Monday
TARGET_HOUR = 0
TARGET_MINUTE = 30
REDIS_LAST_KEY = "character_weekly_topics:last_gen"


async def _already_ran_this_week() -> bool:
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        val = await r.get(REDIS_LAST_KEY)
        if val is None:
            return False
        s = val.decode("utf-8", errors="ignore") if isinstance(val, (bytes, bytearray)) else str(val)
        return s.strip() == current_iso_week_id()
    except Exception:
        return False


async def _mark_ran_this_week() -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.set(REDIS_LAST_KEY, current_iso_week_id(), ex=86400 * 10)
    except Exception:
        logger.exception("character_weekly_topics mark redis failed")


async def _user_ids_with_daily_progress_over_5(*, day_key: str) -> list[int]:
    if select is None or func is None:
        return []
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(QuestProgress.user_id, func.sum(QuestProgress.progress))
                .where(QuestProgress.guild_id == GLOBAL_GUILD_ID)
                .where(QuestProgress.period == "daily")
                .where(QuestProgress.period_key == day_key)
                .group_by(QuestProgress.user_id)
            )
            out: list[int] = []
            for uid, total in res.all():
                if int(total or 0) > 5:
                    out.append(int(uid))
            return out
        except Exception:
            logger.exception("weekly topics eligible user query failed")
            return []


async def run_weekly_character_topics_generation(_bot: discord.Client) -> None:
    """Generate rows for eligible users who don't have a row yet this week.

    Uses yesterday's daily quest totals so Monday morning batch sees real numbers.
    """
    yesterday_key = _daily_key(_now_utc() - timedelta(days=1))
    uids = await _user_ids_with_daily_progress_over_5(day_key=yesterday_key)
    if not uids:
        logger.info("Weekly character topics: no eligible users (daily sum > 5).")
        return

    generated = 0
    for uid in uids:
        try:
            st = await load_state(uid)
            sid = (getattr(st, "active_style_id", "") or "").strip().lower()
            if not sid:
                continue
            if not await is_eligible_for_weekly_topics(
                user_id=uid,
                style_id=sid,
                progress_day_key=yesterday_key,
            ):
                continue

            bundle = await load_weekly_topics_bundle(user_id=uid, style_id=sid)
            if bundle and any(t.get("title") for t in bundle.topics):
                continue

            style = get_style(sid)
            if style is None:
                continue

            payload = await generate_weekly_topics_ai(style)
            if not payload:
                continue

            ok = await insert_weekly_topics_row(user_id=uid, style_id=sid, payload=payload)
            if ok:
                generated += 1
        except Exception:
            logger.debug("weekly topic gen skip user=%s", uid, exc_info=True)
        await asyncio.sleep(0.35)

    if generated:
        logger.info("Weekly character topics: generated %s new rows", generated)


def start_character_weekly_topics_loop(bot: discord.Client) -> None:
    async def _runner() -> None:
        await asyncio.sleep(15)
        while True:
            try:
                now = datetime.now(timezone.utc)
                if (
                    now.weekday() == TARGET_WEEKDAY
                    and now.hour == TARGET_HOUR
                    and now.minute >= TARGET_MINUTE
                ):
                    if not await _already_ran_this_week():
                        await run_weekly_character_topics_generation(bot)
                        await _mark_ran_this_week()
            except Exception:
                logger.exception("character_weekly_topics_loop tick failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    asyncio.create_task(_runner())
    logger.info("character_weekly_topics_loop scheduled (Monday ~%02d:%02d UTC)", TARGET_HOUR, TARGET_MINUTE)
