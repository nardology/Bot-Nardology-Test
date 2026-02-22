"""Pytest configuration and fixtures. Run without real Redis/DB by default."""
from __future__ import annotations

import os
import sys

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Avoid loading .env that might point at prod
os.environ.setdefault("ENVIRONMENT", "dev")


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """By default, make get_redis_or_none return None so tests don't need Redis."""
    try:
        from utils import backpressure
        async def _none():
            return None
        monkeypatch.setattr(backpressure, "get_redis_or_none", _none)
    except Exception:
        pass


@pytest.fixture
async def async_db_session():
    """Yield an in-memory async SQLite session with all tables created."""
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from utils.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    await engine.dispose()
