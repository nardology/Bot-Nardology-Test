# utils/streak_reminders.py
"""Streak reminder DMs: opt-in storage, tracking, and DM helpers.

Two separate DM flows:
  1. Daily reward streak -- about /points daily claim cycle.
  2. Character talk streak -- about maintaining /talk conversations with specific characters.

Users can disable all streak DMs via /points reminders off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord

from utils.backpressure import get_redis_or_none
from utils.character_streak import get_all_character_streaks
from utils.character_registry import get_style
from core.kai_mascot import embed_kaihappy, embed_kailove

logger = logging.getLogger("bot.streak_reminders")

# --- Redis key prefixes ---

OPT_OUT_KEY_PREFIX = "streak_reminders:opt_out:"

# Daily reward streak sent flags
DAILY_REMINDER_SENT_PREFIX = "streak_reminders:reminder_sent:"
DAILY_WARNING_8H_SENT_PREFIX = "streak_reminders:warning8h_sent:"
DAILY_WARNING_1H_SENT_PREFIX = "streak_reminders:warning1h_sent:"
DAILY_ENDED_SENT_PREFIX = "streak_reminders:ended_sent:"

# Character talk streak sent flags
CHAR_REMINDER_SENT_PREFIX = "streak_reminders:char_reminder_sent:"
CHAR_WARNING_8H_SENT_PREFIX = "streak_reminders:char_warning8h_sent:"
CHAR_WARNING_1H_SENT_PREFIX = "streak_reminders:char_warning1h_sent:"
CHAR_ENDED_SENT_PREFIX = "streak_reminders:char_ended_sent:"

SENT_TTL = 60 * 60 * 48  # 48 hours so key expires after "today" is long past


def _day_utc(dt: datetime | None = None) -> str:
    t = (dt or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return t


# ──────────────────────────────────────────────
# Opt-in / opt-out
# ──────────────────────────────────────────────

async def get_streak_reminders_enabled(user_id: int) -> bool:
    """True if we should send streak reminder DMs; False if user opted out."""
    r = await get_redis_or_none()
    if r is None:
        return True  # degrade: allow reminders when Redis down
    key = f"{OPT_OUT_KEY_PREFIX}{int(user_id)}"
    try:
        val = await r.get(key)
        if val is None:
            return True
        s = val.decode("utf-8", errors="ignore") if isinstance(val, (bytes, bytearray)) else str(val)
        return s.strip().lower() not in ("1", "true", "yes", "on")
    except Exception:
        logger.exception("get_streak_reminders_enabled failed")
        return True


async def set_streak_reminders_enabled(user_id: int, enabled: bool) -> None:
    """Set whether to send streak reminder DMs. enabled=True means send; False = opt out."""
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{OPT_OUT_KEY_PREFIX}{int(user_id)}"
    try:
        if enabled:
            await r.delete(key)
        else:
            await r.set(key, "1")
    except Exception:
        logger.exception("set_streak_reminders_enabled failed")


# ──────────────────────────────────────────────
# Generic sent-flag helpers (Redis)
# ──────────────────────────────────────────────

async def _flag_sent(prefix: str, user_id: int, day: str | None = None, *, suffix: str = "") -> bool:
    """Check if a sent flag is set for this user today."""
    d = day or _day_utc()
    r = await get_redis_or_none()
    if r is None:
        return False
    key = f"{prefix}{int(user_id)}:{d}" + (f":{suffix}" if suffix else "")
    try:
        return await r.get(key) is not None
    except Exception:
        return False


async def _mark_sent(prefix: str, user_id: int, day: str | None = None, *, suffix: str = "") -> None:
    """Mark a sent flag for this user today."""
    d = day or _day_utc()
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{prefix}{int(user_id)}:{d}" + (f":{suffix}" if suffix else "")
    try:
        await r.set(key, "1", ex=SENT_TTL)
    except Exception:
        logger.exception("_mark_sent failed for %s", key)


# ── Daily reward streak sent flags ──

async def reminder_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(DAILY_REMINDER_SENT_PREFIX, user_id, day_utc)

async def mark_reminder_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(DAILY_REMINDER_SENT_PREFIX, user_id, day_utc)

async def warning_8h_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(DAILY_WARNING_8H_SENT_PREFIX, user_id, day_utc)

async def mark_warning_8h_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(DAILY_WARNING_8H_SENT_PREFIX, user_id, day_utc)

async def warning_1h_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(DAILY_WARNING_1H_SENT_PREFIX, user_id, day_utc)

async def mark_warning_1h_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(DAILY_WARNING_1H_SENT_PREFIX, user_id, day_utc)

async def ended_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(DAILY_ENDED_SENT_PREFIX, user_id, day_utc)

async def mark_ended_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(DAILY_ENDED_SENT_PREFIX, user_id, day_utc)

# Backward-compat aliases (old code used warning_sent_today / mark_warning_sent_today)
warning_sent_today = warning_8h_sent_today
mark_warning_sent_today = mark_warning_8h_sent_today

# ── Character talk streak sent flags ──

async def char_reminder_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(CHAR_REMINDER_SENT_PREFIX, user_id, day_utc)

async def mark_char_reminder_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(CHAR_REMINDER_SENT_PREFIX, user_id, day_utc)

async def char_warning_8h_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(CHAR_WARNING_8H_SENT_PREFIX, user_id, day_utc)

async def mark_char_warning_8h_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(CHAR_WARNING_8H_SENT_PREFIX, user_id, day_utc)

async def char_warning_1h_sent_today(user_id: int, day_utc: str | None = None) -> bool:
    return await _flag_sent(CHAR_WARNING_1H_SENT_PREFIX, user_id, day_utc)

async def mark_char_warning_1h_sent_today(user_id: int, day_utc: str | None = None) -> None:
    await _mark_sent(CHAR_WARNING_1H_SENT_PREFIX, user_id, day_utc)

async def char_ended_sent_today(user_id: int, style_id: str, day_utc: str | None = None) -> bool:
    return await _flag_sent(CHAR_ENDED_SENT_PREFIX, user_id, day_utc, suffix=style_id)

async def mark_char_ended_sent_today(user_id: int, style_id: str, day_utc: str | None = None) -> None:
    await _mark_sent(CHAR_ENDED_SENT_PREFIX, user_id, day_utc, suffix=style_id)


# TTL for break-keyed ended flags (30 days — long enough to outlast any dead streak)
_ENDED_BREAK_TTL = 60 * 60 * 24 * 30  # 30 days


async def char_ended_sent_for_break(user_id: int, style_id: str, last_talk_day: str) -> bool:
    """Check if we already sent an 'ended' DM for this specific streak break.

    Keyed by last_talk_day (when the user last talked to the character) rather
    than today's date, so the flag survives across calendar days and won't
    re-fire for the same break event.
    """
    r = await get_redis_or_none()
    if r is None:
        return False
    key = f"{CHAR_ENDED_SENT_PREFIX}{int(user_id)}:{style_id}:{last_talk_day}"
    try:
        return await r.get(key) is not None
    except Exception:
        return False


async def mark_char_ended_sent_for_break(user_id: int, style_id: str, last_talk_day: str) -> None:
    """Mark that we sent an 'ended' DM for this specific streak break."""
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{CHAR_ENDED_SENT_PREFIX}{int(user_id)}:{style_id}:{last_talk_day}"
    try:
        await r.set(key, "1", ex=_ENDED_BREAK_TTL)
    except Exception:
        logger.exception("mark_char_ended_sent_for_break failed for %s", key)


# ──────────────────────────────────────────────
# Character name helpers
# ──────────────────────────────────────────────

async def get_longest_streak_character_name(user_id: int) -> str:
    """Display name of the character this user has the longest streak with.

    Skips characters the user no longer owns (e.g. deleted/removed from inventory).
    Falls back to 'your character' if none found.
    """
    from utils.character_store import owns_style

    streaks = await get_all_character_streaks(user_id=user_id)
    if not streaks:
        return "your character"
    # Sort by streak length descending; pick the best one the user still owns
    for style_id in sorted(streaks, key=lambda k: streaks[k], reverse=True):
        style = get_style(style_id)
        if not style:
            continue
        try:
            if not await owns_style(user_id, style_id):
                continue
        except Exception:
            continue
        if getattr(style, "display_name", None):
            return style.display_name
    return "your character"


def _character_display_name(style_id: str) -> str:
    """Get the display name for a character, or fall back to the style_id."""
    style = get_style(style_id)
    if style and getattr(style, "display_name", None):
        return style.display_name
    return style_id


# ──────────────────────────────────────────────
# DM functions -- Daily reward streak (Type 1)
# ──────────────────────────────────────────────

async def send_after_claim_dm(
    bot: discord.Client,
    user_id: int,
    streak: int,
) -> None:
    """Send DM after daily claim: congratulates on claiming. No character mention. Best-effort."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        logger.debug("fetch_user for after-claim DM failed for %s", user_id)
        return
    if not user:
        return
    enabled = await get_streak_reminders_enabled(user_id)
    if not enabled:
        return
    text = (
        f"You claimed your daily reward! Come back tomorrow to keep your "
        f"**{streak}**-day streak going."
    )
    embed = embed_kailove(text, title="Daily claimed!")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled", user_id)
    except Exception:
        logger.exception("After-claim DM failed for user %s", user_id)


# ──────────────────────────────────────────────
# DM functions -- Character talk streak (Type 2)
# ──────────────────────────────────────────────

async def send_character_streak_started_dm(
    bot: discord.Client,
    user_id: int,
    style_id: str,
) -> None:
    """Send DM when a user starts a new character streak. Best-effort."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return
    if not user:
        return
    enabled = await get_streak_reminders_enabled(user_id)
    if not enabled:
        return
    char_name = _character_display_name(style_id)
    text = (
        f"You started a streak with **{char_name}**! "
        f"Talk to them again tomorrow to keep it going."
    )
    embed = embed_kaihappy(text, title="Character streak started!")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (char streak started)", user_id)
    except Exception:
        logger.exception("Character streak started DM failed for user %s", user_id)


async def send_character_streak_ended_dm(
    bot: discord.Client,
    user_id: int,
    style_id: str,
    streak: int,
) -> None:
    """Send DM when a character streak has ended. Best-effort."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return
    if not user:
        return
    enabled = await get_streak_reminders_enabled(user_id)
    if not enabled:
        return
    char_name = _character_display_name(style_id)
    text = (
        f"Your streak with **{char_name}** ({streak} days) has ended. "
        f"Unfortunately, character streaks cannot be restored."
    )
    try:
        from core.kai_mascot import embed_kaisad
        embed = embed_kaisad(text, title="Character streak ended")
    except Exception:
        embed = discord.Embed(title="Character streak ended", description=text, color=0xED4245)
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (char streak ended)", user_id)
    except Exception:
        logger.exception("Character streak ended DM failed for user %s", user_id)
