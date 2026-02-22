# utils/jobs/scene_cleanup.py
from __future__ import annotations

import asyncio
import logging

from utils.scene_store import expire_stale_scenes_global

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scene_cleanup")


async def main() -> None:
    # Expire anything stale across all guilds/channels
    n = await expire_stale_scenes_global(ttl_seconds=48 * 3600, batch_limit=200)
    logger.info("Expired %s stale scenes", n)


if __name__ == "__main__":
    asyncio.run(main())
