"""core/ai_usage.py

Centralized AI usage budgets (daily + weekly) and usage recording.

Enforces two layers of budgets:
  1. Call-count budgets (daily + weekly per user + per guild, tier-aware)
  2. Token budgets (daily + weekly per user, using entitlements token caps)

Notes:
  - "Weekly" is rolling 7 days in UTC (now - 7 days).
  - Counters are stored in Redis day-buckets with ~8 day TTL.
  - Token budgets use actual tokens reported by the API (recorded after each call).
  - If Redis is unavailable, budget checks degrade safely (treat as 0 used).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from utils.premium import get_talk_caps
from utils.scene_caps import get_scene_caps

from utils.talk_store import (
    count_talk_guild_since,
    count_talk_user_since,
    count_talk_tokens_user_since,
    insert_talk,
    insert_talk_tokens,
)
from utils.scene_usage_store import (
    count_scene_turns_guild_since,
    count_scene_turns_user_since,
    count_scene_tokens_user_since,
    insert_scene_turn,
    insert_scene_tokens,
)


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    message: str = ""


def _utc_day_start(now_utc: datetime) -> datetime:
    return now_utc.replace(hour=0, minute=0, second=0, microsecond=0)


async def check_budget(*, mode: str, guild_id: int, user_id: int) -> BudgetDecision:
    """Check daily + weekly budgets for the given mode.

    Returns BudgetDecision(allowed=False, message=...) if over budget.
    """
    mode = (mode or "").strip().lower()
    now_utc = datetime.now(timezone.utc)
    day_start = _utc_day_start(now_utc)
    week_start = now_utc - timedelta(days=7)

    if mode == "talk":
        caps = await get_talk_caps(user_id)

        used_guild_today = await count_talk_guild_since(guild_id=guild_id, since_utc=day_start)
        if used_guild_today >= caps.guild_daily_max:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ This server hit its daily /talk limit (**{caps.guild_daily_max}/day**). "
                    "Try again tomorrow (UTC)."
                ),
            )

        used_user_today = await count_talk_user_since(guild_id=guild_id, user_id=user_id, since_utc=day_start)
        if used_user_today >= caps.daily_max:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ You’ve hit your daily /talk limit (**{caps.daily_max}/day**). "
                    "Try again tomorrow (UTC)."
                ),
            )

        used_guild_week = await count_talk_guild_since(guild_id=guild_id, since_utc=week_start)
        if used_guild_week >= caps.guild_weekly_max:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ This server hit its weekly /talk limit (**{caps.guild_weekly_max}/7 days**). "
                    "Try again later."
                ),
            )

        used_user_week = await count_talk_user_since(guild_id=guild_id, user_id=user_id, since_utc=week_start)
        if used_user_week >= caps.weekly_max:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ You’ve hit your weekly /talk limit (**{caps.weekly_max}/7 days**). "
                    "Try again later."
                ),
            )

        # Token-budget enforcement (actual tokens consumed, from entitlements caps)
        try:
            from core.entitlements import get_entitlements
            ent = await get_entitlements(user_id=user_id, guild_id=guild_id)

            user_tokens_today = await count_talk_tokens_user_since(
                guild_id=guild_id, user_id=user_id, since_utc=day_start,
            )
            if user_tokens_today >= ent.budgets.talk_daily_tokens:
                return BudgetDecision(
                    allowed=False,
                    message=(
                        f"⛔ You've used your daily AI token budget "
                        f"(**{user_tokens_today:,}/{ent.budgets.talk_daily_tokens:,} tokens**). "
                        "Try again tomorrow (UTC)."
                    ),
                )

            user_tokens_week = await count_talk_tokens_user_since(
                guild_id=guild_id, user_id=user_id, since_utc=week_start,
            )
            if user_tokens_week >= ent.budgets.talk_weekly_tokens:
                return BudgetDecision(
                    allowed=False,
                    message=(
                        f"⛔ You've used your weekly AI token budget "
                        f"(**{user_tokens_week:,}/{ent.budgets.talk_weekly_tokens:,} tokens**). "
                        "Try again later."
                    ),
                )
        except Exception:
            pass

        return BudgetDecision(allowed=True)

    if mode == "scene":
        caps = await get_scene_caps(user_id)

        used_guild_today = await count_scene_turns_guild_since(guild_id=guild_id, since_utc=day_start)
        if used_guild_today >= caps.guild_daily_turns:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ This server hit its daily scene-turn limit (**{caps.guild_daily_turns}/day**). "
                    "Try again tomorrow (UTC)."
                ),
            )

        used_user_today = await count_scene_turns_user_since(guild_id=guild_id, user_id=user_id, since_utc=day_start)
        if used_user_today >= caps.user_daily_turns:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ You hit your daily scene-turn limit (**{caps.user_daily_turns}/day**). "
                    "Try again tomorrow (UTC)."
                ),
            )

        used_guild_week = await count_scene_turns_guild_since(guild_id=guild_id, since_utc=week_start)
        if used_guild_week >= caps.guild_weekly_turns:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ This server hit its weekly scene-turn limit (**{caps.guild_weekly_turns}/7 days**). "
                    "Try again later."
                ),
            )

        used_user_week = await count_scene_turns_user_since(guild_id=guild_id, user_id=user_id, since_utc=week_start)
        if used_user_week >= caps.user_weekly_turns:
            return BudgetDecision(
                allowed=False,
                message=(
                    f"⛔ You hit your weekly scene-turn limit (**{caps.user_weekly_turns}/7 days**). "
                    "Try again later."
                ),
            )

        # Token-budget enforcement for scene mode
        try:
            from core.entitlements import get_entitlements
            ent = await get_entitlements(user_id=user_id, guild_id=guild_id)

            user_tokens_today = await count_scene_tokens_user_since(
                guild_id=guild_id, user_id=user_id, since_utc=day_start,
            )
            if user_tokens_today >= ent.budgets.scene_daily_tokens:
                return BudgetDecision(
                    allowed=False,
                    message=(
                        f"⛔ You've used your daily scene token budget "
                        f"(**{user_tokens_today:,}/{ent.budgets.scene_daily_tokens:,} tokens**). "
                        "Try again tomorrow (UTC)."
                    ),
                )

            user_tokens_week = await count_scene_tokens_user_since(
                guild_id=guild_id, user_id=user_id, since_utc=week_start,
            )
            if user_tokens_week >= ent.budgets.scene_weekly_tokens:
                return BudgetDecision(
                    allowed=False,
                    message=(
                        f"⛔ You've used your weekly scene token budget "
                        f"(**{user_tokens_week:,}/{ent.budgets.scene_weekly_tokens:,} tokens**). "
                        "Try again later."
                    ),
                )
        except Exception:
            pass

        return BudgetDecision(allowed=True)

    # Unknown mode -> don't block
    return BudgetDecision(allowed=True)


async def record_success(*, mode: str, guild_id: int, user_id: int, tokens: int = 0) -> None:
    """Record successful AI usage (call counts + token counts)."""
    mode = (mode or "").strip().lower()
    if mode == "talk":
        await insert_talk(guild_id=guild_id, user_id=user_id)
        if tokens > 0:
            await insert_talk_tokens(guild_id=guild_id, user_id=user_id, tokens=tokens)
        return
    if mode == "scene":
        await insert_scene_turn(guild_id=guild_id, user_id=user_id, scene_id=0)
        if tokens > 0:
            await insert_scene_tokens(guild_id=guild_id, user_id=user_id, tokens=tokens)
        return
