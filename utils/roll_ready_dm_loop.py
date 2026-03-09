# utils/roll_ready_dm_loop.py
"""Background loop: DM users when their next character roll is ready."""
from __future__ import annotations

import asyncio
import logging

import discord

from utils.roll_ready_dm import (
    get_pending_roll_ready_user_ids,
    get_roll_ready_dm_enabled,
    send_roll_ready_dm,
)

logger = logging.getLogger("bot.roll_ready_dm_loop")

CHECK_INTERVAL_SECONDS = 60  # run every minute


async def _tick(bot: discord.Client) -> None:
    user_ids = await get_pending_roll_ready_user_ids()
    sent = 0
    for uid in user_ids:
        try:
            if not await get_roll_ready_dm_enabled(uid):
                from utils.roll_ready_dm import clear_scheduled_roll_ready_dm
                await clear_scheduled_roll_ready_dm(uid)
                continue
            if await send_roll_ready_dm(bot, uid):
                sent += 1
            await asyncio.sleep(0.3)
        except Exception:
            logger.exception("Roll-ready DM tick failed for user %s", uid)
    if sent:
        logger.info("Roll-ready DMs sent: %d", sent)


async def _loop(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    logger.info("Roll-ready DM loop started (interval %ds)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Roll-ready DM loop tick error")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_roll_ready_dm_loop(bot: discord.Client) -> None:
    """Start the background roll-ready DM loop."""
    task = asyncio.create_task(_loop(bot))
    task.add_done_callback(
        lambda t: None if t.cancelled() else logger.warning("Roll-ready DM loop exited: %s", t.exception())
    )
