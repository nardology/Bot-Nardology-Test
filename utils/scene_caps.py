# utils/scene_caps.py
from __future__ import annotations

from dataclasses import dataclass
from utils.premium import get_premium_tier

@dataclass(frozen=True)
class SceneCaps:
    user_daily_turns: int
    guild_daily_turns: int
    user_weekly_turns: int
    guild_weekly_turns: int
    active_per_channel: int
    active_per_guild: int
    active_per_user: int   



async def get_scene_caps(user_id: int) -> SceneCaps:
    tier = await get_premium_tier(user_id)

    if tier == "pro":
        return SceneCaps(
            user_daily_turns=100,
            guild_daily_turns=600,
            user_weekly_turns=500,
            guild_weekly_turns=4000,
            active_per_channel=5,
            active_per_guild=30,
            active_per_user=10,
        )
    
    return SceneCaps(
        user_daily_turns=20,
        guild_daily_turns=120,
        user_weekly_turns=100,
        guild_weekly_turns=800,
        active_per_channel=2,
        active_per_guild=8,
        active_per_user=2,
    )
