# utils/streak_reminder_loop.py
"""Background loop: two separate DM flows for daily and character streaks.

Type 1 -- Daily reward streak DMs:
  - Reminder:   14:00 UTC  (haven't claimed today)
  - Warning 8h: 16:00 UTC  (streak ends in 8 hours)
  - Warning 1h: 23:00 UTC  (streak ends in 1 hour)
  - Ended:      any window  (detected when streak breaks)

Type 2 -- Character talk streak DMs:
  - Reminder:   14:00 UTC  (haven't talked to streaked characters today)
  - Warning 8h: 16:00 UTC  (character streak ends in 8 hours)
  - Warning 1h: 23:00 UTC  (character streak ends in 1 hour)
  - Ended:      any window  (detected when character streak breaks)

Started DMs for character streaks are sent from talk.py, not from this loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from utils.points_store import get_eligible_reminder_user_ids, get_claim_status, is_streak_alive
from utils.character_streak import get_active_character_streaks_with_status
from utils.streak_reminders import (
    # Opt-in/out
    get_streak_reminders_enabled,
    # Daily sent flags
    reminder_sent_today,
    mark_reminder_sent_today,
    warning_8h_sent_today,
    mark_warning_8h_sent_today,
    warning_1h_sent_today,
    mark_warning_1h_sent_today,
    ended_sent_today,
    mark_ended_sent_today,
    # Character sent flags
    char_reminder_sent_today,
    mark_char_reminder_sent_today,
    char_warning_8h_sent_today,
    mark_char_warning_8h_sent_today,
    char_warning_1h_sent_today,
    mark_char_warning_1h_sent_today,
    char_ended_sent_today,
    mark_char_ended_sent_today,
    char_ended_sent_for_break,
    mark_char_ended_sent_for_break,
    # DM functions
    send_character_streak_ended_dm,
    _character_display_name,
)
from core.kai_mascot import embed_kaihappy, embed_kaisad
from utils.character_streak_dm import send_character_streak_dm

logger = logging.getLogger("bot.streak_reminder_loop")

# ─── Timing constants ───
REMINDER_UTC_HOUR = 14
REMINDER_UTC_MINUTE = 0

WARNING_8H_UTC_HOUR = 16   # 8 hours before midnight UTC
WARNING_8H_UTC_MINUTE = 0

WARNING_1H_UTC_HOUR = 23   # 1 hour before midnight UTC
WARNING_1H_UTC_MINUTE = 0

CHECK_INTERVAL_SECONDS = 60 * 15  # run every 15 minutes
WINDOW_MINUTES = 30  # consider "on time" if within this window after target


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _in_window(now: datetime, hour: int, minute: int) -> bool:
    """True if we're in the first WINDOW_MINUTES after (hour, minute) UTC."""
    if now.hour != hour:
        return False
    return minute <= now.minute < minute + WINDOW_MINUTES


# ──────────────────────────────────────────────
# Daily reward streak DM helpers (Type 1)
# ──────────────────────────────────────────────

async def _send_daily_reminder_dm(bot: discord.Client, user_id: int, streak_days: int) -> None:
    """Send 'don't forget to claim' reminder DM. No character mention."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for daily reminder failed for %s", user_id)
        return
    if not user:
        return
    text = (
        f"Don't forget to claim your daily reward today! "
        f"Your current streak is **{streak_days}** days."
    )
    embed = embed_kaihappy(text, title="Daily reminder")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (daily reminder)", user_id)
    except Exception:
        logger.exception("Daily reminder DM failed for user %s", user_id)


async def _send_daily_warning_dm(bot: discord.Client, user_id: int, streak_days: int, hours_left: int) -> None:
    """Send warning DM that daily streak is about to end."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for daily warning failed for %s", user_id)
        return
    if not user:
        return
    if hours_left <= 1:
        text = (
            f"Your daily streak of **{streak_days}** days will reset in **1 hour**! "
            f"Use `/daily` now before midnight UTC."
        )
    else:
        text = (
            f"Your daily streak of **{streak_days}** days will reset in **{hours_left} hours** "
            f"if you don't claim! Use `/daily` before midnight UTC."
        )
    embed = embed_kaihappy(text, title="Streak warning")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (daily warning)", user_id)
    except Exception:
        logger.exception("Daily warning DM failed for user %s", user_id)


async def _send_daily_ended_dm(bot: discord.Client, user_id: int, streak_days: int) -> None:
    """Send one-time 'your daily streak has ended' DM with restore info."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for daily ended failed for %s", user_id)
        return
    if not user:
        return
    text = (
        f"Your daily streak of **{streak_days}** days has ended. "
        f"You can restore it for **500 points** within 7 days using `/daily` "
        f"-- a **Restore** button will appear."
    )
    try:
        embed = embed_kaisad(text, title="Streak ended")
    except Exception:
        embed = discord.Embed(title="Streak ended", description=text, color=0xED4245)
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (daily ended)", user_id)
    except Exception:
        logger.exception("Daily ended DM failed for user %s", user_id)


# ──────────────────────────────────────────────
# Character talk streak DM helpers (Type 2)
# ──────────────────────────────────────────────

async def _send_char_reminder_dm(bot: discord.Client, user_id: int, chars: list[tuple[str, str, int]]) -> None:
    """Send character streak reminder DM. chars is a list of (style_id, char_name, streak_days)."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return
    if not user:
        return
    lines = []
    for _sid, char_name, streak in chars:
        lines.append(f"• **{char_name}** — {streak} day streak")
    text = (
        "Don't forget to talk to your streaked characters today!\n\n"
        + "\n".join(lines)
    )
    embed = embed_kaihappy(text, title="Character streak reminder")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception:
        logger.exception("Character reminder DM failed for user %s", user_id)


async def _send_char_warning_dm(bot: discord.Client, user_id: int, chars: list[tuple[str, str, int]], hours_left: int) -> None:
    """Send character streak warning DM. chars is list of (style_id, char_name, streak_days)."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return
    if not user:
        return
    lines = []
    for _sid, char_name, streak in chars:
        lines.append(f"• **{char_name}** — {streak} day streak")
    if hours_left <= 1:
        header = "Your character streaks will end in **1 hour**! Use `/talk` now.\n\n"
    else:
        header = f"Your character streaks will end in **{hours_left} hours** if you don't talk to them!\n\n"
    text = header + "\n".join(lines)
    embed = embed_kaihappy(text, title="Character streak warning")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception:
        logger.exception("Character warning DM failed for user %s", user_id)


# ──────────────────────────────────────────────
# Loop logic: Daily Reward Streaks
# ──────────────────────────────────────────────

async def _run_daily_reminders(bot: discord.Client) -> None:
    """14:00 UTC - Send daily claim reminders to eligible users who haven't claimed today."""
    user_ids = await get_eligible_reminder_user_ids()
    sent = 0
    ended = 0
    for uid in user_ids:
        try:
            claimed_today, _, streak = await get_claim_status(guild_id=0, user_id=uid)
            if claimed_today:
                continue
            if not await get_streak_reminders_enabled(uid):
                continue

            alive = await is_streak_alive(uid)

            if alive:
                if await reminder_sent_today(uid):
                    continue
                await _send_daily_reminder_dm(bot, uid, max(1, streak))
                await mark_reminder_sent_today(uid)
                sent += 1
            else:
                # Streak just broke -- send ended DM once
                if streak > 0 and not await ended_sent_today(uid):
                    await _send_daily_ended_dm(bot, uid, streak)
                    await mark_ended_sent_today(uid)
                    ended += 1

            await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Daily reminder tick failed for user %s", uid)
    if sent or ended:
        logger.info("Daily reminders sent: %s, ended DMs: %s", sent, ended)


async def _run_daily_warnings(bot: discord.Client, hours_left: int) -> None:
    """16:00 / 23:00 UTC - Send warning to users whose daily streak will end soon."""
    user_ids = await get_eligible_reminder_user_ids()
    sent = 0
    is_8h = hours_left > 1
    for uid in user_ids:
        try:
            claimed_today, _, streak = await get_claim_status(guild_id=0, user_id=uid)
            if claimed_today:
                continue
            if not await get_streak_reminders_enabled(uid):
                continue
            if not await is_streak_alive(uid):
                continue

            if is_8h:
                if await warning_8h_sent_today(uid):
                    continue
                await _send_daily_warning_dm(bot, uid, max(1, streak), hours_left)
                await mark_warning_8h_sent_today(uid)
            else:
                if await warning_1h_sent_today(uid):
                    continue
                await _send_daily_warning_dm(bot, uid, max(1, streak), hours_left)
                await mark_warning_1h_sent_today(uid)

            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Daily warning tick failed for user %s", uid)
    if sent:
        logger.info("Daily %sh warnings sent: %s users", hours_left, sent)


# ──────────────────────────────────────────────
# Pro character-voiced DM helper
# ──────────────────────────────────────────────

async def _try_character_streak_dm(
    bot: discord.Client,
    user_id: int,
    chars: list[tuple[str, str, int]],
    stage: str,
) -> None:
    """For Pro users, send an AI-generated in-character DM *only* from a
    character whose streak is actually at risk.

    If the user's selected character is among those needing a reminder, use
    that one.  Otherwise pick the at-risk character with the longest streak.
    """
    try:
        from utils.premium import get_premium_tier
        tier = await get_premium_tier(user_id)
        if tier != "pro":
            return

        if not chars:
            return

        from utils.character_store import load_state
        st = await load_state(user_id)
        selected = (st.active_style_id or "").strip().lower()

        at_risk_ids = {sid for sid, _name, _streak in chars}

        # Prefer the selected character if it's at risk; otherwise fall back
        # to whichever at-risk character has the longest streak.
        if selected and selected in at_risk_ids:
            chosen_id = selected
            chosen_streak = next(s for sid, _n, s in chars if sid == selected)
        else:
            best = max(chars, key=lambda c: c[2])
            chosen_id, _, chosen_streak = best

        await send_character_streak_dm(
            bot, user_id, chosen_id, stage, max(1, chosen_streak),
        )
    except Exception:
        logger.debug("Pro character streak DM failed for user %s", user_id, exc_info=True)


# ──────────────────────────────────────────────
# Loop logic: Character Talk Streaks
# ──────────────────────────────────────────────

async def _get_char_streak_users(bot: discord.Client) -> list[int]:
    """Get user IDs from the daily-eligible set (they have active wallets).

    We piggyback on the daily eligible list. Users who have character streaks
    but no wallet are rare; we'd need a separate scan for that. For now this
    covers the vast majority.
    """
    return await get_eligible_reminder_user_ids()


async def _run_character_reminders(bot: discord.Client) -> None:
    """14:00 UTC - Send character streak reminders."""
    user_ids = await _get_char_streak_users(bot)
    sent = 0
    for uid in user_ids:
        try:
            if not await get_streak_reminders_enabled(uid):
                continue
            if await char_reminder_sent_today(uid):
                continue

            streaks = await get_active_character_streaks_with_status(user_id=uid)
            if not streaks:
                continue

            # Find characters that are alive but NOT talked to today
            needs_reminder: list[tuple[str, str, int]] = []
            for style_id, (streak, last_talk, alive) in streaks.items():
                if alive and last_talk != datetime.now(timezone.utc).strftime("%Y%m%d"):
                    char_name = _character_display_name(style_id)
                    needs_reminder.append((style_id, char_name, streak))

            if needs_reminder:
                await _send_char_reminder_dm(bot, uid, needs_reminder)
                await mark_char_reminder_sent_today(uid)
                await _try_character_streak_dm(bot, uid, needs_reminder, "reminder")
                sent += 1
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Character reminder tick failed for user %s", uid)
    if sent:
        logger.info("Character streak reminders sent: %s users", sent)


async def _run_character_warnings(bot: discord.Client, hours_left: int) -> None:
    """16:00 / 23:00 UTC - Send character streak warnings."""
    user_ids = await _get_char_streak_users(bot)
    sent = 0
    is_8h = hours_left > 1
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    for uid in user_ids:
        try:
            if not await get_streak_reminders_enabled(uid):
                continue

            if is_8h:
                if await char_warning_8h_sent_today(uid):
                    continue
            else:
                if await char_warning_1h_sent_today(uid):
                    continue

            streaks = await get_active_character_streaks_with_status(user_id=uid)
            if not streaks:
                continue

            # Characters alive but not talked to today
            needs_warning: list[tuple[str, str, int]] = []
            for style_id, (streak, last_talk, alive) in streaks.items():
                if alive and last_talk != today_str:
                    char_name = _character_display_name(style_id)
                    needs_warning.append((style_id, char_name, streak))

            if needs_warning:
                await _send_char_warning_dm(bot, uid, needs_warning, hours_left)
                if is_8h:
                    await mark_char_warning_8h_sent_today(uid)
                    await _try_character_streak_dm(bot, uid, needs_warning, "warning8h")
                else:
                    await mark_char_warning_1h_sent_today(uid)
                    await _try_character_streak_dm(bot, uid, needs_warning, "warning1h")
                sent += 1
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Character warning tick failed for user %s", uid)
    if sent:
        logger.info("Character %sh warnings sent: %s users", hours_left, sent)


async def _run_character_ended(bot: discord.Client) -> None:
    """Check all users for character streaks that just broke and send ended DMs."""
    user_ids = await _get_char_streak_users(bot)
    sent = 0
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    for uid in user_ids:
        try:
            if not await get_streak_reminders_enabled(uid):
                continue

            streaks = await get_active_character_streaks_with_status(user_id=uid)
            if not streaks:
                continue

            for style_id, (streak, last_talk, alive) in streaks.items():
                if not alive and streak > 0:
                    # Streak just broke -- send ended DM once per break event.
                    # Key by last_talk (not today's date) so the flag survives
                    # across calendar days and won't re-fire for the same break.
                    break_key = last_talk or "unknown"
                    if await char_ended_sent_for_break(uid, style_id, break_key):
                        continue
                    await send_character_streak_ended_dm(bot, uid, style_id, streak)
                    await mark_char_ended_sent_for_break(uid, style_id, break_key)
                    sent += 1
                    await asyncio.sleep(0.3)
        except Exception:
            logger.exception("Character ended tick failed for user %s", uid)
    if sent:
        logger.info("Character streak ended DMs sent: %s", sent)


# ──────────────────────────────────────────────
# Main tick and loop
# ──────────────────────────────────────────────

async def _tick(bot: discord.Client) -> None:
    now = _now_utc()

    # 14:00 UTC -- daily reminders + character reminders
    if _in_window(now, REMINDER_UTC_HOUR, REMINDER_UTC_MINUTE):
        try:
            await _run_daily_reminders(bot)
        except Exception:
            logger.exception("Daily reminder run failed")
        try:
            await _run_character_reminders(bot)
        except Exception:
            logger.exception("Character reminder run failed")

    # 16:00 UTC -- 8-hour warnings (daily + character)
    if _in_window(now, WARNING_8H_UTC_HOUR, WARNING_8H_UTC_MINUTE):
        try:
            await _run_daily_warnings(bot, hours_left=8)
        except Exception:
            logger.exception("Daily 8h warning run failed")
        try:
            await _run_character_warnings(bot, hours_left=8)
        except Exception:
            logger.exception("Character 8h warning run failed")

    # 23:00 UTC -- 1-hour warnings (daily + character)
    if _in_window(now, WARNING_1H_UTC_HOUR, WARNING_1H_UTC_MINUTE):
        try:
            await _run_daily_warnings(bot, hours_left=1)
        except Exception:
            logger.exception("Daily 1h warning run failed")
        try:
            await _run_character_warnings(bot, hours_left=1)
        except Exception:
            logger.exception("Character 1h warning run failed")

    # Character ended checks run at every tick so we detect breaks promptly
    try:
        await _run_character_ended(bot)
    except Exception:
        logger.exception("Character ended run failed")


async def _loop(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    logger.info(
        "Streak reminder loop started (reminder %02d:%02d, "
        "warn-8h %02d:%02d, warn-1h %02d:%02d UTC)",
        REMINDER_UTC_HOUR, REMINDER_UTC_MINUTE,
        WARNING_8H_UTC_HOUR, WARNING_8H_UTC_MINUTE,
        WARNING_1H_UTC_HOUR, WARNING_1H_UTC_MINUTE,
    )
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Streak reminder loop tick error")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_streak_reminder_loop(bot: discord.Client) -> None:
    """Start the background streak reminder loop."""
    task = asyncio.create_task(_loop(bot))
    task.add_done_callback(lambda t: None if t.cancelled() else logger.warning("Streak reminder loop exited: %s", t.exception()))
