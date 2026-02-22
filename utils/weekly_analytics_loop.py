# utils/weekly_analytics_loop.py
"""Background loop: weekly analytics DM to bot owners.

Runs every hour, checks if it's the target day/time (Monday 10:00 UTC).
Aggregates last 7 days of metrics from Postgres and DMs each bot owner.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import discord

from config import BOT_OWNER_IDS
from utils.backpressure import get_redis_or_none
from utils.metrics import estimate_ai_cost_usd_from_tokens

logger = logging.getLogger("bot.weekly_analytics_loop")

# Schedule: Monday 10:00 UTC
TARGET_WEEKDAY = 0  # Monday (datetime.weekday() == 0)
TARGET_HOUR = 10
CHECK_INTERVAL_SECONDS = 60 * 60  # check every hour
REDIS_LAST_SENT_KEY = "weekly_analytics:last_sent"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _week_id(dt: datetime | None = None) -> str:
    """ISO week identifier, e.g. '2026-W07'."""
    d = dt or _now_utc()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def _already_sent_this_week() -> bool:
    """Check Redis to see if we already sent the weekly DM for the current ISO week."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        val = await r.get(REDIS_LAST_SENT_KEY)
        if val is None:
            return False
        s = val.decode("utf-8", errors="ignore") if isinstance(val, (bytes, bytearray)) else str(val)
        return s.strip() == _week_id()
    except Exception:
        return False


async def _mark_sent_this_week() -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        # Expire after 10 days (well past current week)
        await r.set(REDIS_LAST_SENT_KEY, _week_id(), ex=86400 * 10)
    except Exception:
        logger.exception("mark_sent_this_week failed")


async def _aggregate_last_7_days() -> dict:
    """Query Postgres AnalyticsDailyMetric for the last 7 UTC days. Returns totals dict."""
    try:
        from sqlalchemy import select, func  # type: ignore
    except Exception:
        return {}

    try:
        from utils.db import get_sessionmaker
        from utils.models import AnalyticsDailyMetric
        from utils.analytics import utc_day_str
    except Exception:
        return {}

    now_ts = int(time.time())
    days = [utc_day_str(now_ts - 86400 * i) for i in range(0, 7)]

    try:
        Session = get_sessionmaker()
        async with Session() as session:
            rows = await session.execute(
                select(
                    AnalyticsDailyMetric.metric,
                    func.sum(AnalyticsDailyMetric.value),
                )
                .where(AnalyticsDailyMetric.day_utc.in_(days))
                .group_by(AnalyticsDailyMetric.metric)
            )
            data = rows.all()

            # Top 3 guilds by tokens
            rows2 = await session.execute(
                select(
                    AnalyticsDailyMetric.guild_id,
                    func.sum(AnalyticsDailyMetric.value).label("v"),
                )
                .where(AnalyticsDailyMetric.day_utc.in_(days))
                .where(AnalyticsDailyMetric.metric == "daily_ai_token_budget")
                .group_by(AnalyticsDailyMetric.guild_id)
                .order_by(func.sum(AnalyticsDailyMetric.value).desc())
                .limit(3)
            )
            top_guilds = [(int(gid), int(v or 0)) for (gid, v) in rows2.all()]

        totals = {str(m): int(v or 0) for (m, v) in data}
        return {
            "days": days,
            "totals": totals,
            "top_guilds_by_tokens": top_guilds,
        }
    except Exception:
        logger.exception("Failed to aggregate weekly analytics")
        return {}


def _build_embed(data: dict, guild_count: int) -> discord.Embed:
    """Build the weekly analytics embed."""
    totals = data.get("totals") or {}
    days = data.get("days") or []
    top_guilds = data.get("top_guilds_by_tokens") or []

    total_tokens = int(totals.get("daily_ai_token_budget", 0) or 0)
    est_cost = estimate_ai_cost_usd_from_tokens(total_tokens)

    period = f"{days[-1]} to {days[0]}" if len(days) >= 2 else "last 7 days"

    e = discord.Embed(
        title="Weekly Bot Analytics",
        description=f"Report for **{period}** (UTC)",
        color=0x3498DB,
    )

    # Usage section
    usage_lines = [
        f"AI calls (talk+scene): **{totals.get('daily_ai_calls', 0)}**",
        f"Talk calls: **{totals.get('daily_talk_calls', 0)}**",
        f"Scene calls: **{totals.get('daily_scene_calls', 0)}**",
        f"Character rolls: **{totals.get('daily_rolls', 0)}**",
        f"Active users (sum): **{totals.get('daily_active_users', 0)}**",
        f"5-pulls: **{totals.get('pull_5', 0)}** | 10-pulls: **{totals.get('pull_10', 0)}**",
    ]
    e.add_field(name="Usage", value="\n".join(usage_lines), inline=False)

    # Cost section
    cost_lines = [
        f"Total tokens: **{total_tokens:,}**",
        f"Estimated cost: **${est_cost:.4f}** USD",
    ]
    e.add_field(name="Cost", value="\n".join(cost_lines), inline=False)

    # Premium section
    premium_lines = [
        f"Trial starts: **{totals.get('trial_start', 0)}**",
        f"Conversions (Pro): **{totals.get('conversion', 0)}**",
        "Revenue tracking: *coming soon*",
    ]
    e.add_field(name="Premium", value="\n".join(premium_lines), inline=False)

    # Guilds section
    guild_lines = [f"Total guilds: **{guild_count}**"]
    if top_guilds:
        for i, (gid, tokens) in enumerate(top_guilds, 1):
            guild_lines.append(f"{i}. `{gid}` -- {tokens:,} tokens")
    else:
        guild_lines.append("(no guild token data)")
    e.add_field(name="Top guilds by tokens", value="\n".join(guild_lines), inline=False)

    e.set_footer(text="Weekly analytics DM -- sent every Monday at 10:00 UTC")
    e.timestamp = _now_utc()

    return e


async def _send_weekly_dm(bot: discord.Client) -> None:
    """Aggregate analytics and DM each bot owner."""
    if not BOT_OWNER_IDS:
        logger.debug("No BOT_OWNER_IDS configured; skipping weekly analytics DM.")
        return

    data = await _aggregate_last_7_days()
    if not data:
        logger.warning("Weekly analytics: no data aggregated; skipping DM.")
        return

    guild_count = len(list(getattr(bot, "guilds", []) or []))
    embed = _build_embed(data, guild_count)

    sent = 0
    for owner_id in BOT_OWNER_IDS:
        try:
            user = bot.get_user(int(owner_id)) or await bot.fetch_user(int(owner_id))
            if not user:
                continue
            await user.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            logger.debug("Owner %s has DMs disabled (weekly analytics)", owner_id)
        except Exception:
            logger.exception("Weekly analytics DM failed for owner %s", owner_id)

    if sent:
        logger.info("Weekly analytics DM sent to %s owner(s)", sent)
    await _mark_sent_this_week()


async def _tick(bot: discord.Client) -> None:
    now = _now_utc()
    # Only run on target weekday + hour
    if now.weekday() != TARGET_WEEKDAY:
        return
    if now.hour != TARGET_HOUR:
        return
    if await _already_sent_this_week():
        return
    try:
        await _send_weekly_dm(bot)
    except Exception:
        logger.exception("Weekly analytics DM send failed")


async def _loop(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    logger.info("Weekly analytics loop started (Monday %s:00 UTC)", TARGET_HOUR)
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Weekly analytics loop tick error")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_weekly_analytics_loop(bot: discord.Client) -> None:
    """Start the background weekly analytics DM loop."""
    task = asyncio.create_task(_loop(bot))
    task.add_done_callback(lambda t: logger.warning("Weekly analytics loop exited: %s", t.exception()))
