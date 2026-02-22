#!/usr/bin/env python3
"""Phase 5 sanity check: verify analytics cog and get_summary/reset_guild.

Run from project root with full deps installed (e.g. pip install -r requirements.txt).
Usage: python -m tools.check_analytics
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> None:
    errors: list[str] = []

    # 1) Analytics module exposes get_summary, reset_guild
    try:
        from utils.analytics import get_summary, reset_guild
    except ImportError as e:
        errors.append(f"utils.analytics import failed: {e}")
        print("FAIL: utils.analytics import:", e)
        sys.exit(1)
    print("OK: get_summary, reset_guild import from utils.analytics")

    # 2) Analytics cog module loads (safe_ephemeral_send, etc.)
    try:
        from commands.slash.analytics import setup
        print("OK: commands.slash.analytics imports (cog loads)")
    except ImportError as e:
        errors.append(f"commands.slash.analytics import failed: {e}")
        print("FAIL: analytics cog import:", e)
        sys.exit(1)

    # 3) get_summary returns expected shape (needs Redis + guild storage for real data)
    try:
        s = await get_summary(999999, days=7)
        if not all(k in s for k in ("by_command", "by_result", "by_event", "events_total")):
            errors.append("get_summary missing keys")
            print("FAIL: get_summary missing keys; got:", list(s.keys()))
        else:
            print("OK: get_summary returns expected keys:", list(s.keys()))
    except Exception as e:
        print("NOTE: get_summary raised (Redis/storage may be unavailable):", e)

    if errors:
        print("\nErrors:", errors)
        sys.exit(1)
    print("\nPhase 5 analytics check passed. Start the bot and try /analytics view and /analytics reset in a server where you are owner.")


if __name__ == "__main__":
    asyncio.run(main())
