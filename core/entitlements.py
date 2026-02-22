# core/entitlements.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import config
from utils.premium import get_premium_tier  # async: resolves tier per user

UserTier = Literal["free", "pro"]


@dataclass(frozen=True)
class AIBudgets:
    # token budgets (not shown to user)
    talk_daily_tokens: int
    talk_weekly_tokens: int
    scene_daily_tokens: int
    scene_weekly_tokens: int


@dataclass(frozen=True)
class Entitlements:
    tier: UserTier

    # AI policy
    ai_private_only: bool  # True => only ephemeral responses allowed
    max_prompt_chars: int  # soft clamp for user input
    max_output_chars: int  # discord output target (gateway can still trim)

    # AI output token limits (sent to the model as max_output_tokens)
    max_output_tokens_talk: int
    max_output_tokens_scene: int

    # Economy / gacha
    character_slots: int
    daily_free_rolls: int
    paid_roll_cap_per_day: int  # "extra rolls per day" cap if you add tickets later

    # Budgets
    budgets: AIBudgets


# ---- Tier table (single source of truth) ----
# Adjust numbers whenever you want; everything else should read from here.
_TIER_TABLE: dict[UserTier, Entitlements] = {
    "free": Entitlements(
        tier="free",
        ai_private_only=True,         # your rule: free AI must be private
        max_prompt_chars=900,
        max_output_chars=1800,
        max_output_tokens_talk=200,   # ~2-3 sentences; keeps free-tier cost low
        max_output_tokens_scene=500,
        character_slots=6,
        daily_free_rolls=1,
        paid_roll_cap_per_day=0,
        budgets=AIBudgets(
            talk_daily_tokens=8_000,
            talk_weekly_tokens=35_000,
            scene_daily_tokens=10_000,
            scene_weekly_tokens=45_000,
        ),
    ),
    "pro": Entitlements(
        tier="pro",
        ai_private_only=False,        # pro can talk publicly (if channel allowed)
        max_prompt_chars=1400,
        max_output_chars=1900,
        max_output_tokens_talk=350,   # longer, richer character replies
        max_output_tokens_scene=1200,
        character_slots=20,
        daily_free_rolls=2,
        paid_roll_cap_per_day=5,
        budgets=AIBudgets(
            talk_daily_tokens=40_000,
            talk_weekly_tokens=200_000,
            scene_daily_tokens=60_000,
            scene_weekly_tokens=300_000,
        ),
    ),
}


async def get_entitlements(*, user_id: int, guild_id: int | None) -> Entitlements:
    """
    Single entry point to determine what the user is allowed to do.

    Premium is now a *user-level* entitlement.
    """
    uid = int(user_id or 0)
    tier = (await get_premium_tier(uid) or "free").lower().strip()
    if tier not in _TIER_TABLE:
        tier = "free"
    return _TIER_TABLE[tier]


def is_ai_public_allowed(*, ent: Entitlements) -> bool:
    """
    Public AI means non-ephemeral output. Free is private-only.
    """
    if config.AI_DISABLED:
        return False
    return not ent.ai_private_only
