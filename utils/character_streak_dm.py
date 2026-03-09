# utils/character_streak_dm.py
"""Pro-only feature: AI-generated in-character DMs for character streak reminders.

When KAI sends a character streak reminder/warning, Pro users also receive a
short message *from their selected character*, written in-character using the
AI gateway with a minimal token budget.

Three escalation tiers:
  1. reminder  (casual)  -- character mentions something they'd like to talk about
  2. warning8h (concerned) -- character expresses they miss the user
  3. warning1h (urgent)   -- character pleads not to lose the streak
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from utils.character_registry import get_style
from utils.bonds_store import get_bond
from utils.bonds import level_from_xp

if TYPE_CHECKING:
    from utils.character_registry import StyleDef

logger = logging.getLogger("bot.character_streak_dm")

_STAGE_PROMPTS = {
    "reminder": (
        "You haven't spoken to the user today. Send a SHORT casual message (1-2 sentences) "
        "mentioning something you'd like to talk about or are excited about. "
        "Stay in character. Do NOT mention 'streaks' or game mechanics."
    ),
    "warning8h": (
        "It's been a while since the user talked to you today, and you miss them. "
        "Send a SHORT concerned message (1-2 sentences) expressing that you miss them. "
        "Reference something you've enjoyed talking about. Stay in character. "
        "Do NOT mention 'streaks' or game mechanics."
    ),
    "warning1h": (
        "The user hasn't talked to you all day and your time is almost up. "
        "Send a SHORT urgent message (1-2 sentences) pleading with them in your own voice. "
        "Be emotional and authentic to your personality. Stay in character. "
        "Do NOT mention 'streaks' or game mechanics."
    ),
}


def _build_system_prompt(style: "StyleDef", stage: str, streak_days: int, bond_level: int) -> str:
    """Build a minimal system prompt from the character's persona fields."""
    parts = [f"You are {style.display_name}."]

    if style.prompt:
        parts.append(style.prompt)
    if style.speech_style:
        parts.append(f"Speech style: {style.speech_style}")
    if style.catchphrases:
        parts.append(f"Catchphrases: {', '.join(style.catchphrases[:3])}")

    stage_prompt = _STAGE_PROMPTS.get(stage, _STAGE_PROMPTS["reminder"])

    if stage == "reminder" and style.likes:
        stage_prompt += f" Your interests include: {', '.join(style.likes[:3])}."
    elif stage == "warning8h" and style.desires:
        stage_prompt += f" Things you care about: {', '.join(style.desires[:3])}."
    elif stage == "warning1h" and style.fears:
        stage_prompt += f" Your fears include: {', '.join(style.fears[:2])}."

    parts.append(stage_prompt)
    parts.append(
        f"You've known this person for {streak_days} days. "
        f"Your bond level is {bond_level}. "
        "Respond with ONLY your message, nothing else. Keep it under 200 characters."
    )

    return " ".join(parts)


def _color_from_style(style: "StyleDef") -> int:
    """Extract a Discord-compatible color int from the style."""
    c = style.color
    if isinstance(c, int):
        return c
    if isinstance(c, str):
        try:
            return int(c.lstrip("#"), 16)
        except (ValueError, TypeError):
            pass
    return 0x5865F2


async def _is_dm_sent(user_id: int, style_id: str, stage: str) -> bool:
    """Check Redis flag to avoid duplicate sends (24h TTL)."""
    try:
        from utils.backpressure import get_redis_or_none
        r = await get_redis_or_none()
        if r is None:
            return False
        key = f"char_streak_dm:{user_id}:{style_id}:{stage}"
        return bool(await r.get(key))
    except Exception:
        return False


async def _mark_dm_sent(user_id: int, style_id: str, stage: str) -> None:
    try:
        from utils.backpressure import get_redis_or_none
        r = await get_redis_or_none()
        if r is None:
            return
        key = f"char_streak_dm:{user_id}:{style_id}:{stage}"
        await r.set(key, "1", ex=24 * 3600)
    except Exception:
        pass


async def send_character_streak_dm(
    bot: discord.Client,
    user_id: int,
    style_id: str,
    stage: str,
    streak_days: int,
) -> bool:
    """Generate and send an in-character streak DM. Returns True if sent.

    Requires: user is Pro, has an active character streak, and hasn't
    already received this stage's DM today.
    """
    if await _is_dm_sent(user_id, style_id, stage):
        return False

    style = get_style(style_id)
    if style is None:
        return False

    bond_level = 1
    try:
        bond = await get_bond(guild_id=0, user_id=user_id, style_id=style_id)
        if bond:
            bond_level = level_from_xp(int(getattr(bond, "xp", 0) or 0))
    except Exception:
        pass

    system = _build_system_prompt(style, stage, streak_days, bond_level)

    try:
        from utils.ai_client import generate_text
        import config

        model = getattr(config, "OPENAI_MODEL_FREE", None) or getattr(config, "OPENAI_MODEL", "gpt-4.1-nano")

        result = await generate_text(
            system=system,
            user="Send your message now.",
            max_output_tokens=100,
            timeout_s=10.0,
            model=model,
        )
        text = str(result).strip()
        if not text:
            return False
    except Exception:
        logger.debug("AI call failed for character streak DM user=%s char=%s", user_id, style_id, exc_info=True)
        return False

    embed = discord.Embed(
        description=text,
        color=_color_from_style(style),
    )
    embed.set_author(name=style.display_name)
    if style.image_url:
        embed.set_thumbnail(url=style.image_url)
    embed.set_footer(text=f"{streak_days}-day streak · Bond level {bond_level}")

    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if not user:
            return False
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug("User %s has DMs disabled (character streak DM)", user_id)
        return False
    except Exception:
        logger.debug("Character streak DM send failed for user %s", user_id, exc_info=True)
        return False

    await _mark_dm_sent(user_id, style_id, stage)
    logger.info("Character streak DM sent: user=%s char=%s stage=%s", user_id, style_id, stage)
    return True
