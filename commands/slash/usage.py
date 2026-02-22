# commands/slash/usage.py
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.ui import safe_ephemeral_send

from utils.audit import audit_log
from utils.storage import get_guild_setting, set_guild_setting
from utils.talk_store import count_talk_guild_since
from utils.feedback_store import count_feedback_guild_since
from utils.say_store import count_say_guild_since
from utils.premium import get_premium_tier, get_talk_caps

ANALYTICS_KEY = "analytics_events"
AI_PENALTY_KEY = "ai_penalties"
SAY_PENALTY_KEY = "say_penalties"



def _now() -> int:
    return int(time.time())


def _filter_by_days(events: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    days = max(1, min(int(days or 7), 30))
    cutoff = _now() - (days * 86400)
    out: list[dict[str, Any]] = []
    for e in events:
        try:
            if int(e.get("t", 0) or 0) >= cutoff:
                out.append(e)
        except Exception:
            continue
    return out


def _count_active_penalties(penalties: Any) -> int:
    if not isinstance(penalties, dict):
        return 0
    now = _now()
    active = 0
    for _uid, entry in penalties.items():
        if not isinstance(entry, dict):
            continue
        try:
            until = int(entry.get("penalty_until", 0) or 0)
        except Exception:
            until = 0
        if until > now:
            active += 1
    return active


def _fmt_channel(cid: int | None) -> str:
    if not cid:
        return "(unknown)"
    return f"<#{cid}>"


class SlashUsage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    usage = app_commands.Group(name="usage", description="Usage stats for this server")

    @staticmethod
    def _can_view(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if isinstance(interaction.user, discord.Member):
            return bool(interaction.user.guild_permissions.manage_guild)
        return False

    @usage.command(name="view", description="View server usage stats (owner/admin)")
    @app_commands.describe(days="How many days back to summarize (1–30)")
    async def usage_view(self, interaction: discord.Interaction, days: int = 7):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return

        if not self._can_view(interaction):
            await safe_ephemeral_send(interaction, "Only the **server owner** or an **admin (Manage Server)** can view usage.")
            return

        days = max(1, min(int(days or 7), 30))
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        tier = await get_premium_tier(user_id)
        talk_caps = await get_talk_caps(user_id)  # internal name for now

        # ----- DB-backed totals (accurate) -----
        now_utc = datetime.now(timezone.utc)
        since = now_utc - timedelta(days=days)

        talk_uses = await count_talk_guild_since(guild_id=guild_id, since_utc=since)
        say_uses = await count_say_guild_since(guild_id=guild_id, since_utc=since)
        feedback_uses = await count_feedback_guild_since(guild_id=guild_id, since_utc=since)

        # ----- Daily (UTC today) /talk guild usage -----
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        talk_today = await count_talk_guild_since(guild_id=guild_id, since_utc=day_start)
        talk_today_remaining = max(0, int(talk_caps.guild_daily_max) - int(talk_today))

        # ----- Analytics events (for top commands + cooldowns/denied/errors + channel stats) -----
        raw = await get_guild_setting(guild_id, ANALYTICS_KEY, default=[])
        if not isinstance(raw, list):
            raw = []
        events = [e for e in raw if isinstance(e, dict)]
        events = _filter_by_days(events, days)

        by_command = Counter()
        by_result = Counter()

        cooldowns = 0
        denied = 0
        errors = 0
        penalties_applied = 0
        penalized_denies = 0

        # Channel stats
        events_by_channel = Counter()
        say_by_channel = Counter()

        for e in events:
            cmd = e.get("command")
            res = e.get("result")
            cid = e.get("channel_id")

            if cmd:
                by_command[str(cmd)] += 1
            if res:
                by_result[str(res)] += 1

            if cid:
                try:
                    cid_int = int(cid)
                    events_by_channel[cid_int] += 1
                except Exception:
                    pass

            if str(cmd) == "say" and str(res) == "success" and cid:
                try:
                    say_by_channel[int(cid)] += 1
                except Exception:
                    pass

            if res == "cooldown":
                cooldowns += 1
                fields = e.get("fields")
                if isinstance(fields, dict) and bool(fields.get("penalty_applied")):
                    penalties_applied += 1

            if res == "denied":
                denied += 1
                if str(e.get("reason") or "") == "penalized":
                    penalized_denies += 1

            if res == "error":
                errors += 1

        top3 = by_command.most_common(3)
        top3_lines = [f"• `{name}`: **{count}**" for name, count in top3] or ["• (no command data yet)"]

        most_event_channel = None
        most_say_channel = None

        if events_by_channel:
            most_event_channel, _ = events_by_channel.most_common(1)[0]
        if say_by_channel:
            most_say_channel, _ = say_by_channel.most_common(1)[0]

        # Active penalties right now
        ai_pen = await get_guild_setting(guild_id, AI_PENALTY_KEY, default={})
        say_pen = await get_guild_setting(guild_id, SAY_PENALTY_KEY, default={})
        active_ai_penalties = _count_active_penalties(ai_pen)
        active_say_penalties = _count_active_penalties(say_pen)

        msg = (
            f"📈 **Server Usage (last {days} day(s))**\n\n"
            f"**Server tier:** `{tier}`\n\n"
            f"**/talk daily limits (UTC today)**\n"
            f"• Guild: **{talk_today}/{talk_caps.guild_daily_max}** (remaining **{talk_today_remaining}**)\n"
            f"• Per-user cap: **{talk_caps.daily_max}/day**\n\n"
            f"**Top commands**\n" + "\n".join(top3_lines) + "\n\n"
            f"**Counts (DB-backed)**\n"
            f"• `/talk`: **{talk_uses}**\n"
            f"• `/say`: **{say_uses}**\n"
            f"• `/feedback`: **{feedback_uses}**\n\n"
            f"**Moderation / outcomes (analytics)**\n"
            f"• Cooldowns: **{cooldowns}**\n"
            f"• Denied: **{denied}**\n"
            f"• Errors: **{errors}**\n\n"
            f"**Spam & penalties**\n"
            f"• Penalties applied (from cooldown spam): **{penalties_applied}**\n"
            f"• Denied due to active penalty: **{penalized_denies}**\n"
            f"• Active AI penalties right now: **{active_ai_penalties}**\n"
            f"• Active /say penalties right now: **{active_say_penalties}**\n\n"
            f"**Channels**\n"
            f"• Most `/say` messages: {_fmt_channel(most_say_channel)}\n"
            f"• Most logged events: {_fmt_channel(most_event_channel)}\n\n"
            f"**Total logged events (analytics):** **{len(events)}**"
        )

        audit_log(
            "USAGE_VIEW",
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="usage.view",
            result="success",
            fields={"days": days},
        )

        await safe_ephemeral_send(interaction, msg)

    @usage.command(name="reset", description="Reset usage analytics for this server (owner/admin)")
    async def usage_reset(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return

        if not self._can_view(interaction):
            await safe_ephemeral_send(
                interaction,
                "Only the **server owner** or an **admin (Manage Server)** can reset usage.",
            )
            return

        guild_id = interaction.guild.id

        # Reset analytics event log used for top commands/outcomes/channel stats
        await set_guild_setting(guild_id, ANALYTICS_KEY, [])

        audit_log(
            "USAGE_RESET",
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="usage.reset",
            result="success",
        )

        await safe_ephemeral_send(
            interaction,
            "🧹 **Usage reset successfully.**\nCleared stored analytics events used for `/usage` summaries.",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashUsage(bot))

