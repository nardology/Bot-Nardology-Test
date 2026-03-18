from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from utils.analytics import utc_day_str
from utils.db import get_sessionmaker
from utils.models import DailyTopicCompletion, DailyTopicConfig, DailyTopicHistory

try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore

logger = logging.getLogger("bot.daily_topic")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DailyTopic:
    guild_id: int
    topic_text: str
    topic_description: str
    examples: list[str]
    topic_version: int
    last_set_day_utc: str
    last_auto_rotate_day_utc: str


def _safe_examples_from_json(s: str) -> list[str]:
    try:
        v = json.loads(s or "[]")
        if isinstance(v, list):
            out = []
            for x in v:
                if isinstance(x, str) and x.strip():
                    out.append(x.strip()[:200])
            return out[:10]
    except Exception:
        pass
    return []


def _examples_to_json(examples: list[str] | None) -> str:
    xs = []
    for x in (examples or []):
        if not isinstance(x, str):
            continue
        t = x.strip()
        if t:
            xs.append(t[:200])
    return json.dumps(xs[:10], separators=(",", ":"))


async def set_daily_topic(
    *,
    guild_id: int,
    topic_text: str,
    topic_description: str = "",
    examples: list[str] | None = None,
) -> DailyTopic | None:
    """Set the daily topic manually. Increments topic_version and appends history."""
    if select is None:
        return None
    gid = int(guild_id)
    topic_text = (topic_text or "").strip()
    topic_description = (topic_description or "").strip()
    ex_json = _examples_to_json(examples)
    today = utc_day_str()
    now = _now_utc()

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(DailyTopicConfig)
                .where(DailyTopicConfig.guild_id == gid)
                .with_for_update()
                .limit(1)
            )
            cfg = res.scalar_one_or_none()
            if cfg is None:
                cfg = DailyTopicConfig(guild_id=gid)
                session.add(cfg)
                await session.flush()

            # Bump version whenever the topic is changed (or re-set).
            cfg.topic_version = int(getattr(cfg, "topic_version", 1) or 1) + 1
            cfg.topic_text = topic_text[:120]
            cfg.topic_description = topic_description
            cfg.topic_examples_json = ex_json
            cfg.last_set_day_utc = today
            cfg.updated_at = now

            session.add(
                DailyTopicHistory(
                    guild_id=gid,
                    topic_text=cfg.topic_text,
                    topic_description=cfg.topic_description,
                    topic_examples_json=cfg.topic_examples_json,
                    created_at=now,
                )
            )

            await session.commit()
            return DailyTopic(
                guild_id=gid,
                topic_text=str(cfg.topic_text or ""),
                topic_description=str(cfg.topic_description or ""),
                examples=_safe_examples_from_json(str(cfg.topic_examples_json or "")),
                topic_version=int(cfg.topic_version or 0),
                last_set_day_utc=str(cfg.last_set_day_utc or ""),
                last_auto_rotate_day_utc=str(cfg.last_auto_rotate_day_utc or ""),
            )
        except Exception:
            logger.exception("set_daily_topic failed")
            try:
                await session.rollback()
            except Exception:
                pass
            return None


async def get_or_rotate_daily_topic(*, guild_id: int) -> DailyTopic | None:
    """Get the current topic; if no manual set today, auto-rotate once/day from history when possible."""
    if select is None:
        return None
    gid = int(guild_id)
    today = utc_day_str()
    now = _now_utc()

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(DailyTopicConfig).where(DailyTopicConfig.guild_id == gid).with_for_update().limit(1)
            )
            cfg = res.scalar_one_or_none()
            if cfg is None:
                return None

            last_set = str(getattr(cfg, "last_set_day_utc", "") or "")
            last_rot = str(getattr(cfg, "last_auto_rotate_day_utc", "") or "")

            # Auto-rotate only if no manual set today and we haven't rotated today.
            if last_set != today and last_rot != today:
                # Pull a small sample of history; choose a different topic if possible.
                hres = await session.execute(
                    select(DailyTopicHistory)
                    .where(DailyTopicHistory.guild_id == gid)
                    .order_by(DailyTopicHistory.created_at.desc())
                    .limit(50)
                )
                rows = list(hres.scalars().all() or [])
                if rows:
                    cur_text = str(getattr(cfg, "topic_text", "") or "")
                    candidates = [r for r in rows if str(getattr(r, "topic_text", "") or "").strip() and str(getattr(r, "topic_text", "") or "") != cur_text]
                    if candidates:
                        picked = random.choice(candidates)
                        cfg.topic_version = int(getattr(cfg, "topic_version", 1) or 1) + 1
                        cfg.topic_text = str(getattr(picked, "topic_text", "") or "")[:120]
                        cfg.topic_description = str(getattr(picked, "topic_description", "") or "")
                        cfg.topic_examples_json = str(getattr(picked, "topic_examples_json", "") or "")
                        cfg.last_auto_rotate_day_utc = today
                        cfg.updated_at = now
                        await session.commit()

            return DailyTopic(
                guild_id=gid,
                topic_text=str(getattr(cfg, "topic_text", "") or ""),
                topic_description=str(getattr(cfg, "topic_description", "") or ""),
                examples=_safe_examples_from_json(str(getattr(cfg, "topic_examples_json", "") or "")),
                topic_version=int(getattr(cfg, "topic_version", 0) or 0),
                last_set_day_utc=str(getattr(cfg, "last_set_day_utc", "") or ""),
                last_auto_rotate_day_utc=str(getattr(cfg, "last_auto_rotate_day_utc", "") or ""),
            )
        except Exception:
            logger.exception("get_or_rotate_daily_topic failed")
            try:
                await session.rollback()
            except Exception:
                pass
            return None


async def topic_bonus_already_claimed(*, guild_id: int, user_id: int, topic_version: int) -> bool:
    if select is None:
        return False
    gid = int(guild_id)
    uid = int(user_id)
    v = int(topic_version or 0)
    if v <= 0:
        return False
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(DailyTopicCompletion)
                .where(DailyTopicCompletion.guild_id == gid)
                .where(DailyTopicCompletion.user_id == uid)
                .where(DailyTopicCompletion.topic_version == v)
                .limit(1)
            )
            return res.scalar_one_or_none() is not None
        except Exception:
            return False


async def mark_topic_bonus_claimed(*, guild_id: int, user_id: int, topic_version: int) -> bool:
    """Mark the topic bonus as claimed for this topic version (idempotent)."""
    if select is None:
        return False
    gid = int(guild_id)
    uid = int(user_id)
    v = int(topic_version or 0)
    if v <= 0:
        return False
    day = utc_day_str()
    now = _now_utc()
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            # Insert if missing (unique index enforces idempotency)
            session.add(
                DailyTopicCompletion(
                    guild_id=gid,
                    user_id=uid,
                    topic_version=v,
                    completed_day_utc=day,
                    created_at=now,
                )
            )
            await session.commit()
            return True
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            return False

