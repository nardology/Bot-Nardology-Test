"""GDPR-compliant user data export and deletion.

Data layer only — no Discord dependencies.  Called by the /privacy slash command cog.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from utils.db import get_sessionmaker

try:
    from sqlalchemy import select, update, delete  # type: ignore
except Exception:  # pragma: no cover
    select = update = delete = None  # type: ignore

log = logging.getLogger("privacy")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

async def export_user_data(user_id: int) -> dict:
    """Gather every row tied to *user_id* across all Postgres tables.

    Returns a JSON-serialisable dict.
    """
    from utils.models import (
        CharacterUserState,
        CharacterOwnedStyle,
        CharacterCustomStyle,
        BondState,
        PointsWallet,
        PointsLedger,
        QuestProgress,
        QuestClaim,
        UserPremiumEntitlement,
        StripeCustomer,
        PremiumGift,
        CharacterMemory,
        UserFirstSeen,
        UserActivityDay,
    )

    uid = int(user_id)
    data: dict = {
        "user_id": uid,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }

    Session = get_sessionmaker()
    async with Session() as session:
        # CharacterUserState (single row keyed on user_id)
        row = await session.get(CharacterUserState, uid)
        if row:
            data["character_state"] = _row_to_dict(row, exclude={"_sa_instance_state"})

        # CharacterOwnedStyle
        data["owned_styles"] = await _query_all(
            session, select(CharacterOwnedStyle).where(CharacterOwnedStyle.user_id == uid)
        )

        # CharacterCustomStyle
        data["custom_styles"] = await _query_all(
            session, select(CharacterCustomStyle).where(CharacterCustomStyle.user_id == uid)
        )

        # BondState
        data["bonds"] = await _query_all(
            session, select(BondState).where(BondState.user_id == uid)
        )

        # PointsWallet
        data["points_wallets"] = await _query_all(
            session, select(PointsWallet).where(PointsWallet.user_id == uid)
        )

        # PointsLedger (could be large — include anyway for completeness)
        data["points_ledger"] = await _query_all(
            session, select(PointsLedger).where(PointsLedger.user_id == uid)
        )

        # QuestProgress
        data["quest_progress"] = await _query_all(
            session, select(QuestProgress).where(QuestProgress.user_id == uid)
        )

        # QuestClaim
        data["quest_claims"] = await _query_all(
            session, select(QuestClaim).where(QuestClaim.user_id == uid)
        )

        # UserPremiumEntitlement
        ent = await session.get(UserPremiumEntitlement, uid)
        if ent:
            data["premium_entitlement"] = _row_to_dict(ent)

        # StripeCustomer
        data["stripe_customers"] = await _query_all(
            session, select(StripeCustomer).where(StripeCustomer.discord_user_id == uid)
        )

        # PremiumGift (sent and received)
        data["premium_gifts_sent"] = await _query_all(
            session, select(PremiumGift).where(PremiumGift.gifter_user_id == uid)
        )
        data["premium_gifts_received"] = await _query_all(
            session, select(PremiumGift).where(PremiumGift.recipient_user_id == uid)
        )

        # CharacterMemory
        data["character_memories"] = await _query_all(
            session, select(CharacterMemory).where(CharacterMemory.user_id == uid)
        )

        # UserFirstSeen
        data["first_seen"] = await _query_all(
            session, select(UserFirstSeen).where(UserFirstSeen.user_id == uid)
        )

        # UserActivityDay
        data["activity_days"] = await _query_all(
            session, select(UserActivityDay).where(UserActivityDay.user_id == uid)
        )

    return data


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

async def delete_user_data(user_id: int) -> dict:
    """Delete (or anonymise) all data for *user_id*.

    Returns a summary dict with per-table counts of affected rows.

    * Most tables: hard DELETE.
    * PointsLedger: anonymised (user_id set to 0) to preserve financial audit trail.
    * Redis ephemeral keys: best-effort scan + delete.

    Does NOT cancel Stripe subscriptions — the caller should direct the user
    to the billing portal first.
    """
    from utils.models import (
        CharacterUserState,
        CharacterOwnedStyle,
        CharacterCustomStyle,
        BondState,
        PointsWallet,
        PointsLedger,
        QuestProgress,
        QuestClaim,
        UserPremiumEntitlement,
        StripeCustomer,
        PremiumGift,
        CharacterMemory,
        UserFirstSeen,
        UserActivityDay,
    )

    uid = int(user_id)
    summary: dict[str, int] = {}

    Session = get_sessionmaker()
    async with Session() as session:
        # Hard deletes
        for label, stmt in [
            ("character_state", delete(CharacterUserState).where(CharacterUserState.user_id == uid)),
            ("owned_styles", delete(CharacterOwnedStyle).where(CharacterOwnedStyle.user_id == uid)),
            ("custom_styles", delete(CharacterCustomStyle).where(CharacterCustomStyle.user_id == uid)),
            ("bonds", delete(BondState).where(BondState.user_id == uid)),
            ("points_wallets", delete(PointsWallet).where(PointsWallet.user_id == uid)),
            ("quest_progress", delete(QuestProgress).where(QuestProgress.user_id == uid)),
            ("quest_claims", delete(QuestClaim).where(QuestClaim.user_id == uid)),
            ("premium_entitlement", delete(UserPremiumEntitlement).where(UserPremiumEntitlement.user_id == uid)),
            ("stripe_customers", delete(StripeCustomer).where(StripeCustomer.discord_user_id == uid)),
            ("premium_gifts", delete(PremiumGift).where(
                (PremiumGift.gifter_user_id == uid) | (PremiumGift.recipient_user_id == uid)
            )),
            ("character_memories", delete(CharacterMemory).where(CharacterMemory.user_id == uid)),
            ("first_seen", delete(UserFirstSeen).where(UserFirstSeen.user_id == uid)),
            ("activity_days", delete(UserActivityDay).where(UserActivityDay.user_id == uid)),
        ]:
            res = await session.execute(stmt)
            summary[label] = res.rowcount  # type: ignore[union-attr]

        # Anonymise ledger (preserve financial audit trail)
        ledger_res = await session.execute(
            update(PointsLedger)
            .where(PointsLedger.user_id == uid)
            .values(user_id=0)
        )
        summary["points_ledger_anonymised"] = ledger_res.rowcount  # type: ignore[union-attr]

        await session.commit()

    # Best-effort Redis cleanup
    redis_deleted = await _clear_redis_keys(uid)
    summary["redis_keys_deleted"] = redis_deleted

    log.info("Deleted user data for user_id=%s: %s", uid, summary)
    return summary


# ---------------------------------------------------------------------------
# Redis cleanup
# ---------------------------------------------------------------------------

async def _clear_redis_keys(user_id: int) -> int:
    """Scan for and delete ephemeral Redis keys belonging to this user."""
    try:
        from utils.backpressure import get_redis_or_none
        r = await get_redis_or_none()
        if r is None:
            return 0

        uid = int(user_id)
        patterns = [
            f"user:{uid}:*",
            f"char:roll_window:{uid}",
            f"char:pity:*:{uid}",
            f"char:bonus_rolls:{uid}",
            f"char:onboarded:{uid}",
        ]

        deleted = 0
        for pattern in patterns:
            async for key in r.scan_iter(match=pattern, count=200):
                try:
                    await r.delete(key)
                    deleted += 1
                except Exception:
                    pass
        return deleted
    except Exception:
        log.debug("Redis cleanup failed for user %s", user_id, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row, exclude: set[str] | None = None) -> dict:
    """Convert an SQLAlchemy model instance to a JSON-friendly dict."""
    exclude = exclude or set()
    exclude.add("_sa_instance_state")
    d: dict = {}
    for k, v in vars(row).items():
        if k in exclude:
            continue
        if isinstance(v, datetime):
            v = v.isoformat()
        d[k] = v
    return d


async def _query_all(session, stmt) -> list[dict]:
    res = await session.execute(stmt)
    return [_row_to_dict(row) for row in res.scalars().all()]
