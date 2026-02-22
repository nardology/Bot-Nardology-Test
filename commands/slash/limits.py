# commands/slash/limits.py
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from core.ui import safe_ephemeral_send

from utils.premium import get_premium_tier, get_talk_caps

from utils.ai_limits import (
    get_user_limiter,
    get_guild_limiter,
    get_scene_limiter,
    get_summary_limiter,
    get_summary_daily_user_limiter,
    get_summary_daily_guild_limiter,
)

from utils.say_limits import get_say_user_limiter, get_say_guild_limiter
from utils.talk_store import count_talk_user_since, count_talk_guild_since

from utils.scene_caps import get_scene_caps
from utils.scene_usage_store import (
    count_scene_turns_user_since,
    count_scene_turns_guild_since,
)
from utils.scene_store import (
    count_active_scenes_in_channel,
    count_active_scenes_in_guild,
    count_active_scenes_for_user,
)

from utils.reporting import (
    get_report_limiter,
    get_report_daily_user_limiter,
    get_report_daily_guild_limiter,
)



async def _maybe_await(x):
    """Await x if it's awaitable, otherwise return x."""
    if inspect.isawaitable(x):
        return await x
    return x


async def _peek_limiter(limiter, key: str) -> tuple[int | None, int | None, int]:
    """
    Redis-safe "peek" for a limiter.
    Returns: (used, remaining, retry_after_seconds)

    - For Redis sliding-window limiters, we cannot safely inspect internal event queues.
      So we rely on limiter.check(key) and whatever fields it returns.
    - For older in-memory limiters, this still works if their check() returns the same object.
    """
    try:
        chk = await _maybe_await(limiter.check(key))
    except Exception:
        # If limiter is down/misconfigured, don't break /limits.
        return None, None, 0

    # Common fields across your codebase: allowed, remaining, retry_after_seconds
    used = getattr(chk, "used", None)
    remaining = getattr(chk, "remaining", None)
    retry_after = int(getattr(chk, "retry_after_seconds", 0) or 0)

    # Fallback: if remaining isn't provided, estimate from limiter.max_events if possible
    if remaining is None:
        try:
            if getattr(chk, "allowed", True):
                remaining = int(getattr(limiter, "max_events", 0) or 0)
            else:
                remaining = 0
        except Exception:
            remaining = None

    return used, remaining, retry_after


def _cooldown_line(retry_after: int) -> str:
    return f" (cooldown ~{retry_after}s)" if retry_after else ""


def _used_str(x: int | None) -> str:
    return "?" if x is None else str(int(x))


def _rem_str(x: int | None) -> str:
    return "?" if x is None else str(int(x))


class SlashLimits(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    limits = app_commands.Group(name="limits", description="View current limits and remaining usage")

    @limits.command(name="view", description="View current server limits and your remaining usage")
    async def limits_view(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        # If anything below throws, we still want to reply rather than infinite-spin
        try:
            tier = await get_premium_tier(user_id)

            # ---- AI sliding-window limits ----
            ai_user_limiter = await get_user_limiter(guild_id)
            ai_guild_limiter = await get_guild_limiter(guild_id)

            ai_u_used, ai_u_remaining, ai_u_retry = await _peek_limiter(ai_user_limiter, f"user:{user_id}")
            ai_g_used, ai_g_remaining, ai_g_retry = await _peek_limiter(ai_guild_limiter, f"guild:{guild_id}")

            # ---- /scene summary limits ----
            summary_burst = await get_summary_limiter(guild_id)
            sum_b_used, sum_b_remaining, sum_b_retry = await _peek_limiter(
                summary_burst,
                f"guild:{guild_id}:scene_summary",
            )

            summary_daily_user = await get_summary_daily_user_limiter(guild_id)
            sum_du_used, sum_du_remaining, sum_du_retry = await _peek_limiter(
                summary_daily_user,
                f"summary:day:user:{user_id}",
            )

            summary_daily_guild = await get_summary_daily_guild_limiter(guild_id)
            sum_dg_used, sum_dg_remaining, sum_dg_retry = await _peek_limiter(
                summary_daily_guild,
                "summary:day:guild",
            )

            # ---- SAY sliding-window limits ----
            say_user_limiter = await get_say_user_limiter(guild_id)
            say_guild_limiter = await get_say_guild_limiter(guild_id)

            say_u_used, say_u_remaining, say_u_retry = await _peek_limiter(say_user_limiter, f"user:{user_id}")
            say_g_used, say_g_remaining, say_g_retry = await _peek_limiter(say_guild_limiter, f"guild:{guild_id}")

            # ---- /report limits ----
            report_burst_limiter = await get_report_limiter(guild_id)
            rep_b_used, rep_b_remaining, rep_b_retry = await _peek_limiter(report_burst_limiter, f"report:{user_id}")

            report_daily_user_limiter = await get_report_daily_user_limiter(guild_id)
            rep_du_used, rep_du_remaining, rep_du_retry = await _peek_limiter(
                report_daily_user_limiter,
                f"report:day:user:{user_id}",
            )

            report_daily_guild_limiter = await get_report_daily_guild_limiter(guild_id)
            rep_dg_used, rep_dg_remaining, rep_dg_retry = await _peek_limiter(
                report_daily_guild_limiter,
                "report:day:guild",
            )

            # ---- TALK daily caps (UTC day) ----
            talk_caps = await get_talk_caps(user_id)
            now_utc = datetime.now(timezone.utc)
            day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

            talk_used_today_user = await count_talk_user_since(
                guild_id=guild_id,
                user_id=user_id,
                since_utc=day_start,
            )
            talk_remaining_today_user = max(0, int(talk_caps.daily_max) - int(talk_used_today_user))

            talk_used_today_guild = await count_talk_guild_since(
                guild_id=guild_id,
                since_utc=day_start,
            )
            talk_remaining_today_guild = max(0, int(talk_caps.guild_daily_max) - int(talk_used_today_guild))

            # ---- SCENE caps (UTC day + active caps) ----
            scene_caps = await get_scene_caps(user_id)

            scene_used_today_user = await count_scene_turns_user_since(
                guild_id=guild_id,
                user_id=user_id,
                since_utc=day_start,
            )
            scene_remaining_today_user = max(0, int(scene_caps.user_daily_turns) - int(scene_used_today_user))

            scene_used_today_guild = await count_scene_turns_guild_since(
                guild_id=guild_id,
                since_utc=day_start,
            )
            scene_remaining_today_guild = max(0, int(scene_caps.guild_daily_turns) - int(scene_used_today_guild))

            active_in_channel = await count_active_scenes_in_channel(guild_id=guild_id, channel_id=channel_id)
            active_in_guild = await count_active_scenes_in_guild(guild_id=guild_id)
            active_for_user = await count_active_scenes_for_user(guild_id=guild_id, user_id=user_id)

            # ---- Scene cooldown limiter config ----
            scene_limiter = await get_scene_limiter(guild_id)
            scene_limiter_config = f"{getattr(scene_limiter, 'max_events', '?')}/{getattr(scene_limiter, 'window_seconds', '?')}s"

            msg = (
                f"**Limits for this server**\n"
                f"- Premium tier: `{tier}`\n\n"

                f"**AI (/talk) rate limits (sliding window)**\n"
                f"- Per-user: `{ai_user_limiter.max_events}/{ai_user_limiter.window_seconds}s` — used `{_used_str(ai_u_used)}`, remaining `{_rem_str(ai_u_remaining)}`{_cooldown_line(ai_u_retry)}\n"
                f"- Per-server: `{ai_guild_limiter.max_events}/{ai_guild_limiter.window_seconds}s` — used `{_used_str(ai_g_used)}`, remaining `{_rem_str(ai_g_remaining)}`{_cooldown_line(ai_g_retry)}\n\n"

                f"**/say rate limits (sliding window)**\n"
                f"- Per-user: `{say_user_limiter.max_events}/{say_user_limiter.window_seconds}s` — used `{_used_str(say_u_used)}`, remaining `{_rem_str(say_u_remaining)}`{_cooldown_line(say_u_retry)}\n"
                f"- Per-server: `{say_guild_limiter.max_events}/{say_guild_limiter.window_seconds}s` — used `{_used_str(say_g_used)}`, remaining `{_rem_str(say_g_remaining)}`{_cooldown_line(say_g_retry)}\n\n"

                f"**/talk daily caps (UTC day)**\n"
                f"- You: `{talk_used_today_user}/{talk_caps.daily_max}` — remaining `{talk_remaining_today_user}`\n"
                f"- Server: `{talk_used_today_guild}/{talk_caps.guild_daily_max}` — remaining `{talk_remaining_today_guild}`\n\n"

                f"**/report limits**\n"
                f"- Burst (per-user): `{report_burst_limiter.max_events}/{report_burst_limiter.window_seconds}s` — used `{_used_str(rep_b_used)}`, remaining `{_rem_str(rep_b_remaining)}`{_cooldown_line(rep_b_retry)}\n"
                f"- Daily (you): `{_used_str(rep_du_used)}/{report_daily_user_limiter.max_events}` — remaining `{_rem_str(rep_du_remaining)}`{_cooldown_line(rep_du_retry)}\n"
                f"- Daily (server): `{_used_str(rep_dg_used)}/{report_daily_guild_limiter.max_events}` — remaining `{_rem_str(rep_dg_remaining)}`{_cooldown_line(rep_dg_retry)}\n\n"

                f"**/scene summary limits**\n"
                f"- Burst (server): `{summary_burst.max_events}/{summary_burst.window_seconds}s` — used `{_used_str(sum_b_used)}`, remaining `{_rem_str(sum_b_remaining)}`{_cooldown_line(sum_b_retry)}\n"
                f"- Daily (you): `{_used_str(sum_du_used)}/{summary_daily_user.max_events}` — remaining `{_rem_str(sum_du_remaining)}`{_cooldown_line(sum_du_retry)}\n"
                f"- Daily (server): `{_used_str(sum_dg_used)}/{summary_daily_guild.max_events}` — remaining `{_rem_str(sum_dg_remaining)}`{_cooldown_line(sum_dg_retry)}\n\n"

                f"**/scene active caps**\n"
                f"- This channel: `{active_in_channel}/{scene_caps.active_per_channel}`\n"
                f"- This server: `{active_in_guild}/{scene_caps.active_per_guild}`\n"
                f"- You (active scenes): `{active_for_user}/{scene_caps.active_per_user}`\n"
                f"- Scene turn cooldown config: `{scene_limiter_config}`\n"
            )

            await safe_ephemeral_send(interaction, msg)

        except Exception:
            # If anything breaks, respond instead of leaving the user in a spinner.
            await safe_ephemeral_send(
                interaction,
                "⚠️ /limits hit an internal error. (The bot is still running, but limit inspection failed.)",
            )


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashLimits") is None:
        await bot.add_cog(SlashLimits(bot))

