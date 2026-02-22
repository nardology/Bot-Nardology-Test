from __future__ import annotations

"""
Optional SQLAlchemy models (kept for backwards compatibility).

This Redis-first build does not require SQLAlchemy at runtime. Importing this module
must not crash if SQLAlchemy isn't installed.
"""

from typing import Any

try:
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship  # type: ignore
    from sqlalchemy import String, Integer, BigInteger, Boolean, DateTime, Text, ForeignKey, Index  # type: ignore
    from datetime import datetime, timezone

    class Base(DeclarativeBase):
        pass

    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    # --- Legacy models (left intact) ---
    # If you still use these, ensure SQLAlchemy + driver are installed and that
    # you run migrations / create tables appropriately.

    class GuildSetting(Base):
        __tablename__ = "guild_settings"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        key: Mapped[str] = mapped_column(String(64), index=True)
        value: Mapped[str] = mapped_column(Text, default="")
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (Index("ix_guild_settings_guild_key", "guild_id", "key", unique=True),)

    # ------------------------------------------------------------------
    # Durable data models (Postgres source-of-truth)
    # ------------------------------------------------------------------

    class PremiumEntitlement(Base):
        __tablename__ = "premium_entitlements"

        guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
        tier: Mapped[str] = mapped_column(String(16), default="free")
        source: Mapped[str] = mapped_column(String(32), default="manual")
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        # Stripe fields (added Phase 6)
        stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
        stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
        subscription_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
        activated_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

        __table_args__ = (Index("ix_premium_entitlements_guild", "guild_id"),)


    class CharacterUserState(Base):
        __tablename__ = "character_user_state"

        user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
        active_style_id: Mapped[str] = mapped_column(String(64), default="")
        points: Mapped[int] = mapped_column(Integer, default=0)
        inventory_upgrades: Mapped[int] = mapped_column(Integer, default=0)
        roll_day: Mapped[str] = mapped_column(String(16), default="")
        roll_used: Mapped[int] = mapped_column(Integer, default=0)
        pity_mythic: Mapped[int] = mapped_column(Integer, default=0)
        pity_legendary: Mapped[int] = mapped_column(Integer, default=0)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)


    class CharacterOwnedStyle(Base):
        __tablename__ = "character_owned_styles"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        style_id: Mapped[str] = mapped_column(String(64), index=True)
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_owned_unique", "user_id", "style_id", unique=True),
        )


    class CharacterCustomStyle(Base):
        __tablename__ = "character_custom_styles"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        style_id: Mapped[str] = mapped_column(String(64), index=True)
        name: Mapped[str] = mapped_column(String(64), default="")
        prompt: Mapped[str] = mapped_column(Text, default="")
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_custom_unique", "user_id", "style_id", unique=True),
        )


    class BondState(Base):
        __tablename__ = "bond_state"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        style_id: Mapped[str] = mapped_column(String(64), index=True)
        xp: Mapped[int] = mapped_column(Integer, default=0)
        nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_bond_unique", "guild_id", "user_id", "style_id", unique=True),
        )


    class VoiceSound(Base):
        """Durable registry for custom voice sounds.

        Storage modes:
        - "local": file stored on local/volume filesystem
        - "s3": object stored in S3/R2 (url + object_key)
        """

        __tablename__ = "voice_sounds"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        name: Mapped[str] = mapped_column(String(64), index=True)

        storage_mode: Mapped[str] = mapped_column(String(16), default="local")
        object_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
        url: Mapped[str | None] = mapped_column(Text, nullable=True)

        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_voice_sounds_unique", "guild_id", "name", unique=True),
        )


    class AnalyticsDailyMetric(Base):
        """Daily per-guild metrics aggregated from lightweight Redis counters."""

        __tablename__ = "analytics_daily_metrics"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        day_utc: Mapped[str] = mapped_column(String(8), index=True)  # YYYYMMDD
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        metric: Mapped[str] = mapped_column(String(64), index=True)
        value: Mapped[int] = mapped_column(Integer, default=0)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_analytics_daily_unique", "day_utc", "guild_id", "metric", unique=True),
        )


    class UserFirstSeen(Base):
        """First-seen date per (guild,user). Enables retention queries."""

        __tablename__ = "analytics_user_first_seen"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        first_day_utc: Mapped[str] = mapped_column(String(8), index=True)  # YYYYMMDD
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_first_seen_unique", "guild_id", "user_id", unique=True),
        )


    class UserActivityDay(Base):
        """One row per (user, guild, day) when user was active. Enables retention (D1/D7/D30)."""

        __tablename__ = "analytics_user_activity_day"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        day_utc: Mapped[str] = mapped_column(String(8), index=True)  # YYYYMMDD

        __table_args__ = (
            Index("ix_user_activity_day_unique", "user_id", "guild_id", "day_utc", unique=True),
        )


    # ------------------------------------------------------------------
    # Points economy (Phase 2)
    # ------------------------------------------------------------------

    class PointsWallet(Base):
        """Per-(guild,user) points wallet and daily claim state."""

        __tablename__ = "points_wallet"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)

        balance: Mapped[int] = mapped_column(Integer, default=0)

        # Daily claim tracking
        last_claim_day_utc: Mapped[str] = mapped_column(String(8), default="")  # YYYYMMDD
        streak: Mapped[int] = mapped_column(Integer, default=0)

        # If the user breaks their streak, we keep the previous value for a limited
        # time so they can pay to restore it.
        streak_saved: Mapped[int] = mapped_column(Integer, default=0)
        streak_restore_deadline_day_utc: Mapped[str] = mapped_column(String(8), default="")  # YYYYMMDD

        # One-time first claim bonus guard
        first_claimed: Mapped[bool] = mapped_column(Boolean, default=False)

        # Simple booster support (shop demo)
        booster_kind: Mapped[str] = mapped_column(String(32), default="")
        booster_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_points_wallet_unique", "guild_id", "user_id", unique=True),
            Index("ix_points_wallet_guild", "guild_id"),
            Index("ix_points_wallet_user", "user_id"),
        )


    class PointsLedger(Base):
        """Append-only ledger of points changes for audits/anti-abuse."""

        __tablename__ = "points_ledger"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)

        delta: Mapped[int] = mapped_column(Integer, default=0)
        reason: Mapped[str] = mapped_column(String(64), default="")
        meta_json: Mapped[str] = mapped_column(Text, default="")

        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_points_ledger_guild_day", "guild_id", "created_at"),
            Index("ix_points_ledger_user_day", "user_id", "created_at"),
        )


    class QuestProgress(Base):
        """Quest progress per (guild,user,period,quest)."""

        __tablename__ = "quest_progress"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)

        # 'daily' | 'weekly' | 'monthly'
        period: Mapped[str] = mapped_column(String(16), default="daily")
        # Period key so we can reset cleanly: daily=YYYYMMDD, weekly=YYYYMMDD(monday), monthly=YYYYMM
        period_key: Mapped[str] = mapped_column(String(16), default="")

        quest_id: Mapped[str] = mapped_column(String(64), default="")
        progress: Mapped[int] = mapped_column(Integer, default=0)
        completed: Mapped[bool] = mapped_column(Boolean, default=False)

        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_quest_progress_unique", "guild_id", "user_id", "period", "quest_id", unique=True),
            Index("ix_quest_progress_user", "guild_id", "user_id"),
        )


    class QuestClaim(Base):
        """Claimed quest rewards per (guild,user,period,period_key,quest)."""

        __tablename__ = "quest_claims"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
        user_id: Mapped[int] = mapped_column(BigInteger, index=True)

        # 'daily' | 'weekly' | 'monthly'
        period: Mapped[str] = mapped_column(String(16), default="daily")
        period_key: Mapped[str] = mapped_column(String(16), default="")
        quest_id: Mapped[str] = mapped_column(String(64), default="")

        claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index(
                "ix_quest_claims_unique",
                "guild_id",
                "user_id",
                "period",
                "period_key",
                "quest_id",
                unique=True,
            ),
            Index("ix_quest_claims_user", "guild_id", "user_id"),
        )

    # ------------------------------------------------------------------
    # Stripe (Phase 6)
    # ------------------------------------------------------------------

    class StripeCustomer(Base):
        """Maps Discord user IDs to Stripe customer IDs for reuse across purchases."""

        __tablename__ = "stripe_customers"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
        stripe_customer_id: Mapped[str] = mapped_column(String(128), nullable=False)
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_stripe_customers_discord", "discord_user_id", unique=True),
            Index("ix_stripe_customers_stripe", "stripe_customer_id", unique=True),
        )

    # ------------------------------------------------------------------
    # User-level premium (individual subscriptions)
    # ------------------------------------------------------------------

    class UserPremiumEntitlement(Base):
        """Per-user premium entitlement. Primary key is the Discord user ID."""

        __tablename__ = "user_premium_entitlements"

        user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
        tier: Mapped[str] = mapped_column(String(16), default="free")
        source: Mapped[str] = mapped_column(String(32), default="manual")
        updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
        stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
        subscription_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
        gifted_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

        __table_args__ = (Index("ix_user_premium_entitlements_user", "user_id"),)


    class PremiumGift(Base):
        """Audit trail for gifted premium subscriptions."""

        __tablename__ = "premium_gifts"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        gifter_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        recipient_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
        months: Mapped[int] = mapped_column(Integer, default=1)
        stripe_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_premium_gifts_gifter", "gifter_user_id"),
            Index("ix_premium_gifts_recipient", "recipient_user_id"),
        )

    # NOTE: Old PremiumEntitlement (guild-level) is kept for data preservation.

    # ------------------------------------------------------------------
    # Phase 6b: Persistent Memory Anchors
    # ------------------------------------------------------------------

    class CharacterMemory(Base):
        """Long-term memory anchors for a user-character pair (Pro-only)."""

        __tablename__ = "character_memories"

        id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
        user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
        style_id: Mapped[str] = mapped_column(String(64), nullable=False)
        memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
        memory_value: Mapped[str] = mapped_column(String(512), nullable=False)
        source: Mapped[str] = mapped_column(String(16), nullable=False, default="keyword")
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

        __table_args__ = (
            Index("ix_charmem_user_style", "user_id", "style_id"),
        )

except Exception:  # pragma: no cover
    # Provide stubs so imports won't crash.
    Base: Any = object  # type: ignore
