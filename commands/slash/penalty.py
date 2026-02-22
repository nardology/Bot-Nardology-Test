# commands/slash/penalty.py
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core.ui import safe_ephemeral_send

from utils.audit import audit_log
from utils.owner import is_bot_owner
from utils.storage import get_guild_setting, set_guild_setting

from utils.say_penalties import is_user_penalized as is_say_penalized

try:
    from utils.AI_penalties import is_user_penalized as is_ai_penalized
except Exception:
    from utils.ai_penalties import is_user_penalized as is_ai_penalized  # type: ignore

logger = logging.getLogger("bot.penalty")

AI_PENALTY_KEY = "ai_penalties"
SAY_PENALTY_KEY = "say_penalties"



def _now() -> int:
    return int(time.time())


def _format_seconds(s: int) -> str:
    if s <= 0:
        return "0s"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m {s%60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _clear_penalty_entry(penalties: dict, user_id: int) -> bool:
    key_variants = [str(user_id), user_id]
    changed = False
    for k in key_variants:
        if k in penalties:
            penalties.pop(k, None)
            changed = True
    return changed


class SlashPenalty(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    penalty = app_commands.Group(name="penalty", description="View/reset spam penalties")

    @penalty.command(name="view", description="View a user's active penalty status")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def penalty_view(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return

        guild_id = interaction.guild.id
        uid = user.id

        ai_locked, ai_remaining = await is_ai_penalized(guild_id, uid)
        say_locked, say_remaining = await is_say_penalized(guild_id, uid)

        msg = (
            f"**Penalty status for {user.mention}**\n"
            f"- AI (/talk): {'⛔ ACTIVE' if ai_locked else '✅ none'}"
            + (f" (**{_format_seconds(ai_remaining)}** left)" if ai_locked else "")
            + "\n"
            f"- /say: {'⛔ ACTIVE' if say_locked else '✅ none'}"
            + (f" (**{_format_seconds(say_remaining)}** left)" if say_locked else "")
        )

        audit_log(
            "PENALTY_VIEW",
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="penalty.view",
            result="success",
            fields={"target_user_id": uid},
        )

        await safe_ephemeral_send(interaction, msg)

    @penalty.command(name="reset", description="Reset a user's penalties (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(2, 30.0)
    @app_commands.describe(kind="Which penalty to reset")
    @app_commands.choices(kind=[
        app_commands.Choice(name="ai (/talk)", value="ai"),
        app_commands.Choice(name="say (/say)", value="say"),
        app_commands.Choice(name="both", value="both"),
    ])
    async def penalty_reset(self, interaction: discord.Interaction, user: discord.Member, kind: app_commands.Choice[str]):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "Use this in a server.")
            return

        guild_id = interaction.guild.id
        target_id = user.id

        if not is_bot_owner(interaction.user.id):
            pass

        changed_ai = False
        changed_say = False

        ai_map = await get_guild_setting(guild_id, AI_PENALTY_KEY, default={})
        say_map = await get_guild_setting(guild_id, SAY_PENALTY_KEY, default={})

        if not isinstance(ai_map, dict):
            ai_map = {}
        if not isinstance(say_map, dict):
            say_map = {}

        if kind.value in ("ai", "both"):
            changed_ai = _clear_penalty_entry(ai_map, target_id)
            await set_guild_setting(guild_id, AI_PENALTY_KEY, ai_map)

        if kind.value in ("say", "both"):
            changed_say = _clear_penalty_entry(say_map, target_id)
            await set_guild_setting(guild_id, SAY_PENALTY_KEY, say_map)

        audit_log(
            "PENALTY_RESET",
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            username=interaction.user.name,
            command="penalty.reset",
            result="success",
            fields={
                "target_user_id": target_id,
                "kind": kind.value,
                "changed_ai": changed_ai,
                "changed_say": changed_say,
            },
        )

        status_parts = []
        if kind.value in ("ai", "both"):
            status_parts.append("AI ✅" if changed_ai else "AI (none)")
        if kind.value in ("say", "both"):
            status_parts.append("/say ✅" if changed_say else "/say (none)")

        await safe_ephemeral_send(interaction, f"✅ Reset penalties for {user.mention}: " + ", ".join(status_parts))


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashPenalty(bot))

