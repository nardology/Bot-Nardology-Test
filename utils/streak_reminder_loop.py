# utils/streak_reminder_loop.py
"""Background loop: two separate DM flows for daily and character streaks.

Type 1 -- Daily reward streak DMs:
  - Ready:      00:00 UTC  (daily reward is active — "your daily is ready to claim")
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

from utils.points_store import (
    get_eligible_reminder_user_ids,
    get_claim_status,
    is_streak_alive,
    STREAK_RESTORE_COST,
    STREAK_RESTORE_MIN_STREAK,
)
from utils.character_streak import get_active_character_streaks_with_status
from utils.streak_reminders import (
    OPT_OUT_ADVICE,
    # Opt-in/out
    get_streak_reminders_enabled,
    # Daily sent flags
    daily_ready_sent_today,
    mark_daily_ready_sent_today,
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
from utils.character_store import load_state
from commands.slash.points import DailyReminderDmView

logger = logging.getLogger("bot.streak_reminder_loop")

# ─── Timing constants ───
DAILY_READY_UTC_HOUR = 0   # Midnight UTC: "your daily is ready to claim"
DAILY_READY_UTC_MINUTE = 0
REMINDER_UTC_HOUR = 14
REMINDER_UTC_MINUTE = 0

WARNING_8H_UTC_HOUR = 16   # 8 hours before midnight UTC
WARNING_8H_UTC_MINUTE = 0

WARNING_1H_UTC_HOUR = 23   # 1 hour before midnight UTC
WARNING_1H_UTC_MINUTE = 0

CHECK_INTERVAL_SECONDS = 60 * 15  # run every 15 minutes
WINDOW_MINUTES = 60  # full hour: reminder 14:00-14:59, 8h warning 16:00-16:59, 1h warning 23:00-23:59 UTC


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

async def _send_daily_ready_dm(bot: discord.Client, user_id: int, streak_days: int) -> None:
    """Send 'your daily reward is ready to claim' DM when the new day is active (e.g. after midnight UTC)."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for daily ready DM failed for %s", user_id)
        return
    if not user:
        return
    text = (
        f"Your daily reward is ready to claim! "
        f"Use the button below to open your daily, then tap **Claim Daily** to keep your **{streak_days}**-day streak "
        f"(or use **/points daily** in any server).\n\n{OPT_OUT_ADVICE}"
    )
    embed = embed_kaihappy(text, title="Daily reward ready!")
    try:
        await user.send(embed=embed, view=DailyReminderDmView(bot=bot, user_id=user_id))
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (daily ready)", user_id)
    except Exception:
        logger.exception("Daily ready DM failed for user %s", user_id)


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
        f"Your current streak is **{streak_days}** days. "
        f"Use the button below to open your daily, then tap **Claim Daily**.\n\n{OPT_OUT_ADVICE}"
    )
    embed = embed_kaihappy(text, title="Daily reminder")
    try:
        await user.send(embed=embed, view=DailyReminderDmView(bot=bot, user_id=user_id))
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
            f"Use `/daily` now before midnight UTC.\n\n{OPT_OUT_ADVICE}"
        )
    else:
        text = (
            f"Your daily streak of **{streak_days}** days will reset in **{hours_left} hours** "
            f"if you don't claim! Use `/daily` before midnight UTC.\n\n{OPT_OUT_ADVICE}"
        )
    embed = embed_kaihappy(text, title="Streak warning")
    try:
        await user.send(embed=embed, view=DailyReminderDmView(bot=bot, user_id=user_id))
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (daily warning)", user_id)
    except Exception:
        logger.exception("Daily warning DM failed for user %s", user_id)


async def _send_daily_ended_dm(bot: discord.Client, user_id: int, streak_days: int) -> None:
    """Send one-time 'your daily streak has ended' DM. Restore option only for 14+ day streaks."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for daily ended failed for %s", user_id)
        return
    if not user:
        return
    text = f"Your daily streak of **{streak_days}** days has ended."
    if streak_days >= STREAK_RESTORE_MIN_STREAK:
        text += (
            f" You can restore it for **{STREAK_RESTORE_COST} points** within 7 days using `/daily` "
            f"-- a **Restore** button will appear."
        )
    text += f"\n\n{OPT_OUT_ADVICE}"
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
        + f"\n\n{OPT_OUT_ADVICE}"
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
    text = header + "\n".join(lines) + f"\n\n{OPT_OUT_ADVICE}"
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

async def _run_daily_ready(bot: discord.Client) -> None:
    """00:00 UTC (first hour) - Send 'daily is ready to claim' DMs to eligible users who haven't claimed today."""
    user_ids = await get_eligible_reminder_user_ids()
    sent = 0
    skipped_claimed = 0
    skipped_not_alive = 0
    skipped_reminders_off = 0
    skipped_already_sent = 0
    for uid in user_ids:
        try:
            claimed_today, _, streak = await get_claim_status(guild_id=0, user_id=uid)
            if claimed_today:
                skipped_claimed += 1
                continue
            if not await get_streak_reminders_enabled(uid):
                skipped_reminders_off += 1
                continue
            # If the streak has already broken, don't send "streak" pings.
            # They can still manually claim /points daily to start a new streak.
            if not await is_streak_alive(uid):
                skipped_not_alive += 1
                continue
            if await daily_ready_sent_today(uid):
                skipped_already_sent += 1
                continue
            await _send_daily_ready_dm(bot, uid, max(1, streak))
            await mark_daily_ready_sent_today(uid)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Daily ready tick failed for user %s", uid)
    if sent or skipped_claimed or skipped_reminders_off or skipped_already_sent:
        logger.info(
            "Daily ready DMs: sent=%s (skipped: claimed=%s not_alive=%s reminders_off=%s already_sent=%s)",
            sent, skipped_claimed, skipped_not_alive, skipped_reminders_off, skipped_already_sent,
        )


async def _run_daily_reminders(bot: discord.Client) -> None:
    """14:00 UTC - Send daily claim reminders to eligible users who haven't claimed today."""
    user_ids = await get_eligible_reminder_user_ids()
    eligible = len(user_ids)
    sent = 0
    ended = 0
    skipped_claimed = 0
    skipped_reminders_off = 0
    skipped_already_sent = 0
    for uid in user_ids:
        try:
            claimed_today, _, streak = await get_claim_status(guild_id=0, user_id=uid)
            if claimed_today:
                skipped_claimed += 1
                continue
            if not await get_streak_reminders_enabled(uid):
                skipped_reminders_off += 1
                continue

            alive = await is_streak_alive(uid)

            if alive:
                if await reminder_sent_today(uid):
                    skipped_already_sent += 1
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
    logger.info(
        "Daily reminders: eligible=%s sent=%s ended=%s (skipped: claimed=%s reminders_off=%s already_sent=%s)",
        eligible, sent, ended, skipped_claimed, skipped_reminders_off, skipped_already_sent,
    )


async def _run_daily_warnings(bot: discord.Client, hours_left: int) -> None:
    """16:00 / 23:00 UTC - Send warning to users whose daily streak will end soon."""
    user_ids = await get_eligible_reminder_user_ids()
    eligible = len(user_ids)
    sent = 0
    is_8h = hours_left > 1
    skipped_claimed = 0
    skipped_reminders_off = 0
    skipped_not_alive = 0
    skipped_already_sent = 0
    for uid in user_ids:
        try:
            claimed_today, _, streak = await get_claim_status(guild_id=0, user_id=uid)
            if claimed_today:
                skipped_claimed += 1
                continue
            if not await get_streak_reminders_enabled(uid):
                skipped_reminders_off += 1
                continue
            if not await is_streak_alive(uid):
                skipped_not_alive += 1
                continue

            if is_8h:
                if await warning_8h_sent_today(uid):
                    skipped_already_sent += 1
                    continue
                await _send_daily_warning_dm(bot, uid, max(1, streak), hours_left)
                await mark_warning_8h_sent_today(uid)
            else:
                if await warning_1h_sent_today(uid):
                    skipped_already_sent += 1
                    continue
                await _send_daily_warning_dm(bot, uid, max(1, streak), hours_left)
                await mark_warning_1h_sent_today(uid)

            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Daily warning tick failed for user %s", uid)
    logger.info(
        "Daily %sh warnings: eligible=%s sent=%s (skipped: claimed=%s off=%s not_alive=%s already_sent=%s)",
        hours_left, eligible, sent, skipped_claimed, skipped_reminders_off, skipped_not_alive, skipped_already_sent,
    )


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
    skipped_no_selected = 0
    for uid in user_ids:
        try:
            if not await get_streak_reminders_enabled(uid):
                continue
            if await char_reminder_sent_today(uid):
                continue

            # Only remind for the user's SELECTED character.
            try:
                st = await load_state(uid)
                selected = (getattr(st, "active_style_id", "") or "").strip().lower()
            except Exception:
                selected = ""
            if not selected:
                skipped_no_selected += 1
                continue

            streaks = await get_active_character_streaks_with_status(user_id=uid)
            if not streaks or selected not in streaks:
                continue

            streak, last_talk, alive = streaks[selected]
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            if alive and last_talk != today:
                char_name = _character_display_name(selected)
                needs_reminder = [(selected, char_name, streak)]
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

            # Only warn for the user's SELECTED character.
            try:
                st = await load_state(uid)
                selected = (getattr(st, "active_style_id", "") or "").strip().lower()
            except Exception:
                selected = ""
            if not selected:
                continue

            streaks = await get_active_character_streaks_with_status(user_id=uid)
            if not streaks or selected not in streaks:
                continue

            streak, last_talk, alive = streaks[selected]
            if alive and last_talk != today_str:
                needs_warning = [(selected, _character_display_name(selected), streak)]
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
    for uid in user_ids:
        try:
            if not await get_streak_reminders_enabled(uid):
                continue

            # Only end-notify for the user's SELECTED character.
            try:
                st = await load_state(uid)
                selected = (getattr(st, "active_style_id", "") or "").strip().lower()
            except Exception:
                selected = ""
            if not selected:
                continue

            streaks = await get_active_character_streaks_with_status(user_id=uid)
            if not streaks or selected not in streaks:
                continue

            streak, last_talk, alive = streaks[selected]
            if not alive and streak > 0:
                # Streak just broke -- send ended DM once per break event.
                break_key = last_talk or "unknown"
                if await char_ended_sent_for_break(uid, selected, break_key):
                    continue
                await send_character_streak_ended_dm(bot, uid, selected, streak)
                await mark_char_ended_sent_for_break(uid, selected, break_key)
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

    # 00:00 UTC -- "your daily reward is ready to claim" DMs
    if _in_window(now, DAILY_READY_UTC_HOUR, DAILY_READY_UTC_MINUTE):
        logger.info("Streak reminder loop: in 00:00 UTC window — running daily ready DMs")
        try:
            await _run_daily_ready(bot)
        except Exception:
            logger.exception("Daily ready run failed")

    # 14:00 UTC -- daily reminders + character reminders ("don't forget to claim" message)
    if _in_window(now, REMINDER_UTC_HOUR, REMINDER_UTC_MINUTE):
        logger.info("Streak reminder loop: in 14:00 UTC window — running daily reminders")
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
        logger.info("Streak reminder loop: in 16:00 UTC window — running 8h warnings")
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
        logger.info("Streak reminder loop: in 23:00 UTC window — running 1h warnings")
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
