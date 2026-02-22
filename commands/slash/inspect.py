# commands/slash/inspect.py
"""Inspect a member's stats or your own (public/private)."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.stats import get_user_stats, UserStats
from utils.character_registry import get_style
from utils.character_store import compute_limits, get_inventory_upgrades
from utils.cosmetics import cosmetic_image_url, default_cosmetic_image_url
from utils.premium import get_premium_tier
from utils.cosmetics_store import get_selected
from utils.media_assets import resolve_embed_image_url, fetch_embed_image_as_file

logger = logging.getLogger("bot.inspect")


def _add_stats_fields(e: discord.Embed, stats: UserStats, *, max_inventory_slots: Optional[int] = None) -> None:
    """Add stats fields to an embed."""
    e.add_field(name="Points", value=f"**{stats.points}**", inline=True)
    e.add_field(name="Daily streak", value=f"**{stats.daily_streak}** day(s)", inline=True)
    if max_inventory_slots is not None:
        e.add_field(
            name="Inventory",
            value=f"**{stats.characters_owned_count}** / **{max_inventory_slots}**",
            inline=True,
        )
    e.add_field(
        name="Characters owned",
        value=f"**{stats.characters_owned_count}**",
        inline=True,
    )
    if stats.selected_character_name:
        e.add_field(
            name="Selected character",
            value=stats.selected_character_name,
            inline=True,
        )
    else:
        e.add_field(name="Selected character", value="*(none)*", inline=True)

    if stats.highest_bond:
        e.add_field(
            name="Highest bond",
            value=f"**{stats.highest_bond.character_name}** — {stats.highest_bond.title} (Lv.{stats.highest_bond.level})",
            inline=True,
        )
    e.add_field(name="Total bond XP", value=f"**{stats.total_bond_xp}**", inline=True)

    if stats.character_ids:
        count = len(stats.character_ids)
        e.add_field(
            name="Characters",
            value=f"**{count}** owned \u2014 use the dropdown below to inspect",
            inline=False,
        )


def _build_inspect_embeds(
    stats: UserStats,
    display_name: str,
    *,
    is_self: bool,
    character_image_url: str | None = None,
    cosmetic_image_url: str | None = None,
    max_inventory_slots: Optional[int] = None,
) -> list[discord.Embed]:
    """Build one or two embeds for /inspect. When both images exist: cosmetic (large) on top, then stats + character (large) below."""
    title = "Your stats" if is_self else f"Stats for {display_name}"
    color = 0x5865F2
    footer = "KAI · your friendly robot cat"

    def add_fields(emb: discord.Embed) -> None:
        _add_stats_fields(emb, stats, max_inventory_slots=max_inventory_slots)

    has_cosmetic = bool(cosmetic_image_url)
    has_character = bool(character_image_url)

    if has_cosmetic and has_character:
        # Two embeds: cosmetic large on top, then stats + character large below
        top = discord.Embed(title=title, color=color)
        top.set_image(url=cosmetic_image_url)
        top.set_footer(text=footer)
        bottom = discord.Embed(color=color)
        add_fields(bottom)
        bottom.set_image(url=character_image_url)
        bottom.set_footer(text=footer)
        return [top, bottom]
    if has_cosmetic:
        e = discord.Embed(title=title, color=color)
        e.set_image(url=cosmetic_image_url)
        add_fields(e)
        e.set_footer(text=footer)
        return [e]
    if has_character:
        e = discord.Embed(title=title, color=color)
        e.set_image(url=character_image_url)
        add_fields(e)
        e.set_footer(text=footer)
        return [e]
    e = discord.Embed(title=title, color=color)
    add_fields(e)
    e.set_footer(text=footer)
    return [e]


# ---------------------------------------------------------------------------
# Rarity formatting helpers
# ---------------------------------------------------------------------------

RARITY_EMOJI = {
    "common": "\u2b1c",
    "uncommon": "\U0001f7e9",
    "rare": "\U0001f7e6",
    "epic": "\U0001f7ea",
    "legendary": "\U0001f7e8",
    "mythic": "\u2b50",
}


def _char_label(style_id: str) -> str:
    """Format a character for a dropdown option label."""
    sid = (style_id or "").strip().lower()
    s = get_style(sid)
    if not s:
        return sid or style_id
    r = (str(getattr(s, "rarity", "")) or "").lower().strip()
    emoji = RARITY_EMOJI.get(r, "\u2754")
    return f"{s.display_name}  {emoji} {r or 'unknown'}"


# ---------------------------------------------------------------------------
# Character inspect dropdown
# ---------------------------------------------------------------------------

class CharacterInspectSelect(discord.ui.Select):
    """Dropdown that shows owned characters; selecting one shows details."""

    def __init__(self, target_id: int, character_ids: list[str], allowed_user_id: int):
        self.target_id = target_id
        self.allowed_user_id = allowed_user_id

        options: list[discord.SelectOption] = []
        for sid in character_ids[:25]:
            sid_lower = (sid or "").strip().lower()
            s = get_style(sid_lower)
            label = _char_label(sid_lower)[:100]
            desc = None
            if s and getattr(s, "description", None):
                desc = str(s.description)[:100]
            options.append(discord.SelectOption(label=label, value=sid_lower, description=desc))

        if not options:
            options = [discord.SelectOption(label="No characters", value="none")]

        super().__init__(placeholder="Select a character to inspect\u2026", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.allowed_user_id:
            try:
                await interaction.response.send_message("This menu isn\u2019t yours.", ephemeral=True)
            except Exception:
                pass
            return

        sid = (self.values[0] if self.values else "").strip().lower()
        if sid == "none":
            return

        s = get_style(sid)
        if not s:
            await interaction.response.send_message("Character not found.", ephemeral=True)
            return

        rarity_str = (str(getattr(s, "rarity", "")) or "unknown").lower()
        emoji = RARITY_EMOJI.get(rarity_str, "\u2754")
        color = int(getattr(s, "color", 0x5865F2) or 0x5865F2)

        embed = discord.Embed(
            title=f"{s.display_name}  {emoji} {rarity_str}",
            color=color,
        )
        if getattr(s, "description", None):
            embed.description = str(s.description)

        # Streak info
        try:
            from utils.character_streak import get_character_streak, is_character_streak_alive
            streak = await get_character_streak(user_id=self.target_id, style_id=sid)
            alive = await is_character_streak_alive(user_id=self.target_id, style_id=sid)
            if streak > 0:
                status = "\u2705 Active" if alive else "\u274c Broken"
                embed.add_field(name="Talk streak", value=f"**{streak}** day{'s' if streak != 1 else ''} ({status})", inline=True)
        except Exception:
            pass

        # Bond info
        try:
            from utils.bond import get_bond
            bond = await get_bond(user_id=self.target_id, style_id=sid)
            if bond and getattr(bond, "level", 0) > 0:
                title_str = getattr(bond, "title", "") or ""
                embed.add_field(
                    name="Bond",
                    value=f"Lv.{bond.level} \u2014 {title_str}" if title_str else f"Lv.{bond.level}",
                    inline=True,
                )
        except Exception:
            pass

        # Pack info
        pack_id = getattr(s, "pack_id", "core") or "core"
        if pack_id != "core":
            embed.add_field(name="Pack", value=pack_id, inline=True)

        # Tips
        tips = getattr(s, "tips", None)
        if tips and isinstance(tips, list) and tips:
            embed.add_field(name="Tips", value="\n".join(f"\u2022 {t}" for t in tips[:5]), inline=False)

        # Character image
        files: list[discord.File] = []
        img_url = getattr(s, "image_url", None)
        if img_url and isinstance(img_url, str):
            resolved = resolve_embed_image_url(img_url)
            f = await fetch_embed_image_as_file(resolved, filename="inspect_detail.png")
            if f:
                files.append(f)
                embed.set_thumbnail(url="attachment://inspect_detail.png")

        try:
            if files:
                await interaction.response.send_message(embed=embed, files=files, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            logger.debug("CharacterInspectSelect callback failed", exc_info=True)


class CharacterInspectView(discord.ui.View):
    """View wrapping the character inspect dropdown."""

    def __init__(self, target_id: int, character_ids: list[str], allowed_user_id: int):
        super().__init__(timeout=180)
        self.add_item(CharacterInspectSelect(target_id, character_ids, allowed_user_id))


class SlashInspect(commands.Cog):
    """Inspect a server member's stats or your own (public or private)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="inspect", description="View stats for a member or yourself (public or private)")
    @app_commands.describe(
        user="Member to inspect (leave blank to view your own stats)",
        public="If viewing your own: show in channel (true) or only to you (false)",
    )
    async def inspect(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        public: bool = False,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Use this command in a server.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        target_id = int(user.id) if user else int(interaction.user.id)
        display_name = (user.display_name if user else interaction.user.display_name) or "Someone"
        is_self = target_id == interaction.user.id

        try:
            stats = await get_user_stats(user_id=target_id, guild_id=guild_id)
        except Exception:
            logger.exception("get_user_stats failed for user_id=%s", target_id)
            await interaction.response.send_message(
                "Could not load stats. Try again later.",
                ephemeral=True,
            )
            return

        # Max inventory slots (base by tier + upgrades) for this server
        max_inventory_slots: Optional[int] = None
        try:
            tier = await get_premium_tier(target_id)
            is_pro = tier == "pro"
            _, base_slots = compute_limits(is_pro=is_pro)
            upgrades = int(await get_inventory_upgrades(target_id) or 0)
            max_inventory_slots = int(base_slots) + (upgrades * 5)
        except Exception:
            pass

        # Defer early so Discord doesn't time out while we fetch images.
        ephemeral = is_self and not public
        await interaction.response.defer(ephemeral=ephemeral)

        character_image_url: str | None = None
        if stats.selected_character_id:
            style = get_style(stats.selected_character_id)
            if style and getattr(style, "image_url", None):
                url = getattr(style, "image_url", None)
                if url and isinstance(url, str):
                    character_image_url = resolve_embed_image_url(url)

        selected_cosmetic_id = await get_selected(target_id)
        cosmetic_img_url: str | None = cosmetic_image_url(selected_cosmetic_id) if selected_cosmetic_id else None

        # Fetch images and attach as files so they always display (Discord often fails to load external URLs).
        files: list[discord.File] = []
        char_attach_name = "inspect_char.png"
        cosmetic_attach_name = "inspect_cosmetic.png"
        if character_image_url:
            f = await fetch_embed_image_as_file(character_image_url, filename=char_attach_name)
            if f:
                files.append(f)
                character_image_url = f"attachment://{char_attach_name}"
        if cosmetic_img_url:
            f = await fetch_embed_image_as_file(cosmetic_img_url, filename=cosmetic_attach_name)
            if f is None and selected_cosmetic_id:
                f = await fetch_embed_image_as_file(
                    default_cosmetic_image_url(selected_cosmetic_id), filename=cosmetic_attach_name
                )
            if f:
                files.append(f)
                cosmetic_img_url = f"attachment://{cosmetic_attach_name}"

        embeds = _build_inspect_embeds(
            stats,
            display_name,
            is_self=is_self,
            character_image_url=character_image_url,
            cosmetic_image_url=cosmetic_img_url,
            max_inventory_slots=max_inventory_slots,
        )

        # Build character dropdown if the target has characters
        view: discord.ui.View | None = None
        if stats.character_ids:
            view = CharacterInspectView(
                target_id=target_id,
                character_ids=stats.character_ids,
                allowed_user_id=int(interaction.user.id),
            )

        kwargs: dict = {"embeds": embeds}
        if files:
            kwargs["files"] = files
        if view:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashInspect(bot))
