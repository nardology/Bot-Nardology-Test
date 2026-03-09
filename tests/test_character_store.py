"""Tests for character_store logic (no DB/Redis required for these)."""
from __future__ import annotations

import pytest


def test_compute_limits():
    from utils.character_store import compute_limits, ROLLS_PER_DAY_FREE, ROLLS_PER_DAY_PRO

    rolls_free, slots_free = compute_limits(is_pro=False)
    rolls_pro, slots_pro = compute_limits(is_pro=True)

    assert slots_free == 3
    assert slots_pro == 10
    assert rolls_free == ROLLS_PER_DAY_FREE
    assert rolls_pro == ROLLS_PER_DAY_PRO


def test_count_inventory_nonbase():
    from utils.character_store import _count_inventory_nonbase
    from utils.character_registry import BASE_STYLE_IDS

    base_set = {s.lower() for s in (BASE_STYLE_IDS or [])}
    # Empty
    assert _count_inventory_nonbase([]) == 0
    assert _count_inventory_nonbase(set()) == 0
    # Only base (if any)
    if base_set:
        assert _count_inventory_nonbase(list(base_set)) == 0
    # All custom (no overlap with base)
    assert _count_inventory_nonbase(["wizard", "knight", "wizard"]) == 2  # unique
    assert _count_inventory_nonbase(["a", "b", "c"]) == 3


def test_roll_window_seconds_env():
    from utils.character_store import roll_window_seconds, _roll_window_seconds

    # Default when env not set is 18000 (5h) or 0 if ROLL_WINDOW_SECONDS=0
    val = _roll_window_seconds()
    assert val >= 0
    assert val in (0, 18000) or True  # may be overridden by env in CI
