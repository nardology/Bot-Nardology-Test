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
                "AI-Powered Roleplay, Characters & Community\n\n"
                "Learn about our features, Pro subscription, technology stack, "
                "roadmap, and how to get in touch."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Nardology Enterprises")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Visit Landing Page", url=base, style=discord.ButtonStyle.link))
        view.add_item(discord.ui.Button(label="World Lore", url=f"{base}/lore", style=discord.ButtonStyle.link))
        view.add_item(discord.ui.Button(label="Support Server", url=config.SUPPORT_SERVER_URL, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashAbout(bot))
