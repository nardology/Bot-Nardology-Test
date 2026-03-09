"""Slash command: /recommend — opens the character recommendation form on Netlify."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.recommendations import generate_token, get_pending_recommendation

logger = logging.getLogger("cmd.recommend")


class RecommendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="recommend", description="Recommend a new official character for Bot-Nardology")
    async def recommend(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        form_base = config.RECOMMEND_FORM_URL
        api_base = config.BASE_URL
        if not form_base or not api_base:
            await interaction.followup.send(
                "The recommendation system is not fully configured yet. "
                "Please contact the bot owner to set BASE_URL.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id
        token = generate_token(user_id, "submit")
        url = f"{form_base}?token={token}&api={api_base}"

        existing = await get_pending_recommendation(user_id)

        if existing:
            embed = discord.Embed(
                title="Edit Your Recommendation",
                description=(
                    f"You already have a pending recommendation for **{existing.display_name}**.\n\n"
                    f"Click the link below to edit it:\n{url}"
                ),
                color=0xF39C12,
            )
        else:
            embed = discord.Embed(
                title="Recommend a Character",
                description=(
                    "Click the link below to open the recommendation form. "
                    "Fill out as much detail as you'd like about your character idea.\n\n"
                    f"**Form:** {url}\n\n"
                    "You'll be notified via DM when your recommendation is reviewed."
                ),
                color=0xE94560,
            )

        embed.set_footer(text="This link expires in 30 days.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecommendCog(bot))
