# commands/slash/connection.py
"""Discord entrypoints for connection traits (shop also on web)."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from commands.slash.character import ac_character_select
from utils.start_required import require_start
from utils.connection_traits_store import (
    load_profile,
    purchase_trait,
    update_payload_fields,
    get_shard_balance,
    has_trait,
)

logger = logging.getLogger("bot.connection")


class SlashConnection(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    conn = app_commands.Group(name="connection", description="Connection traits (per-character, shard shop)")

    @conn.command(name="dashboard", description="Open the web dashboard for connection traits")
    @require_start()
    async def connection_dashboard(self, interaction: discord.Interaction):
        base = (config.BASE_URL or "").strip().rstrip("/")
        if not base:
            await interaction.response.send_message(
                "Web dashboard is not configured (set **BASE_URL** on the bot).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"**Connection traits dashboard:** {base}/connection\n"
            f"(Log in with Discord — same app OAuth redirect must match your developer portal.)",
            ephemeral=True,
        )

    @conn.command(name="traits", description="View shard balance and link to the dashboard")
    @require_start()
    async def connection_traits(self, interaction: discord.Interaction):
        uid = int(interaction.user.id)
        bal = await get_shard_balance(uid)
        base = (config.BASE_URL or "").strip().rstrip("/")
        link = f"{base}/connection" if base else "(configure BASE_URL)"
        await interaction.response.send_message(
            f"**Shards:** {bal}\n**Dashboard:** {link}",
            ephemeral=True,
        )

    @conn.command(name="edit_name", description="Pay 15 shards to set/change your name (requires Remember Name trait)")
    @require_start()
    @app_commands.describe(character="Character you own", name="Up to 10 words")
    @app_commands.autocomplete(character=ac_character_select)
    async def connection_edit_name(self, interaction: discord.Interaction, character: str, name: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = int(interaction.user.id)
        style_id = (character or "").strip().lower()
        data = await load_profile(user_id=uid, style_id=style_id)
        pur = data.get("purchased") or {}
        pl = data.get("payload") or {}
        if not has_trait(pur, "remember_name"):
            await interaction.followup.send(
                "⚠️ Purchase **Remember your name** for this character first (web dashboard).",
                ephemeral=True,
            )
            return
        had_name = bool((pl.get("display_name") or "").strip())
        if had_name:
            ok, msg = await purchase_trait(
                user_id=uid, style_id=style_id, trait_id="remember_name", kind="remember_name_edit"
            )
            if not ok:
                await interaction.followup.send("⚠️ " + msg, ephemeral=True)
                return
        ok2, msg2 = await update_payload_fields(
            user_id=uid, style_id=style_id, fields={"display_name": name}
        )
        if not ok2:
            await interaction.followup.send("⚠️ " + msg2, ephemeral=True)
            return
        note = "Paid **15** shards for name edit." if had_name else "First name set (no extra shard cost)."
        await interaction.followup.send(f"✅ {note}\n{msg2}", ephemeral=True)

    @conn.command(name="weekly_status", description="Set your weekly life update (250 words max)")
    @require_start()
    @app_commands.describe(character="Character", text="Up to 250 words")
    @app_commands.autocomplete(character=ac_character_select)
    async def connection_weekly_status(self, interaction: discord.Interaction, character: str, text: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = int(interaction.user.id)
        style_id = (character or "").strip().lower()
        data = await load_profile(user_id=uid, style_id=style_id)
        pur = data.get("purchased") or {}
        if not has_trait(pur, "weekly_life") and not has_trait(pur, "daily_status"):
            await interaction.followup.send(
                "⚠️ Purchase **Weekly life** or **Daily status** (web or future shop command).",
                ephemeral=True,
            )
            return
        ok, msg = await update_payload_fields(
            user_id=uid, style_id=style_id, fields={"weekly_status": text}
        )
        if not ok:
            await interaction.followup.send("⚠️ " + msg, ephemeral=True)
            return
        await interaction.followup.send("✅ " + msg, ephemeral=True)

    @conn.command(name="daily_status", description="Set today's daily note (100 words max; Daily status trait)")
    @require_start()
    @app_commands.describe(character="Character", text="Up to 100 words")
    @app_commands.autocomplete(character=ac_character_select)
    async def connection_daily_status(self, interaction: discord.Interaction, character: str, text: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = int(interaction.user.id)
        style_id = (character or "").strip().lower()
        data = await load_profile(user_id=uid, style_id=style_id)
        pur = data.get("purchased") or {}
        if not has_trait(pur, "daily_status"):
            await interaction.followup.send("⚠️ Purchase **Daily status** first.", ephemeral=True)
            return
        ok, msg = await update_payload_fields(
            user_id=uid, style_id=style_id, fields={"daily_today": text}
        )
        if not ok:
            await interaction.followup.send("⚠️ " + msg, ephemeral=True)
            return
        await interaction.followup.send("✅ " + msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashConnection(bot))
