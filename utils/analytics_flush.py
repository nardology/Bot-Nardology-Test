from __future__ import annotations

"""Background flush loop for analytics.

This runs inside the bot process and periodically flushes Redis counters
into Postgres. It is intentionally lightweight:
  - only flushes guilds marked "dirty" for today and yesterday
  - uses SPOP as a work queue to avoid double work across shards
"""

import asyncio
import logging
import os

from utils.analytics import utc_day_str, pop_dirty_guilds, flush_day_to_db, GLOBAL_GUILD_ID


logger = logging.getLogger("analytics.flush")

_task: asyncio.Task | None = None


def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        v = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        v = default
    return max(min_value, v)


async def _flush_once() -> None:
    # Today + yesterday (UTC)
    now = utc_day_str()
    # 86400 seconds ago -> yesterday
    yesterday = utc_day_str(int(__import__("time").time()) - 86400)

    for day in (now, yesterday):
        guilds = await pop_dirty_guilds(day_utc=day, max_items=_get_env_int("ANALYTICS_FLUSH_BATCH", 200, min_value=1))
        # Always flush guild_id=0 (global rolls/counters) so /owner global total has data
        if GLOBAL_GUILD_ID not in guilds:
            guilds = [GLOBAL_GUILD_ID] + guilds
        for gid in guilds:
            try:
                await flush_day_to_db(day_utc=day, guild_id=int(gid))
            except Exception:
                logger.exception("flush failed for day=%s guild=%s", day, gid)


async def _loop() -> None:
    interval = _get_env_int("ANALYTICS_FLUSH_INTERVAL_S", 60, min_value=10)
    while True:
        try:
            await _flush_once()
        except Exception:
            logger.exception("analytics flush tick failed")
        await asyncio.sleep(interval)


def start_analytics_flush_loop() -> None:
    """Start the background flush loop once per process."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())
