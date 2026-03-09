# commands/slash/bond.py
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.bonds import (
    DAILY_XP_CAP_PER_CHARACTER,
    level_from_xp,
    title_for_level,
)
from utils.bonds_store import get_bond, upsert_bond_nickname
from utils.character_registry import BASE_STYLE_IDS, get_style


async def ac_bond_character(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for /bond character:
    - base: fun, serious
    - user's owned custom styles (global inventory)
    """
    cur = (current or "").strip().lower()

    # Only allow bonding with user-owned, non-server-default characters.
    style_ids: list[str] = []

    # owned custom styles (global)
    try:
        from utils.character_store import load_state  # local import
        st = await load_state(interaction.user.id)
        for sid in (st.owned_custom or []):
            sid = (sid or "").strip().lower()
            if not sid:
                continue
            if sid in {s.lower() for s in BASE_STYLE_IDS}:
                continue
            style_ids.append(sid)
    except Exception:
        pass

    # de-dupe preserving order
    seen = set()
    deduped: list[str] = []
    for sid in style_ids:
        sid = (sid or "").strip().lower()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        deduped.append(sid)

    # filter by typed text
    if cur:
        deduped = [sid for sid in deduped if cur in sid.lower()]

    choices: list[app_commands.Choice[str]] = []
    for sid in deduped[:25]:
        s = get_style(sid)
        if s:
            label = f"{s.display_name}  [{s.rarity}]"
        else:
            label = sid
        choices.append(app_commands.Choice(name=label, value=sid))
    return choices


class SlashBond(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    bond = app_commands.Group(name="bond", description="Bond progression with characters")

    @bond.command(name="view", description="View your bond with a character")
    @app_commands.autocomplete(character=ac_bond_character)
    @app_commands.describe(character="Character/style ID (e.g. fun, serious, samurai)")
    async def view(self, interaction: discord.Interaction, character: str | None = None):
        # guild only (bonds are per-server)
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server, not DMs.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id

        style_id = (character or "").strip().lower()
        if not style_id:
            # Default to the user's active character (if it's eligible)
            try:
                from utils.character_store import load_state  # local import
                st = await load_state(user_id)
                style_id = (getattr(st, "active_style_id", "") or "").strip().lower()
            except Exception:
                style_id = ""

        if not style_id or style_id in {s.lower() for s in BASE_STYLE_IDS}:
            await interaction.response.send_message(
                "Bonding is only for characters you own (not the server default). Use **/character roll** then **/character select**, or pick a character in this command.",
                ephemeral=True,
            )
            return

        b = await get_bond(guild_id=guild_id, user_id=user_id, style_id=style_id)

        xp = int(b.xp) if b else 0
        lvl = level_from_xp(xp)
        title = title_for_level(lvl)

        # Daily progress isn't tracked in the Bond record in this build.
        cap = int(DAILY_XP_CAP_PER_CHARACTER)

        s = get_style(style_id)
        display = s.display_name if s else style_id
        rarity = f"[{s.rarity}]" if s else ""

        nickname = (b.nickname or "").strip() if b else ""
        nickname_line = f"**Nickname:** {nickname}\n" if nickname else ""

        embed = discord.Embed(
            title=f"Bond — {display} {rarity}".strip(),
            description=f"**Level:** {lvl}\n**Title:** {title}\n{nickname_line}**XP:** {xp}\n**Daily cap:** {cap} (earned via /talk)",
            color=(s.color if s else 0x5865F2),
        )
        embed.set_footer(text="Bond XP is earned when /talk successfully sends a response.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bond.command(name="nickname", description="Set (or clear) a nickname for a character")
    @app_commands.autocomplete(character=ac_bond_character)
    @app_commands.describe(
        character="Character/style ID",
        nickname="Leave blank to clear (or type 'clear')",
    )
    async def nickname(self, interaction: discord.Interaction, character: str, nickname: str | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server, not DMs.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id

        style_id = (character or "").strip().lower()
        if not style_id:
            await interaction.response.send_message("Pick a character.", ephemeral=True)
            return

        if style_id in {s.lower() for s in BASE_STYLE_IDS}:
            await interaction.response.send_message(
                "Bonding is only for characters you own (not the server default).",
                ephemeral=True,
            )
            return

        nick = (nickname or "").strip()
        if not nick or nick.lower() == "clear":
            nick = None

        # light validation
        if nick and len(nick) > 32:
            await interaction.response.send_message("Nickname is too long (max 32 chars).", ephemeral=True)
            return

        await upsert_bond_nickname(guild_id=guild_id, user_id=user_id, style_id=style_id, nickname=nick)

        s = get_style(style_id)
        display = s.display_name if s else style_id
        if nick:
            await interaction.response.send_message(f"✅ Nickname set for **{display}**: **{nick}**", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Nickname cleared for **{display}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashBond") is None:
        await bot.add_cog(SlashBond(bot))
