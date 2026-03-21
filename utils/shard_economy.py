"""Shard economy helpers (duplicate rolls, points conversion parity)."""
from __future__ import annotations

# Rarity string -> shards awarded when rolling a duplicate of an owned character.
DUPLICATE_SHARDS_BY_RARITY: dict[str, int] = {
    "common": 5,
    "uncommon": 10,
    "rare": 25,
    "legendary": 100,
    "mythic": 500,
}

# 1 shard = 10 wallet points (before transfer fee).
POINTS_PER_SHARD = 10

# 10% deducted from the OUTPUT of each conversion (both directions).
TRANSFER_FEE_FRACTION = 0.10


def duplicate_shards_for_rarity(rarity: str | None) -> int:
    """Return shard payout for a duplicate roll; unknown rarities default to common."""
    r = (rarity or "common").strip().lower()
    if r not in DUPLICATE_SHARDS_BY_RARITY:
        r = "common"
    return int(DUPLICATE_SHARDS_BY_RARITY[r])


def shards_to_points_after_fee(shards: int) -> int:
    """Convert shards to wallet points: floor(shards * POINTS_PER_SHARD * (1 - fee))."""
    s = max(0, int(shards or 0))
    if s <= 0:
        return 0
    import math

    return int(math.floor(s * POINTS_PER_SHARD * (1.0 - TRANSFER_FEE_FRACTION)))


def points_to_shards_after_fee(points: int) -> int:
    """Convert wallet points to shards: floor((points / POINTS_PER_SHARD) * (1 - fee))."""
    p = max(0, int(points or 0))
    if p <= 0:
        return 0
    import math

    return int(math.floor((p / float(POINTS_PER_SHARD)) * (1.0 - TRANSFER_FEE_FRACTION)))
