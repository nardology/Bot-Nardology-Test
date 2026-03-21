"""Shard duplicate payouts and points conversion helpers."""
from __future__ import annotations

from utils.shard_economy import (
    POINTS_PER_SHARD,
    duplicate_shards_for_rarity,
    points_to_shards_after_fee,
    shards_to_points_after_fee,
)


def test_duplicate_shards_by_rarity():
    assert duplicate_shards_for_rarity("common") == 5
    assert duplicate_shards_for_rarity("uncommon") == 10
    assert duplicate_shards_for_rarity("rare") == 25
    assert duplicate_shards_for_rarity("legendary") == 100
    assert duplicate_shards_for_rarity("mythic") == 500
    assert duplicate_shards_for_rarity("bogus") == 5


def test_points_per_shard_constant():
    assert POINTS_PER_SHARD == 10


def test_conversion_fees():
    # 10 shards -> 90 points (10% fee on point output)
    assert shards_to_points_after_fee(10) == 90
    # 100 points -> 9 shards floor(10 * 0.9)
    assert points_to_shards_after_fee(100) == 9
    # Too small -> 0
    assert points_to_shards_after_fee(10) == 0


def test_hobby_pricing_formula():
    from utils.connection_traits_catalog import hobby_slot_price_after_expansions

    assert hobby_slot_price_after_expansions(0) == round(25 * 1.1**1)
    assert hobby_slot_price_after_expansions(1) == round(25 * 1.1**2)
