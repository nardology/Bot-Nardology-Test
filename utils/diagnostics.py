from __future__ import annotations

"""
DB-related utilities (legacy).

This Redis-first build keeps this file to avoid breaking old imports, but it does not
require SQLAlchemy at runtime. If you want to use these DB tools, install SQLAlchemy
and the appropriate async DB driver, and then re-implement these commands.
"""

import logging

log = logging.getLogger(__name__)


async def run(*args, **kwargs):
    raise RuntimeError(
        "DB utilities are disabled in the Redis-first build. "
        "If you still need them, re-enable SQLAlchemy dependencies and restore logic."
    )
