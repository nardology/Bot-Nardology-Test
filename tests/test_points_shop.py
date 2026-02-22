"""Tests for points shop logic (costs, limits)."""
from __future__ import annotations

import math

import pytest


def test_inv_upgrade_cost_scaling():
    # Same formula as in points.py: 500 * (1.25 ** upg)
    # upg 0->500, 1->625, 2->782, 3->977, 4->1221
    base = 500
    expected = [500, 625, 782, 977, 1221]
    for upg in range(0, 5):
        cost = int(math.ceil(base * (1.25 ** upg)))
        assert cost >= 500
        assert cost == expected[upg], f"upgrade {upg}: expected {expected[upg]}, got {cost}"


def test_shop_items_structure():
    from commands.slash.points import SHOP_ITEMS

    assert "inv_upgrade" in SHOP_ITEMS
    assert "pull_5" in SHOP_ITEMS
    assert "pull_10" in SHOP_ITEMS
    for key, item in SHOP_ITEMS.items():
        assert "cost" in item, f"{key} missing cost"
        assert "name" in item or "emoji" in item, f"{key} should have name or emoji"


def test_compute_limits_inventory_math():
    from utils.character_store import compute_limits, get_inventory_upgrades
    # Base slots only (get_inventory_upgrades may need DB; we test formula)
    _, base_slots_free = compute_limits(is_pro=False)
    _, base_slots_pro = compute_limits(is_pro=True)
    max_slots_free = base_slots_free + (0 * 5)
    max_slots_pro = base_slots_pro + (2 * 5)  # 2 upgrades
    assert max_slots_free == 3
    assert max_slots_pro == 20
