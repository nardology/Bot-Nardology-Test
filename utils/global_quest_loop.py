"""Periodically resolve global quests (time expiry) without requiring a /talk."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("bot.global_quest_loop")


async def tick_global_quest_resolutions() -> None:
    try:
        from utils.global_quest import resolve_event_if_needed
        from utils.models import GlobalQuestEvent
        from utils.db import get_sessionmaker

        try:
            from sqlalchemy import select  # type: ignore
        except Exception:
            return

        Session = get_sessionmaker()
        async with Session() as session:
            res = await session.execute(
                select(GlobalQuestEvent.id).where(
                    GlobalQuestEvent.status == "active",
                    GlobalQuestEvent.resolution_applied == False,  # noqa: E712
                )
            )
            ids = [int(r[0]) for r in res.all()]
        for eid in ids:
            try:
                await resolve_event_if_needed(event_id=eid)
            except Exception:
                logger.debug("resolve_event_if_needed failed id=%s", eid, exc_info=True)
    except Exception:
        logger.exception("tick_global_quest_resolutions failed")


def start_global_quest_loop() -> None:
    async def _runner() -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await tick_global_quest_resolutions()
            except Exception:
                logger.exception("global quest loop tick failed")
            await asyncio.sleep(300)

    asyncio.create_task(_runner())
    logger.info("global_quest_loop started (5 min tick)")
