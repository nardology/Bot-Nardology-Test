"""Unit tests for pity progress formatting."""
from __future__ import annotations

from utils.pity_display import (
    LEGENDARY_GUARANTEE_AT,
    MYTHIC_GUARANTEE_AT,
    pct_toward_guarantee,
    legendary_phase,
    mythic_phase,
)


def test_pct_toward_guarantee_monotonic():
    prev = -1.0
    for i in range(0, LEGENDARY_GUARANTEE_AT + 5, 5):
        p = pct_toward_guarantee(i, LEGENDARY_GUARANTEE_AT)
        assert p >= prev
        assert 0.0 <= p <= 100.0
        prev = p


def test_pct_mythic_cap():
    p = pct_toward_guarantee(2000, MYTHIC_GUARANTEE_AT)
    assert p == 100.0


def test_phases_order():
    assert legendary_phase(0) == "early"
    assert "warming" in legendary_phase(20)
    assert "guaranteed" in legendary_phase(LEGENDARY_GUARANTEE_AT)
    assert "guaranteed" in mythic_phase(MYTHIC_GUARANTEE_AT)
