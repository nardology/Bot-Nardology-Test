# utils/leaderboard_reset_loop.py
"""Background loop: reset daily/weekly/monthly leaderboard keys so periods show real windows.

Runs every hour; resets each period at most once per day/week/month using Redis markers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from utils.backpressure import get_redis_or_none
from utils.leaderboard import (
    reset_period,
    PERIOD_DAILY,
    PERIOD_WEEKLY,
    PERIOD_MONTHLY,
)
from utils.analytics import utc_day_str

logger = logging.getLogger("bot.leaderboard_reset")

CHECK_INTERVAL_SECONDS = 60 * 60  # run every hour
LAST_DAILY_KEY = "leaderboard_reset:last_daily"   # value: YYYYMMDD
LAST_WEEKLY_KEY = "leaderboard_reset:last_weekly"  # value: YYYYMMDD (Monday of week)
LAST_MONTHLY_KEY = "leaderboard_reset:last_monthly"  # value: YYYYMM


def _utc_today() -> str:
    return utc_day_str()


def _utc_week_monday() -> str:
    """YYYYMMDD for Monday of current week (UTC)."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y%m%d")


def _utc_month() -> str:
    """YYYYMM for current month (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y%m")


def _redis_str(val: bytes | str | None) -> str:
    """Normalize Redis GET result to str (Redis may return bytes or str depending on client)."""
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="ignore").strip()
    return str(val).strip()


async def _tick() -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    today = _utc_today()
    week = _utc_week_monday()
    month = _utc_month()
    try:
        last_daily = _redis_str(await r.get(LAST_DAILY_KEY))
        if last_daily != today:
            await reset_period(PERIOD_DAILY)
            await r.set(LAST_DAILY_KEY, today, ex=86400 * 32)
            logger.info("Leaderboard daily period reset (day=%s)", today)

        last_weekly = _redis_str(await r.get(LAST_WEEKLY_KEY))
        if last_weekly != week:
            await reset_period(PERIOD_WEEKLY)
            await r.set(LAST_WEEKLY_KEY, week, ex=86400 * 14)
            logger.info("Leaderboard weekly period reset (week=%s)", week)

        last_monthly = _redis_str(await r.get(LAST_MONTHLY_KEY))
        if last_monthly != month:
            await reset_period(PERIOD_MONTHLY)
            await r.set(LAST_MONTHLY_KEY, month, ex=86400 * 70)
            logger.info("Leaderboard monthly period reset (month=%s)", month)
    except Exception:
        logger.exception("Leaderboard reset tick failed")


async def _loop() -> None:
    logger.info("Leaderboard reset loop started (interval=%ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Leaderboard reset loop tick error")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_leaderboard_reset_loop() -> None:
    """Start the background leaderboard period reset loop."""
    task = asyncio.create_task(_loop())
    task.add_done_callback(lambda t: None if t.cancelled() else logger.warning("Leaderboard reset loop exited: %s", t.exception()))
