"""Tests for points economy logic (utils/points_store.py)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from utils.points_store import _daily_amount_for_streak


# ---------------------------------------------------------------------------
# Pure function: _daily_amount_for_streak
# ---------------------------------------------------------------------------

class TestDailyAmountForStreak:
    """Economy formula: base(30) + streak_bonus(+2/day, cap 10 days) + milestones."""

    def test_streak_1(self):
        assert _daily_amount_for_streak(1) == 30

    def test_streak_2(self):
        assert _daily_amount_for_streak(2) == 32

    def test_streak_5(self):
        # 30 + (4 * 2) = 38
        assert _daily_amount_for_streak(5) == 38

    def test_streak_7_milestone(self):
        # 30 + (6 * 2) + 20(milestone) = 62
        assert _daily_amount_for_streak(7) == 62

    def test_streak_10_milestone(self):
        # 30 + min(9, 10)*2 + 20(7-day) + 30(10-day) = 30 + 18 + 20 + 30 = 98
        assert _daily_amount_for_streak(10) == 98

    def test_streak_11_capped_bonus(self):
        # 30 + min(10, 10)*2 + 20 + 30 = 30 + 20 + 20 + 30 = 100
        assert _daily_amount_for_streak(11) == 100

    def test_streak_50_same_as_11(self):
        # Bonus capped at +20, milestones at 7+10 — no further scaling
        assert _daily_amount_for_streak(50) == 100

    def test_streak_0_edge(self):
        # streak=0 is unusual but shouldn't crash; bonus = max(0, -1) = 0
        assert _daily_amount_for_streak(0) == 30


# ---------------------------------------------------------------------------
# claim_daily — DB-backed tests
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_engine():
    """Create a standalone async engine + tables for points economy tests."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from utils.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


class TestClaimDaily:

    @pytest.mark.asyncio
    async def test_first_claim_gives_bonus(self, db_engine):
        """First-ever claim awards base + 100 first-claim bonus, streak = 1."""
        from utils.points_store import claim_daily

        _patch_db(db_engine)

        result = await claim_daily(guild_id=0, user_id=9001)
        assert result.streak == 1
        assert result.first_bonus_awarded == 100
        expected_base = _daily_amount_for_streak(1)
        assert result.awarded == expected_base + 100
        assert result.balance == expected_base + 100
        assert result.claimed_today is True

    @pytest.mark.asyncio
    async def test_duplicate_claim_same_day(self, db_engine):
        """Second claim on same day returns awarded=0."""
        from utils.points_store import claim_daily

        _patch_db(db_engine)

        first = await claim_daily(guild_id=0, user_id=9002)
        assert first.awarded > 0

        second = await claim_daily(guild_id=0, user_id=9002)
        assert second.awarded == 0
        assert second.claimed_today is True
        assert second.balance == first.balance

    @pytest.mark.asyncio
    async def test_consecutive_day_increments_streak(self, db_engine):
        """Claiming on consecutive days increments the streak."""
        from utils.points_store import claim_daily
        from utils.models import PointsWallet
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy import select as sa_select

        _patch_db(db_engine)

        r1 = await claim_daily(guild_id=0, user_id=9003)
        assert r1.streak == 1

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        async with AsyncSession(db_engine, expire_on_commit=False) as s:
            wallet = (await s.execute(
                sa_select(PointsWallet).where(PointsWallet.user_id == 9003).limit(1)
            )).scalar_one()
            wallet.last_claim_day_utc = yesterday
            await s.commit()

        r2 = await claim_daily(guild_id=0, user_id=9003)
        assert r2.streak == 2

    @pytest.mark.asyncio
    async def test_missed_day_resets_streak(self, db_engine):
        """Missing a day resets streak to 1 and saves old streak for restore."""
        from utils.points_store import claim_daily
        from utils.models import PointsWallet
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy import select as sa_select

        _patch_db(db_engine)

        r1 = await claim_daily(guild_id=0, user_id=9004)
        assert r1.streak == 1

        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
        async with AsyncSession(db_engine, expire_on_commit=False) as s:
            wallet = (await s.execute(
                sa_select(PointsWallet).where(PointsWallet.user_id == 9004).limit(1)
            )).scalar_one()
            wallet.last_claim_day_utc = three_days_ago
            wallet.streak = 5
            await s.commit()

        r2 = await claim_daily(guild_id=0, user_id=9004)
        assert r2.streak == 1

        async with AsyncSession(db_engine, expire_on_commit=False) as s:
            wallet = (await s.execute(
                sa_select(PointsWallet).where(PointsWallet.user_id == 9004).limit(1)
            )).scalar_one()
            assert wallet.streak_saved == 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_db(async_engine):
    """Monkey-patch get_sessionmaker to return a factory backed by the test engine."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    import utils.points_store as ps
    import utils.db as db_mod

    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    db_mod._sessionmaker = factory
    db_mod._engine = async_engine

    ps._update_points_leaderboard = AsyncMock()
    try:
        import utils.leaderboard
        utils.leaderboard.update_all_periods = AsyncMock()
    except Exception:
        pass
