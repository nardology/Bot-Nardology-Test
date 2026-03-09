"""Slash command for viewing legal documents and contact information."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config


class SlashLegal(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="legal", description="View our Terms of Service, Privacy Policy, and contact info")
    async def legal(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Legal & Policies",
            description="KAI by Nardology Enterprises",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Terms of Service",
            value=f"[Read our Terms of Service]({config.TERMS_OF_SERVICE_URL})",
            inline=False,
        )
        embed.add_field(
            name="Privacy Policy",
            value=f"[Read our Privacy Policy]({config.PRIVACY_POLICY_URL})",
            inline=False,
        )
        embed.add_field(
            name="Support Server",
            value=f"[Join our Discord]({config.SUPPORT_SERVER_URL})",
            inline=False,
        )
        embed.add_field(
            name="Contact",
            value="fontanafletcher@gmail.com",
            inline=False,
        )
        embed.set_footer(text="Nardology Enterprises · Greenville, NC")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashLegal(bot))
