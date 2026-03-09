# commands/slash/cosmetic.py
"""Cosmetic profile display: select which cosmetic shows on /inspect."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.cosmetics import cosmetic_display_name, COSMETIC_IDS
from utils.cosmetics_store import get_owned, get_selected, set_selected

logger = logging.getLogger("bot.cosmetic")


async def _ac_cosmetic_owned(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete: owned cosmetics only."""
    uid = int(interaction.user.id)
    owned = await get_owned(uid)
    cur = (current or "").strip().lower()
    out: list[app_commands.Choice[str]] = []
    for cid in sorted(owned):
        name = cosmetic_display_name(cid)
        if not cur or cur in cid or cur in name.lower():
            out.append(app_commands.Choice(name=name, value=cid))
        if len(out) >= 25:
            break
    return out


class SlashCosmetic(commands.Cog):
    """Cosmetic profile display: select one cosmetic to show when someone inspects you."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    cosmetic = app_commands.Group(name="cosmetic", description="Profile cosmetic (shown on /inspect)")

    @cosmetic.command(name="select", description="Choose which cosmetic to display on your profile when inspected")
    @app_commands.describe(cosmetic="Cosmetic to display (only owned cosmetics)")
    @app_commands.autocomplete(cosmetic=_ac_cosmetic_owned)
    async def cosmetic_select(
        self,
        interaction: discord.Interaction,
        cosmetic: str | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        uid = int(interaction.user.id)
        owned = await get_owned(uid)
        if not owned:
            await interaction.response.send_message(
                "You don't own any cosmetics yet. Buy one from **/points cosmetic-shop** first.",
                ephemeral=True,
            )
            return
        # If no choice given, show current + list owned and ask to pick
        if not cosmetic or (cosmetic := cosmetic.strip().lower()) not in COSMETIC_IDS:
            current = await get_selected(uid)
            lines = [f"• **{cosmetic_display_name(cid)}** (`{cid}`)" for cid in sorted(owned)]
            msg = "**Your cosmetics:**\n" + "\n".join(lines)
            if current:
                msg += f"\n\nCurrently showing: **{cosmetic_display_name(current)}**."
            msg += "\n\nUse `/cosmetic select cosmetic:<name>` to choose one (e.g. `tails`)."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        if cosmetic not in owned:
            await interaction.response.send_message(
                f"You don't own **{cosmetic_display_name(cosmetic)}**. Buy it from **/points cosmetic-shop** first.",
                ephemeral=True,
            )
            return
        await set_selected(uid, cosmetic)
        await interaction.response.send_message(
            f"✅ Your profile cosmetic is now **{cosmetic_display_name(cosmetic)}**. It will show when someone uses /inspect on you.",
            ephemeral=True,
        )

    @cosmetic.command(name="clear", description="Remove your profile cosmetic (inspect will show no cosmetic)")
    async def cosmetic_clear(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        uid = int(interaction.user.id)
        await set_selected(uid, None)
        await interaction.response.send_message(
            "✅ Profile cosmetic cleared. /inspect will no longer show a cosmetic for you.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashCosmetic(bot))
