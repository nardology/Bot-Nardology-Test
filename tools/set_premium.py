"""Owner-only helper to set premium tier for a user.

Usage:
  python tools/set_premium.py <USER_ID> pro
  python tools/set_premium.py <USER_ID> free

This edits the user_premium_entitlements table directly.
"""

import sys
import asyncio


async def main():
    if len(sys.argv) != 3:
        print("Usage: python tools/set_premium.py <USER_ID> <free|pro>")
        return 2
    user_id = int(sys.argv[1])
    tier = sys.argv[2].lower().strip()
    if tier not in {"free", "pro"}:
        print("Tier must be 'free' or 'pro'")
        return 2

    from utils.db import get_sessionmaker
    from utils.models import UserPremiumEntitlement
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    Session = get_sessionmaker()
    async with Session() as session:
        ent = await session.get(UserPremiumEntitlement, user_id)
        if ent:
            ent.tier = tier
            ent.source = "manual"
            ent.updated_at = now
        else:
            ent = UserPremiumEntitlement(
                user_id=user_id,
                tier=tier,
                source="manual",
                updated_at=now,
            )
            session.add(ent)
        await session.commit()

    print(f"OK: user {user_id} premium_tier={tier}")
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
