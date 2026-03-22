# commands/slash/global_quest.py
"""Community monthly global quest — training progress."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.character_store import load_state

logger = logging.getLogger("bot.global_quest")


class GlobalQuestCog(commands.Cog):
    """/globalquest — show active event and your training contribution."""

    @app_commands.command(
        name="globalquest",
        description="View the active community training event and your progress",
    )
    async def globalquest(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        gid = int(interaction.guild_id or 0)
        uid = int(interaction.user.id)
        try:
            st = await load_state(uid)
            sel = (getattr(st, "active_style_id", "") or "").strip().lower()
        except Exception:
            sel = ""

        try:
            from utils.global_quest import build_quest_view_for_user

            v = await build_quest_view_for_user(
                guild_id=gid,
                user_id=uid,
                selected_style_id=sel or None,
            )
        except Exception:
            logger.exception("globalquest failed")
            await interaction.followup.send("⚠️ Could not load event data.", ephemeral=True)
            return

        if v is None:
            await interaction.followup.send(
                "There is **no active** community event for this server right now.",
                ephemeral=True,
            )
            return

        scope_note = "**Global** — all servers share one progress bar." if v.scope == "global" else "**This server only** — progress counts for this community."
        user_line = f"Your training (all characters here): **{v.user_training}**"
        if sel:
            user_line += f"\nOn **{sel}**: **{v.user_character_training}**"

        embed = discord.Embed(
            title=f"🎯 {v.title}",
            description=(v.description[:3900] + ("…" if len(v.description) > 3900 else "")) or "*(no description)*",
            color=0xE94560,
        )
        embed.add_field(name="Scope", value=scope_note, inline=False)
        if v.activated_at:
            embed.add_field(
                name="Activated (UTC)",
                value=f"<t:{int(v.activated_at.timestamp())}:F>",
                inline=True,
            )
        bar_amt = v.total_training if v.scope == "global" else v.guild_training
        embed.add_field(
            name="Community progress",
            value=f"**{v.progress_pct:.1f}%** — {bar_amt:,} / {v.target_training_points:,} training points",
            inline=False,
        )
        embed.add_field(name="You", value=user_line, inline=False)
        embed.add_field(name="Days left (UTC)", value=str(v.days_left), inline=True)
        if v.image_url:
            embed.set_image(url=v.image_url)
        elif v.image_url_secondary:
            embed.set_image(url=v.image_url_secondary)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GlobalQuestCog(bot))
