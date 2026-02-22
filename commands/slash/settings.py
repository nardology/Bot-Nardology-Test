# commands/slash/settings.py
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.ui import safe_ephemeral_send

from utils.audit import audit_log
from utils.premium import clamp_int, get_caps, get_premium_tier
from utils.storage import get_guild_settings, set_guild_setting, list_add, list_remove

# Use the new style registry instead of utils.styles (prevents crashes after renames)
from utils.character_registry import BASE_STYLE_IDS, get_style

logger = logging.getLogger("bot.settings")

ALLOWED_LANGUAGES = ["english", "spanish", "french"]


def _audit(interaction: discord.Interaction, action: str, *, result: str, fields: Optional[dict] = None) -> None:
    try:
        audit_log(
            action,
            guild_id=getattr(interaction.guild, "id", None),
            channel_id=getattr(interaction, "channel_id", None),
            user_id=getattr(interaction.user, "id", None),
            username=getattr(interaction.user, "name", "unknown"),
            command="settings",
            result=result,
            fields=fields or {},
        )
    except Exception:
        logger.exception("audit_log failed")



def _normalize_style_id(x: str) -> str:
    return (x or "").strip().lower().replace(" ", "_")


def _style_exists(style_id: str) -> bool:
    sid = _normalize_style_id(style_id)
    if sid in BASE_STYLE_IDS:
        return True
    return get_style(sid) is not None


def _style_label(style_id: str) -> str:
    s = get_style(style_id)
    return s.display_name if s else style_id


class SlashSettings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    settings = app_commands.Group(name="settings", description="Server settings for Bot-Nardology")

    # Premium tier is now controlled via owner-only tools (see /owner premium ...).
    # Keeping it out of /settings reduces accidental toggles by server staff.
    ai = app_commands.Group(name="ai", description="AI access controls", parent=settings)
    say = app_commands.Group(name="say", description="Say/voice rate limits", parent=settings)
    announce = app_commands.Group(name="announce", description="Announcements", parent=settings)

    # ----- General -----

    @settings.command(name="show", description="Show current server settings")
    async def settings_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        s = await get_guild_settings(interaction.guild.id)

        # Friendly defaults
        language = s.get("language", "english")
        style_id = s.get("style", "fun")  # keep key name "style" for compatibility
        tier = await get_premium_tier(int(interaction.user.id))

        msg = (
            f"**Current settings**\n"
            f"- Language: `{language}`\n"
            f"- Default character: `{style_id}`\n"
            f"- Premium: `{tier}`\n"
        )
        await safe_ephemeral_send(interaction, msg)

    # ----- Announcements -----

    @announce.command(name="channel", description="Set the server announcement channel used by broadcasts")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel where bot announcements should be posted")
    async def announce_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        await set_guild_setting(int(interaction.guild.id), "announce_channel_id", int(channel.id))
        _audit(interaction, "SET_ANNOUNCE_CHANNEL", result="success", fields={"channel_id": int(channel.id)})
        await safe_ephemeral_send(interaction, f"✅ Announcement channel set to {channel.mention}.")

    @announce.command(name="clear_channel", description="Clear the server announcement channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce_clear_channel(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        await set_guild_setting(int(interaction.guild.id), "announce_channel_id", 0)
        _audit(interaction, "CLEAR_ANNOUNCE_CHANNEL", result="success", fields={})
        await safe_ephemeral_send(interaction, "✅ Announcement channel cleared.")

    @announce.command(name="show", description="Show the configured announcement channel")
    async def announce_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        s = await get_guild_settings(int(interaction.guild.id))
        ch_id = int(s.get("announce_channel_id", 0) or 0)
        if ch_id:
            await safe_ephemeral_send(interaction, f"Announcement channel: <#{ch_id}> (`{ch_id}`)")
        else:
            await safe_ephemeral_send(interaction, "Announcement channel: *(not set)*")

    @settings.command(name="language", description="Set bot language for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(language=[app_commands.Choice(name=l, value=l) for l in ALLOWED_LANGUAGES])
    async def settings_language(self, interaction: discord.Interaction, language: app_commands.Choice[str]):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        await set_guild_setting(interaction.guild.id, "language", language.value)
        _audit(interaction, "SET_LANGUAGE", result="success", fields={"language": language.value})
        await safe_ephemeral_send(interaction, f"✅ Language set to `{language.value}`.")

    @settings.command(name="character", description="Set the server's default character")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(character="Character/style id (autocomplete)")
    async def settings_character(self, interaction: discord.Interaction, character: str):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        style_norm = _normalize_style_id(character)
        if not _style_exists(style_norm):
            await safe_ephemeral_send(interaction, "Unknown character. Try a different one.")
            return

        # Keep storage key as "style" so you don't break existing data
        await set_guild_setting(interaction.guild.id, "style", style_norm)
        _audit(interaction, "SET_STYLE", result="success", fields={"style": style_norm})
        await safe_ephemeral_send(interaction, f"✅ Default character set to **{_style_label(style_norm)}** (`{style_norm}`).")

    @settings_character.autocomplete("character")
    async def character_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = (current or "").strip().lower()
        # Show base styles + registry-known styles that match
        # NOTE: get_style lookup is cheap; just scan a few likely ids:
        # If you have a big registry list, add an exported list of IDs later.
        candidates = list(sorted(BASE_STYLE_IDS))

        # Also include some common ones if they exist
        # (Optional heuristic; won't crash if missing)
        for sid in ["pirate", "anime", "noir", "wizard", "villain", "hero"]:
            if _style_exists(sid):
                candidates.append(sid)

        # de-dupe
        seen = set()
        out = []
        for sid in candidates:
            if sid in seen:
                continue
            seen.add(sid)
            s = get_style(sid)
            dn = (s.display_name.lower() if s else "")
            if not cur or (cur in sid.lower() or (dn and cur in dn)):
                out.append(
                    app_commands.Choice(
                        name=f"{_style_label(sid)} ({sid})",
                        value=sid,
                    )
                )
        return out[:25]

    # ----- AI access lists -----

    @ai.command(name="allow-role", description="Allow a role to use AI commands")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_allow_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        ok = await list_add(interaction.guild.id, "ai_allowed_role_ids", int(role.id))
        await safe_ephemeral_send(interaction, "✅ Added." if ok else "Already allowed.")

    @ai.command(name="block-role", description="Block a role from using AI commands")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_block_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        ok = await list_add(interaction.guild.id, "ai_blocked_role_ids", int(role.id))
        await safe_ephemeral_send(interaction, "✅ Added." if ok else "Already blocked.")

    @ai.command(name="unallow-role", description="Remove a role from the AI allow list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_unallow_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        ok = await list_remove(interaction.guild.id, "ai_allowed_role_ids", int(role.id))
        await safe_ephemeral_send(interaction, "✅ Removed." if ok else "Role not in allow list.")

    @ai.command(name="unblock-role", description="Remove a role from the AI block list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_unblock_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        ok = await list_remove(interaction.guild.id, "ai_blocked_role_ids", int(role.id))
        await safe_ephemeral_send(interaction, "✅ Removed." if ok else "Role not in block list.")
    @ai.command(name="allow-channel", description="Allow AI commands in a channel (global or per-command)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(scope="all, talk, or scene", channel="Channel to allow")
    @app_commands.choices(scope=[
        app_commands.Choice(name="all (global)", value="all"),
        app_commands.Choice(name="talk only", value="talk"),
        app_commands.Choice(name="scene only", value="scene"),
    ])
    async def ai_allow_channel(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        if scope.value == "all":
            key = "ai_allowed_channel_ids"
        else:
            key = f"{scope.value}_allowed_channel_ids"

        ok = await list_add(interaction.guild.id, key, int(channel.id))
        await safe_ephemeral_send(interaction, f"✅ Allowed {channel.mention} for `{scope.value}`." if ok else "Already allowed.")

    @ai.command(name="unallow-channel", description="Remove a channel from the allow list (global or per-command)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(scope="all, talk, or scene", channel="Channel to remove")
    @app_commands.choices(scope=[
        app_commands.Choice(name="all (global)", value="all"),
        app_commands.Choice(name="talk only", value="talk"),
        app_commands.Choice(name="scene only", value="scene"),
    ])
    async def ai_unallow_channel(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        if scope.value == "all":
            key = "ai_allowed_channel_ids"
        else:
            key = f"{scope.value}_allowed_channel_ids"

        ok = await list_remove(interaction.guild.id, key, int(channel.id))
        await safe_ephemeral_send(interaction, f"✅ Removed {channel.mention} for `{scope.value}`." if ok else "That channel wasn’t on the list.")
    @ai.command(name="safety-mode", description="Set AI safety mode (standard or strict)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(mode=[
        app_commands.Choice(name="standard", value="standard"),
        app_commands.Choice(name="strict", value="strict"),
    ])
    async def ai_safety_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        await set_guild_setting(interaction.guild.id, "ai_safety_mode", mode.value)
        await safe_ephemeral_send(interaction, f"✅ Safety mode set to `{mode.value}`.")

    @ai.command(name="block-topic", description="Add a blocked topic (simple substring match)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(topic="A word or phrase to block")
    async def ai_block_topic(self, interaction: discord.Interaction, topic: str):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        topic = (topic or "").strip()
        if len(topic) < 2:
            await safe_ephemeral_send(interaction, "Topic is too short.")
            return
        ok = await list_add(interaction.guild.id, "ai_blocked_topics", topic)
        await safe_ephemeral_send(interaction, "✅ Added." if ok else "Already blocked.")

    @ai.command(name="unblock-topic", description="Remove a blocked topic")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(topic="Exact blocked topic entry to remove")
    async def ai_unblock_topic(self, interaction: discord.Interaction, topic: str):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        ok = await list_remove(interaction.guild.id, "ai_blocked_topics", (topic or "").strip())
        await safe_ephemeral_send(interaction, "✅ Removed." if ok else "That topic wasn’t on the list.")

    @ai.command(name="list-topics", description="List blocked topics")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_list_topics(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return
        s = await get_guild_settings(interaction.guild.id)
        topics = s.get("ai_blocked_topics", [])
        if not isinstance(topics, list) or not topics:
            await safe_ephemeral_send(interaction, "No blocked topics set.")
            return
        msg = "**Blocked topics**\n" + "\n".join([f"- `{t}`" for t in topics if isinstance(t, str)])
        await safe_ephemeral_send(interaction, msg)

    @ai.command(name="list-channels", description="Show allowed channels (global + per-command)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_list_channels(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        s = await get_guild_settings(interaction.guild.id)
        def _fmt(ids):
            if not isinstance(ids, list) or not ids:
                return "*(none)*"
            return "\n".join([f"<#{int(x)}>" for x in ids if str(x).isdigit()])

        msg = (
            "**Allowed AI Channels (global)**\n"
            f"{_fmt(s.get('ai_allowed_channel_ids', []))}\n\n"
            "**Allowed /talk Channels**\n"
            f"{_fmt(s.get('talk_allowed_channel_ids', []))}\n\n"
            "**Allowed /scene Channels**\n"
            f"{_fmt(s.get('scene_allowed_channel_ids', []))}"
        )
        await safe_ephemeral_send(interaction, msg)


    # ----- AI limits -----

    @ai.command(name="limits", description="Set /talk AI rate limits (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        user_max="Max AI uses per user in window",
        user_window="Seconds for per-user window",
        guild_max="Max AI uses per guild in window",
        guild_window="Seconds for per-guild window",
    )
    async def ai_limits(self, interaction: discord.Interaction, user_max: int, user_window: int, guild_max: int, guild_window: int):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        caps = await get_caps(int(interaction.user.id))

        user_max = clamp_int(user_max, min_value=caps.ai_user_max_min, max_value=caps.ai_user_max_max)
        user_window = clamp_int(user_window, min_value=caps.ai_user_window_min, max_value=caps.ai_user_window_max)
        guild_max = clamp_int(guild_max, min_value=caps.ai_guild_max_min, max_value=caps.ai_guild_max_max)
        guild_window = clamp_int(guild_window, min_value=caps.ai_guild_window_min, max_value=caps.ai_guild_window_max)

        await set_guild_setting(interaction.guild.id, "ai_user_max", user_max)
        await set_guild_setting(interaction.guild.id, "ai_user_window", user_window)
        await set_guild_setting(interaction.guild.id, "ai_guild_max", guild_max)
        await set_guild_setting(interaction.guild.id, "ai_guild_window", guild_window)

        _audit(
            interaction,
            "SET_AI_LIMITS",
            result="success",
            fields={
                "ai_user_max": user_max,
                "ai_user_window": user_window,
                "ai_guild_max": guild_max,
                "ai_guild_window": guild_window,
            },
        )

        await safe_ephemeral_send(
            interaction,
            f"✅ Updated AI limits for `/talk`. user={user_max}/{user_window}s guild={guild_max}/{guild_window}s",
        )

    # ----- Say limits -----

    @say.command(name="limits", description="Set /say rate limits (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        user_max="Max /say per user in window",
        user_window="Seconds for per-user window",
        guild_max="Max /say per guild in window",
        guild_window="Seconds for per-guild window",
    )
    async def say_limits(self, interaction: discord.Interaction, user_max: int, user_window: int, guild_max: int, guild_window: int):
        if interaction.guild is None:
            await safe_ephemeral_send(interaction, "This command can only be used in a server.")
            return

        user_max = clamp_int(user_max, min_value=1, max_value=50)
        user_window = clamp_int(user_window, min_value=5, max_value=86400)
        guild_max = clamp_int(guild_max, min_value=1, max_value=200)
        guild_window = clamp_int(guild_window, min_value=5, max_value=86400)

        await set_guild_setting(interaction.guild.id, "say_user_max", user_max)
        await set_guild_setting(interaction.guild.id, "say_user_window", user_window)
        await set_guild_setting(interaction.guild.id, "say_guild_max", guild_max)
        await set_guild_setting(interaction.guild.id, "say_guild_window", guild_window)

        await safe_ephemeral_send(
            interaction,
            f"✅ Updated /say limits. user={user_max}/{user_window}s guild={guild_max}/{guild_window}s",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashSettings(bot))


