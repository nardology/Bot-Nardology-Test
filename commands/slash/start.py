from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.character_store import grant_onboarding_roll
from utils.premium import grant_premium_trial
from core.kai_mascot import embed_kai_start, get_kai_start_greeting


logger = logging.getLogger("slash.start")


class StartRollView(discord.ui.View):
    def __init__(self, bot: commands.Bot, *, user_id: int, guild_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="\U0001f3b2 Use your free roll", style=discord.ButtonStyle.primary)
    async def do_roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("SlashCharacter")
        if cog is None:
            await interaction.response.send_message("\u26a0\ufe0f Character system isn't loaded yet.", ephemeral=True)
            return

        try:
            button.disabled = True
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        try:
            await cog._do_roll(interaction, from_button=True)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("/start roll delegation failed")
            try:
                await interaction.followup.send("\u26a0\ufe0f Roll failed due to an internal error.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="\u2b50 Enable premium (5 days)", style=discord.ButtonStyle.success)
    async def enable_premium_trial(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            button.disabled = True
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        try:
            uid = int(interaction.user.id)
            ok, msg = await grant_premium_trial(user_id=uid, days=5)
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(msg, ephemeral=True)
                    else:
                        await interaction.edit_original_response(content=msg, view=self)
                except Exception:
                    pass
            if ok:
                logger.info("Premium trial granted via /start (user_id=%s)", uid)
        except Exception:
            logger.exception("Failed to grant premium trial via /start")
            try:
                await interaction.followup.send("\u26a0\ufe0f Premium trial failed due to an internal error.", ephemeral=True)
            except Exception:
                pass


class SlashStart(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="start", description="Quick onboarding: get a free roll and learn the basics")
    async def start(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            guild_id = int(interaction.guild.id)
            user_id = int(interaction.user.id)

            granted = await grant_onboarding_roll(user_id=user_id)

            e = embed_kai_start(
                get_kai_start_greeting(),
                title="Welcome to Bot-Nardology \u2014 KAI here!",
            )
            e.description += (
                "\n\n**60-second setup:**\n"
                "1) **Roll** a character with **/character roll**\n"
                "2) **Add** it to your collection\n"
                "3) **Select** your active character with **/character select**\n"
                "4) **Chat** with it using **/talk**\n\n"
                "Your characters gain **bond XP** over time, and duplicates give **shards**. "
                "Use **/points daily** and **/points quests** to earn more \u2014 I'll be cheering you on!"
            )

            if granted:
                e.add_field(
                    name="\U0001f381 Free roll added",
                    value="You received **+1 bonus roll** to try it out.",
                    inline=False,
                )
            else:
                e.add_field(
                    name="Already started",
                    value="You've already claimed the onboarding roll \u2014 you can still roll anytime if you have rolls left today.",
                    inline=False,
                )

            e.add_field(
                name="\u200b",
                value=(
                    "By using KAI, you agree to our "
                    f"[Terms of Service]({config.TERMS_OF_SERVICE_URL}) and "
                    f"[Privacy Policy]({config.PRIVACY_POLICY_URL})."
                ),
                inline=False,
            )

            view = StartRollView(self.bot, user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(embed=e, view=view, ephemeral=True)
        except Exception:
            logger.exception("/start command failed")
            try:
                await interaction.followup.send(
                    "\u26a0\ufe0f Something went wrong during onboarding. Please try again!",
                    ephemeral=True,
                )
            except Exception:
                pass


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashStart") is None:
        await bot.add_cog(SlashStart(bot))
