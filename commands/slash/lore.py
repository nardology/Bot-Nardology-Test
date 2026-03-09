import discord
from discord.ext import commands
from discord import app_commands

import config


class SlashLore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="lore", description="Explore the worlds and characters of Bot-Nardology")
    async def lore(self, interaction: discord.Interaction):
        base = config.BASE_URL
        if not base:
            await interaction.response.send_message(
                "Lore page is not available right now (BASE_URL not configured).",
                ephemeral=True,
            )
            return

        url = f"{base}/lore"
        embed = discord.Embed(
            title="World Lore",
            description=(
                "Explore the worlds, regions, and characters of Bot-Nardology.\n"
                "You can also suggest lore changes on the page!"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="The lore page is public — share the link with anyone!")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Lore Page", url=url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashLore(bot))
