from __future__ import annotations

"""
Optional SQLAlchemy DB layer.

This project is Redis-first. These helpers are kept ONLY for backwards-compatibility
with older codepaths. Importing this module must NOT crash if SQLAlchemy isn't installed.
If you still need Postgres/SQLite, add `sqlalchemy[asyncio]` and an async driver like
`asyncpg` (Postgres) or `aiosqlite` (SQLite) to requirements.txt.
"""

import os
import logging
from typing import Optional, Any

log = logging.getLogger("db")

_engine: Any = None
_sessionmaker: Any = None


def get_database_url() -> str:
    """Public accessor for the database URL (used by Alembic and other tooling)."""
    return _database_url()


def _database_url() -> str:
    url = (os.getenv("DATABASE_URL", "") or "").strip()
    if url:
        # Convert sync postgres URLs to asyncpg URLs if needed
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    # In production, DATABASE_URL must be provided. A silent SQLite fallback
    # causes confusing crash-loops (aiosqlite missing) and data loss across deploys.
    env = (os.getenv("ENVIRONMENT", "prod") or "prod").strip().lower()
    if env != "dev":
        raise RuntimeError(
            "DATABASE_URL is missing. Set DATABASE_URL (Postgres) in your environment. "
            "If you are running locally, set ENVIRONMENT=dev to allow a local SQLite fallback."
        )

    # Dev-only fallback (requires aiosqlite if you actually use it)
    return "sqlite+aiosqlite:///./bot.db"


def _require_sqlalchemy() -> Any:
    try:
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession  # type: ignore
        return create_async_engine, async_sessionmaker, AsyncSession
    except Exception as e:
        raise RuntimeError(
            "SQLAlchemy is not installed. This bot is Redis-first; "
            "if you still need a DB, add `sqlalchemy[asyncio]` and an async driver "
            "(e.g., asyncpg or aiosqlite) to requirements.txt."
        ) from e


def get_engine():
    global _engine
    if _engine is None:
        create_async_engine, _, _ = _require_sqlalchemy()
        url = _database_url()
        is_sqlite = url.startswith("sqlite")
        pool_kwargs: dict = {}
        if not is_sqlite:
            pool_kwargs = {
                "pool_size": 10,
                "max_overflow": 20,
                "pool_timeout": 30,
                "pool_recycle": 1800,
                "pool_pre_ping": True,
            }
        _engine = create_async_engine(url, future=True, echo=False, **pool_kwargs)
    return _engine


def get_sessionmaker():
    global _sessionmaker
    if _sessionmaker is None:
        _, async_sessionmaker, _ = _require_sqlalchemy()
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker



# ---------------------------------------------------------------------------
# Schema drift safety: _ensure_* functions
# ---------------------------------------------------------------------------
# When adding a new column to an ORM model used in production (e.g. PointsWallet,
# QuestProgress, UserPremiumEntitlement), do BOTH:
#   1. Add an Alembic migration that introduces the column.
#   2. If this table has an _ensure_* helper below, add the same column here with
#      ADD COLUMN IF NOT EXISTS and the same type/default, so deployments that
#      skip migrations still work. Keep this in sync with the latest migrations.
# Tables with ensure helpers: points_wallet, character_user_state, stripe/premium.
# ---------------------------------------------------------------------------


async def _ensure_points_wallet_columns(conn) -> None:
    """Ensure new columns exist on points_wallet even if migrations weren't applied.

    create_all(checkfirst=True) does NOT add missing columns, so we do a lightweight
    ALTER TABLE ADD COLUMN IF NOT EXISTS for backwards-compatible schema evolution.
    Must stay in sync with alembic/versions 0013_engagement_streak_rewards and
    0014_engagement_badges_weekly (and any future points_wallet migrations).
    """
    try:
        from sqlalchemy import text  # type: ignore
        dialect = getattr(conn, "dialect", None)
        name = getattr(dialect, "name", "")
        if name != "postgresql":
            return

        # Add columns used by streak-restore feature
        await conn.execute(text("ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_saved INTEGER DEFAULT 0"))
        await conn.execute(
            text("ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_restore_deadline_day_utc VARCHAR(16) DEFAULT ''")
        )
        # Engagement streak rewards (0013)
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_7_bonus_given BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_last_30_bonus_at INTEGER DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_10_character_claimed BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_15_character_claimed BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_25_character_claimed BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_75_notification_sent BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS random_bonus_consecutive_days INTEGER DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS random_bonus_last_reward_day_utc VARCHAR(8) DEFAULT ''"
        ))
        # Engagement badges and weekly activity (0014)
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_badge_30 BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_badge_60 BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS streak_badge_90 BOOLEAN DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE points_wallet ADD COLUMN IF NOT EXISTS weekly_activity_bonus_week_utc VARCHAR(8) DEFAULT ''"
        ))
    except Exception:
        # Don't crash-loop on permissions or non-Postgres setups.
        log.exception("points_wallet column ensure failed")


async def _ensure_character_user_state_columns(conn) -> None:
    """Best-effort schema drift fix: add columns introduced after initial deploy."""
    try:
        from sqlalchemy import text  # type: ignore
        await conn.execute(text("""
            ALTER TABLE character_user_state
            ADD COLUMN IF NOT EXISTS inventory_upgrades INTEGER DEFAULT 0
        """))
    except Exception:
        # Don't crash-loop on permissions or non-Postgres setups.
        log.exception("character_user_state column ensure failed")


async def _ensure_stripe_columns(conn) -> None:
    """Best-effort: add Stripe columns to premium_entitlements and create stripe_customers table.

    Phase 6: Stripe payment integration. These columns/tables are needed before
    Alembic migration 0010 is applied.
    """
    try:
        from sqlalchemy import text  # type: ignore
        dialect = getattr(conn, "dialect", None)
        name = getattr(dialect, "name", "")
        if name != "postgresql":
            return

        # Add Stripe columns to premium_entitlements
        await conn.execute(text(
            "ALTER TABLE premium_entitlements ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(128)"
        ))
        await conn.execute(text(
            "ALTER TABLE premium_entitlements ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(128)"
        ))
        await conn.execute(text(
            "ALTER TABLE premium_entitlements ADD COLUMN IF NOT EXISTS subscription_period_end TIMESTAMPTZ"
        ))
        await conn.execute(text(
            "ALTER TABLE premium_entitlements ADD COLUMN IF NOT EXISTS activated_by_user_id BIGINT"
        ))

        # Create stripe_customers table if it doesn't exist
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stripe_customers (
                id SERIAL PRIMARY KEY,
                discord_user_id BIGINT NOT NULL,
                stripe_customer_id VARCHAR(128) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        # Best-effort indexes (ignore if they already exist)
        try:
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_stripe_customers_discord ON stripe_customers (discord_user_id)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_stripe_customers_stripe ON stripe_customers (stripe_customer_id)"
            ))
        except Exception:
            pass

        log.info("Stripe columns/tables ensured")
    except Exception:
        log.exception("Stripe column ensure failed (non-fatal)")


async def _ensure_global_quest_columns(conn) -> None:
    """ADD COLUMN activated_at when migrations were not run (create_all does not alter)."""
    try:
        from sqlalchemy import text  # type: ignore

        dialect = getattr(conn, "dialect", None)
        name = getattr(dialect, "name", "")
        if name == "postgresql":
            await conn.execute(
                text(
                    "ALTER TABLE global_quest_events "
                    "ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ"
                )
            )
        elif name == "sqlite":
            try:
                await conn.execute(
                    text("ALTER TABLE global_quest_events ADD COLUMN activated_at DATETIME")
                )
            except Exception:
                pass
    except Exception:
        log.exception("global_quest_events column ensure failed")


async def _ensure_global_quest_schema(conn) -> None:
    """Create global quest tables when prod skips Base.metadata.create_all (same data path as /connection traits)."""
    try:
        from utils.models import GlobalQuestContribution, GlobalQuestEvent, UserProfileBadge

        await conn.run_sync(lambda sync_conn: GlobalQuestEvent.__table__.create(sync_conn, checkfirst=True))
        await conn.run_sync(lambda sync_conn: GlobalQuestContribution.__table__.create(sync_conn, checkfirst=True))
        await conn.run_sync(lambda sync_conn: UserProfileBadge.__table__.create(sync_conn, checkfirst=True))
        log.info("global_quest schema: tables present or created")
    except Exception:
        log.exception("global_quest table create failed")
    await _ensure_global_quest_columns(conn)


_DB_RETRY_ATTEMPTS = 5
_DB_RETRY_BASE_DELAY = 2.0


async def init_db() -> None:
    """Initialize the async SQLAlchemy engine and create tables.

    This bot was originally Redis-first, but we now use Postgres as the
    source-of-truth for durable data (premium, characters, bonds).

    Fast path (no Alembic required): create missing tables on startup.

    Retries up to 5 times with exponential backoff for transient connectivity
    failures (common on Railway cold starts when the DB boots after the app).
    """
    try:
        from utils.models import Base  # type: ignore
    except Exception as e:
        raise RuntimeError("Failed importing SQLAlchemy models (utils.models)") from e

    engine = get_engine()
    env = (os.getenv("ENVIRONMENT", "prod") or "prod").strip().lower()
    auto_create = str(os.getenv("DB_AUTO_CREATE", "")).strip().lower() in {"1", "true", "yes", "on"}

    last_exc: Exception | None = None
    for attempt in range(1, _DB_RETRY_ATTEMPTS + 1):
        try:
            await _init_db_inner(engine, Base, env, auto_create)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < _DB_RETRY_ATTEMPTS:
                delay = _DB_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.warning(
                    "DB init attempt %d/%d failed (%s); retrying in %.1fs…",
                    attempt, _DB_RETRY_ATTEMPTS, exc, delay,
                )
                import asyncio
                await asyncio.sleep(delay)
            else:
                log.exception("DB init/preflight failed after %d attempts", _DB_RETRY_ATTEMPTS)
                raise last_exc


async def _init_db_inner(engine, Base, env: str, auto_create: bool) -> None:
    """Core init_db logic, separated so the retry wrapper stays clean."""
    async with engine.begin() as conn:
        if env == "dev" or auto_create:
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_points_wallet_columns(conn)
            await _ensure_character_user_state_columns(conn)
            await _ensure_stripe_columns(conn)
            await _ensure_global_quest_schema(conn)
            log.info("DB init OK (tables ensured; env=%s auto_create=%s)", env, auto_create)
        else:
            from sqlalchemy import text  # type: ignore

            await conn.execute(text("SELECT 1"))

            try:
                from utils.models import (
                    AnalyticsDailyMetric,
                    UserFirstSeen,
                    PointsWallet,
                    PointsLedger,
                    QuestProgress,
                    QuestClaim,
                    CharacterRecommendation,
                )  # type: ignore

                await conn.run_sync(lambda sync_conn: AnalyticsDailyMetric.__table__.create(sync_conn, checkfirst=True))
                await conn.run_sync(lambda sync_conn: UserFirstSeen.__table__.create(sync_conn, checkfirst=True))
                await conn.run_sync(lambda sync_conn: PointsWallet.__table__.create(sync_conn, checkfirst=True))
                await _ensure_points_wallet_columns(conn)
                await _ensure_character_user_state_columns(conn)
                await conn.run_sync(lambda sync_conn: PointsLedger.__table__.create(sync_conn, checkfirst=True))
                await conn.run_sync(lambda sync_conn: QuestProgress.__table__.create(sync_conn, checkfirst=True))
                await conn.run_sync(lambda sync_conn: QuestClaim.__table__.create(sync_conn, checkfirst=True))
                await conn.run_sync(lambda sync_conn: CharacterRecommendation.__table__.create(sync_conn, checkfirst=True))
                await _ensure_stripe_columns(conn)
                log.info("DB preflight OK (env=%s). Analytics + points tables ensured.", env)
            except Exception:
                log.exception("DB analytics table ensure failed")
                log.info("DB preflight OK (env=%s). Apply full migrations via Alembic.", env)
            try:
                await _ensure_global_quest_schema(conn)
            except Exception:
                log.exception("global_quest schema ensure failed (non-fatal)")
