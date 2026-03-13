# utils/start_required.py
"""Require /start before using commands that log data (roll, feedback, daily, bond, etc.)."""
from __future__ import annotations

import logging

import discord
from discord import app_commands

from utils.backpressure import get_redis_or_none

logger = logging.getLogger("bot.start_required")

STARTED_KEY_PREFIX = "started:"
STARTED_TTL = 86400 * 365  # 1 year


async def has_used_start(user_id: int) -> bool:
    """True if the user has run /start at least once."""
    r = await get_redis_or_none()
    if r is None:
        return True  # degrade: allow when Redis down
    key = f"{STARTED_KEY_PREFIX}{int(user_id)}"
    try:
        val = await r.get(key)
        return val is not None
    except Exception:
        logger.exception("has_used_start failed")
        return True


async def mark_started(user_id: int) -> None:
    """Record that the user has completed /start."""
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{STARTED_KEY_PREFIX}{int(user_id)}"
    try:
        await r.set(key, "1", ex=STARTED_TTL)
    except Exception:
        logger.exception("mark_started failed")


def require_start():
    """app_commands.check: user must have run /start before using this command."""

    async def predicate(interaction: discord.Interaction) -> bool:
        msg = (
            "You need to run **/start** first to use this command. Use **/start** in a server to get started!"
        )
        try:
            if not interaction.guild:
                return True  # let command handle DM
            uid = int(interaction.user.id)
            if await has_used_start(uid):
                return True
            try:
                await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                # Response may be expired or already used; try defer + followup so user always sees the message
                logger.warning("require_start: response.send_message failed, trying defer + followup")
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer(ephemeral=True)
                    await interaction.followup.send(msg, ephemeral=True)
                except Exception as e2:
                    logger.exception("require_start followup also failed: %s", e2)
            return False
        except Exception:
            logger.exception("require_start predicate failed")
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                await interaction.followup.send(
                    "Something went wrong. Please try **/start** first or try again in a moment.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return False

    return app_commands.check(predicate)
