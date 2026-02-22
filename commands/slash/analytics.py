# commands/slash/analytics.py
from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.ui import safe_ephemeral_send
from utils.analytics import get_summary, reset_guild
from utils.audit import audit_log



def _is_guild_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id


class SlashAnalytics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Owner-only: guild_only + runtime check below; avoid Group kwargs that older discord.py lacks
    analytics = app_commands.Group(
        name="analytics",
        description="(Owner-only) Usage analytics for this server",
        guild_only=True,
    )

    @analytics.command(name="view", description="(Owner-only) View server usage analytics")
    @app_commands.describe(days="How many days back to summarize (1–30)")
    async def analytics_view(self, interaction: discord.Interaction, days: int = 7):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return

        if not _is_guild_owner(interaction):
            await safe_ephemeral_send(interaction, "Only the **server owner** can use this.")
            return

        days = max(1, min(int(days or 7), 30))

        summary = await get_summary(interaction.guild.id, days=days)
        by_cmd = summary.get("by_command", {}) or {}
        by_res = summary.get("by_result", {}) or {}
        by_event = summary.get("by_event", {}) or {}

        # Back-compat: if your audit_log uses "ask" internally, keep reading "ask"
        talk_uses = int(by_cmd.get("talk", 0) or 0)
        ask_uses = int(by_cmd.get("ask", 0) or 0)
        say_uses = int(by_cmd.get("say", 0) or 0)

        # Prefer talk if present, else fallback to ask
        talk_display = talk_uses if talk_uses > 0 else ask_uses

        cooldowns = int(by_res.get("cooldown", 0) or 0)
        denied = int(by_res.get("denied", 0) or 0)
        errors = int(by_res.get("error", 0) or 0)

        # Event sanity checks
        talk_success = int(by_event.get("TALK_SUCCESS", 0) or 0)
        ask_success = int(by_event.get("TALK_SUCCESS", 0) or 0)
        say_cooldown_user = int(by_event.get("SAY_COOLDOWN_USER", 0) or 0)

        msg = (
            f"📊 **Server Analytics (last {days} day(s))**\n\n"
            f"**Usage (by command)**\n"
            f"• `/talk`: **{talk_display}**\n"
            f"• `/say`: **{say_uses}**\n\n"
            f"**Outcomes (by result)**\n"
            f"• Cooldowns: **{cooldowns}**\n"
            f"• Denied: **{denied}**\n"
            f"• Errors: **{errors}**\n\n"
            f"**Event sanity checks**\n"
            f"• TALK_SUCCESS: **{talk_success}** (fallback TALK_SUCCESS={ask_success})\n"
            f"• SAY_COOLDOWN_USER: **{say_cooldown_user}**\n\n"
            f"**Total logged events:** **{int(summary.get('events_total', 0) or 0)}**"
        )

        audit_log(
            "ANALYTICS_VIEW",
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="analytics.view",
            result="success",
            fields={"days": days},
        )

        await safe_ephemeral_send(interaction, msg)

    @analytics.command(name="reset", description="(Owner-only) Clear stored analytics for this server")
    async def analytics_reset(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return

        if not _is_guild_owner(interaction):
            await safe_ephemeral_send(interaction, "Only the **server owner** can use this.")
            return

        await reset_guild(interaction.guild.id)

        audit_log(
            "ANALYTICS_RESET",
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="analytics.reset",
            result="success",
        )

        await safe_ephemeral_send(
            interaction,
            "🧹 **Analytics reset successfully.**\nAll usage counters have been cleared.",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashAnalytics(bot))
