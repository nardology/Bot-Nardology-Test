# utils/bonds.py
from __future__ import annotations

import math

# Step 2 defaults (tweak later)
DAILY_XP_CAP_PER_CHARACTER = 10

# Level curve: fast early, slower later
# lvl 1 at 0xp, lvl 2 at ~4xp, lvl 3 at ~9xp, lvl 5 at ~25xp, lvl 10 at ~100xp
def level_from_xp(xp: int) -> int:
    xp = max(0, int(xp or 0))
    return max(1, int(math.sqrt(xp)) + 1)

def next_level_xp(level: int) -> int:
    level = max(1, int(level or 1))
    # inverse of level_from_xp approx: (level-1)^2
    return (level - 1) ** 2

def title_for_level(level: int) -> str:
    # Simple ladder (you can theme these later)
    if level >= 20:
        return "Soulbound"
    if level >= 15:
        return "Devoted"
    if level >= 10:
        return "Close Companion"
    if level >= 5:
        return "Trusted"
    if level >= 3:
        return "Friend"
    return "Acquaintance"

def tier_for_level(level: int) -> int:
    """Map a numeric bond LEVEL to a bond TIER (0..5) aligned with titles.

    Tier meanings:
      0 = Acquaintance (no bond image)
      1 = Friend (Level 3-4)
      2 = Trusted (Level 5-9)
      3 = Close Companion (Level 10-14)
      4 = Devoted (Level 15-19)
      5 = Soulbound (Level 20+)
    """
    try:
        lvl = int(level)
    except Exception:
        return 0

    if lvl >= 20:
        return 5
    if lvl >= 15:
        return 4
    if lvl >= 10:
        return 3
    if lvl >= 5:
        return 2
    if lvl >= 3:
        return 1
    return 0

