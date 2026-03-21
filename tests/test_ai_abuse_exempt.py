"""Throttle exempt does not bypass manual restriction; skips auto-throttle when flagged."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_should_throttle_exempt_skips_flagged_throttle(monkeypatch):
    from utils import ai_abuse

    async def _no_restrict(_uid):
        return False

    async def _exempt(_uid):
        return True

    async def _flagged(_uid):
        return True

    monkeypatch.setattr(ai_abuse, "is_abuse_restricted", _no_restrict)
    monkeypatch.setattr(ai_abuse, "is_throttle_exempt", _exempt)
    monkeypatch.setattr(ai_abuse, "is_abuse_flagged", _flagged)

    import config

    monkeypatch.setattr(config, "AI_ABUSE_AUTO_THROTTLE", True, raising=False)

    out = await ai_abuse.should_throttle_user(12345)
    assert out is False


@pytest.mark.asyncio
async def test_manual_restrict_still_throttles_even_if_exempt(monkeypatch):
    from utils import ai_abuse

    async def _restricted(_uid):
        return True

    async def _exempt(_uid):
        return True

    monkeypatch.setattr(ai_abuse, "is_abuse_restricted", _restricted)
    monkeypatch.setattr(ai_abuse, "is_throttle_exempt", _exempt)

    out = await ai_abuse.should_throttle_user(99999)
    assert out is True
