# commands/slash/basic.py
from __future__ import annotations

import discord
import logging
import time


from discord.ext import commands
from discord import app_commands

from utils.audit import audit_log
from utils.say_limits import get_say_user_limiter, get_say_guild_limiter
from utils.say_store import insert_say
from utils.say_penalties import is_user_penalized, record_cooldown_strike
from utils.permissions import contains_mass_mention

logger = logging.getLogger("bot.basic")



class SlashBasic(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- /ping ----------
    @app_commands.command(name="ping", description="Check if the bot is alive")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pong! 🏓", ephemeral=True)

    # ---------- /hello ----------
    @app_commands.command(name="hello", description="Greet the bot")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Hello {interaction.user.name} 👋", ephemeral=True)

    # ---------- /say ----------
    @app_commands.command(name="say", description="Repeat a message")
    @app_commands.describe(message="What should I say?")
    async def say(self, interaction: discord.Interaction, message: str):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this command in a server, not DMs.")
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id

        # 0) If penalized, block immediately
        locked, remaining = await is_user_penalized(guild_id, user_id)
        if locked:
            audit_log(
                "SAY_DENIED_PENALIZED",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="denied",
                reason="penalized",
                fields={"remaining_s": remaining},
            )

            await safe_ephemeral_send(
                interaction,
                f"⛔ You’re temporarily blocked from using `/say` for **{remaining}s** (spam penalty).",
            )
            return


        # 1) Validate input early (avoids consuming limiter capacity)
        message = (message or "").strip()
        if not message:
            await safe_ephemeral_send(interaction, "Please provide a message.")
            return

        if len(message) > 1500:
            audit_log(
                "SAY_DENIED_TOO_LONG",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="denied",
                reason="too_long",
                fields={"message_len": len(message)},
            )
            await safe_ephemeral_send(interaction, "Message too long (max 1500 characters).")
            return

        # 2) Block mass mentions for non-admins
        if contains_mass_mention(message):
            member = interaction.user
            if isinstance(member, discord.Member):
                if not (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
                    audit_log(
                        "SAY_BLOCKED_MASS_MENTION",
                        guild_id=guild_id,
                        channel_id=interaction.channel_id,
                        user_id=user_id,
                        username=interaction.user.name,
                        command="say",
                        result="denied",
                        reason="mass_mention",
                    )
                    await safe_ephemeral_send(interaction, "Mass mentions like @everyone/@here aren’t allowed.")
                    return

        # 3) Dynamic limiters (read from DB via say_limits.py)
        user_limiter = await get_say_user_limiter(guild_id)
        guild_limiter = await get_say_guild_limiter(guild_id)

        # 4) Per-user rate limit (strike -> penalty escalation)
        u = await user_limiter.check(f"user:{user_id}")
        if not u.allowed:
            entry = await record_cooldown_strike(guild_id, user_id)

            # If a penalty was applied, penalty_until will be in the future.
            now = int(time.time())
            penalty_until = int(entry.get("penalty_until", 0) or 0)
            penalty_seconds = max(0, penalty_until - now) if penalty_until else 0
            penalty_applied = penalty_seconds > 0

            audit_log(
                "SAY_COOLDOWN_USER",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="cooldown",
                reason="user_rate_limit",
                fields={
                    "retry_after_s": u.retry_after_seconds,
                    "penalty_applied": penalty_applied,
                    "penalty_seconds": penalty_seconds,
                },
            )

            if penalty_applied:
                await safe_ephemeral_send(
                    interaction,
                    f"⛔ Too many `/say` attempts. Penalty applied: **{penalty_seconds}s**.",
                )
            else:
                await safe_ephemeral_send(
                    interaction,
                    f"⏳ Slow down—try again in **{u.retry_after_seconds}s**. (Strike recorded)",
                )
            return

        # 5) Per-guild rate limit (deny but do NOT strike the user)
        g = await guild_limiter.check(f"guild:{guild_id}")
        if not g.allowed:
            audit_log(
                "SAY_COOLDOWN_GUILD",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="cooldown",
                reason="guild_rate_limit",
                fields={"retry_after_s": g.retry_after_seconds},
            )
            await safe_ephemeral_send(
                interaction,
                f"⏳ This server is on cooldown. Try again in **{g.retry_after_seconds}s**.",
            )
            return

        # 6) Success
        try:
            await interaction.response.send_message(message)

            # DB log (never crash command if DB is down)
            # Insert only guild_id and user_id: the Redis-based insert_say
            # function does not accept channel_id or message_len.  Passing
            # extra arguments would raise a TypeError, so restrict to the
            # supported parameters.
            try:
                await insert_say(
                    guild_id=guild_id,
                    user_id=user_id,
                )
            except Exception:
                logger.exception("insert_say failed")

            # Audit success regardless of DB outcome
            audit_log(
                "SAY_SUCCESS",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="success",
                fields={"message_len": len(message)},
            )

        except discord.Forbidden:
            audit_log(
                "SAY_DENIED",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="denied",
                reason="forbidden",
            )
            await safe_ephemeral_send(interaction, "I don’t have permission to send messages here.")
        except discord.HTTPException:
            audit_log(
                "SAY_ERROR",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="say",
                result="error",
                reason="HTTPException",
            )
            logger.warning("SAY send failed", exc_info=True)
            await safe_ephemeral_send(interaction, "Something went wrong sending that message.")




    # ---------- /add ----------
    @app_commands.command(name="add", description="Add two numbers")
    async def add(self, interaction: discord.Interaction, a: int, b: int):
        await interaction.response.send_message(f"{a} + {b} = {a + b}", ephemeral=True)


# ---------- Context menu: React 🔥 ----------
@app_commands.context_menu(name="React 🔥")
async def react_fire(interaction: discord.Interaction, message: discord.Message):
    if interaction.guild is None:
        await safe_ephemeral_send(interaction, "Use this in a server, not DMs.")
        return

    try:
        await message.add_reaction("🔥")
        await safe_ephemeral_send(interaction, "Added 🔥 reaction.")
    except discord.Forbidden:
        audit_log(
            "REACT_DENIED",
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="context.react_fire",
            result="denied",
            reason="forbidden",
        )
        await safe_ephemeral_send(interaction, "I don’t have permission to add reactions here.")
    except discord.HTTPException:
        audit_log(
            "REACT_ERROR",
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="context.react_fire",
            result="error",
            reason="HTTPException",
        )
        logger.warning("React 🔥 failed", exc_info=True)
        await safe_ephemeral_send(interaction, "Something went wrong adding that reaction.")


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashBasic") is None:
        await bot.add_cog(SlashBasic(bot))

    # Guard against re-adding context menu in dev reloads
    existing = bot.tree.get_command("React 🔥", type=discord.AppCommandType.message)
    if existing is None:
        bot.tree.add_command(react_fire)
