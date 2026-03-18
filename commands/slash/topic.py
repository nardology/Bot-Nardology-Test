from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.ui import safe_ephemeral_send
from utils.daily_topic import set_daily_topic, get_or_rotate_daily_topic

logger = logging.getLogger("bot.topic")


class SlashTopic(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    topic = app_commands.Group(name="topic", description="Daily topic settings (admin)")

    @topic.command(name="set", description="Set the daily topic and its meaning (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        topic="Short topic name (e.g. 'cats')",
        description="What the topic means / what counts (helps detection)",
        examples="Optional: example prompts users could say (separate with ';')",
    )
    async def topic_set(
        self,
        interaction: discord.Interaction,
        topic: str,
        description: str = "",
        examples: str = "",
    ):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return
        t = (topic or "").strip()
        if len(t) < 2:
            await safe_ephemeral_send(interaction, "Topic is too short.")
            return
        if len(t) > 120:
            t = t[:120]
        desc = (description or "").strip()
        if len(desc) > 1200:
            desc = desc[:1200]
        ex_list = []
        if examples:
            # Simple delimiter so admins can paste quickly.
            for part in str(examples).split(";"):
                p = part.strip()
                if p:
                    ex_list.append(p[:200])
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        saved = await set_daily_topic(
            guild_id=int(interaction.guild.id),
            topic_text=t,
            topic_description=desc,
            examples=ex_list,
        )
        if not saved:
            await safe_ephemeral_send(interaction, "⚠️ Could not save topic (DB unavailable).")
            return
        msg = f"✅ Topic set to **{saved.topic_text}**."
        if saved.topic_description:
            msg += f"\nMeaning: {saved.topic_description[:600]}"
        if saved.examples:
            msg += "\nExamples:\n" + "\n".join([f"- {x}" for x in saved.examples[:5]])
        await safe_ephemeral_send(interaction, msg)

    @topic.command(name="view", description="View the current daily topic")
    async def topic_view(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        cur = await get_or_rotate_daily_topic(guild_id=int(interaction.guild.id))
        if not cur or not cur.topic_text.strip():
            await safe_ephemeral_send(interaction, "No topic is set yet. Admins can set one with `/topic set`.")
            return
        msg = f"**Today’s topic:** **{cur.topic_text}**"
        if cur.topic_description:
            msg += f"\n**Meaning:** {cur.topic_description[:900]}"
        if cur.examples:
            msg += "\n**Examples:**\n" + "\n".join([f"- {x}" for x in cur.examples[:5]])
        await safe_ephemeral_send(interaction, msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashTopic(bot))

