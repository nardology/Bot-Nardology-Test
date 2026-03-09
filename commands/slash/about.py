import discord
from discord.ext import commands
from discord import app_commands

import config


class SlashAbout(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="about", description="Learn about Bot-Nardology — features, pricing, tech, and more")
    async def about(self, interaction: discord.Interaction):
        base = config.BASE_URL
        if not base:
            await interaction.response.send_message(
                "The about page is not available right now (BASE_URL not configured).",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="About Bot-Nardology",
            description=(
                f"AI-Powered Roleplay, Characters & Community\n\n"
                f"**[Visit Landing Page]({base})**\n\n"
                f"Learn about our features, Pro subscription, technology stack, "
                f"roadmap, and how to get in touch."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Quick Links",
            value=(
                f"[World Lore]({base}/lore)\n"
                f"[Support Server]({config.SUPPORT_SERVER_URL})\n"
                f"[Terms of Service]({config.TERMS_OF_SERVICE_URL})\n"
                f"[Privacy Policy]({config.PRIVACY_POLICY_URL})"
            ),
            inline=False,
        )
        embed.set_footer(text="Nardology Enterprises")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashAbout(bot))
