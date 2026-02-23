# commands/slash/character.py
from __future__ import annotations

import logging
import asyncio
import random


import discord
from discord import app_commands
from discord.ext import commands

from utils.backpressure import get_redis

from utils.premium import get_premium_tier
from utils.analytics import track_roll
from utils.character_registry import BASE_STYLE_IDS, get_style, list_rollable, roll_style
from utils.pack_badges import badges_for_style_id
from utils.character_emotion_manifest import ASSETS_UI_BASE, ROLL_ANIMATION_UI_BASE
from core.kai_mascot import (
    PITY_LEGENDARY_THRESHOLD,
    PITY_MYTHIC_THRESHOLD,
    embed_kailove,
    embed_kaihappy,
    get_kai_legendary_roll_message,
    get_kai_pity_roll_message,
)
from utils.character_store import (
    can_roll_is_pro,
    clear_roll_window,
    consume_roll,
    apply_pity_after_roll,
    set_pity,
    load_state,
    set_active_style,
    add_style_to_inventory,
    replace_style_in_inventory,
    award_dupe_shards,
    compute_limits,
    delete_custom_style_profile,
    remove_style_from_inventory,
    get_roll_retry_after_seconds,
    roll_window_seconds,
    get_inventory_upgrades,
)

from utils.points_store import get_active_booster, get_booster_stack, spend_points
from utils.packs_store import get_enabled_pack_ids
from utils.media_assets import asset_abspath, get_discord_file_for_asset
from utils.quests import apply_quest_event

logger = logging.getLogger("bot.character")

EXTRA_ROLL_COST = 60
AUTOCOMPLETE_MAX = 25


async def _safe_ephemeral_send(interaction: discord.Interaction, content: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        pass

RARITY_EMOJI = {
    "common": "⚪",
    "uncommon": "🟢",
    "rare": "🔵",
    "legendary": "🟣",
    "mythic": "🟡",
}

# Pending rolls MUST be shared across processes/shards.
# Store in Redis with a short TTL.
PENDING_TTL_S = 120  # seconds


def _k_pending(user_id: int) -> str:
    return f"char:pending:{int(user_id)}"


async def pending_set(user_id: int, *, style_id: str) -> None:
    r = await get_redis()
    await r.set(_k_pending(user_id), (style_id or "").strip().lower(), ex=int(PENDING_TTL_S))


async def pending_get(user_id: int) -> str | None:
    r = await get_redis()
    v = await r.get(_k_pending(user_id))
    if not v:
        return None
    return str(v).strip().lower() or None


async def pending_clear(user_id: int) -> None:
    r = await get_redis()
    await r.delete(_k_pending(user_id))


async def pending_is_valid(user_id: int, *, expected_style_id: str | None = None) -> bool:
    cur = await pending_get(user_id)
    if not cur:
        return False
    if expected_style_id:
        exp = (expected_style_id or "").strip().lower()
        return cur == exp
    return True


def _format_character_name(style_id: str | None) -> str:
    sid = (style_id or "").strip().lower()
    if not sid:
        return "(none)"
    s = get_style(sid)
    return s.display_name if s else sid


def _choice_label(style_id: str) -> str:
    """
    What shows in the dropdown. Value returned is still the style_id.
    Example: "Pirate  [common]"
    """
    sid = (style_id or "").strip().lower()
    s = get_style(sid)
    if not s:
        return sid or style_id
    r = (str(getattr(s, "rarity", "")) or "").lower().strip()
    emoji = RARITY_EMOJI.get(r, "❔")
    return f"{s.display_name}  {emoji} {r or 'unknown'}"


async def ac_character_select(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for /character select:
    - base characters (fun/serious)
    - owned characters (registry + custom)
    """
    cur = (current or "").strip().lower()

    style_ids: list[str] = []
    style_ids.extend(sorted([s.lower() for s in BASE_STYLE_IDS]))

    try:
        st = await load_state(user_id=interaction.user.id)
        owned = list(getattr(st, "owned_custom", []) or [])
        style_ids.extend([s.lower() for s in owned])
    except Exception:
        pass

    # de-dupe preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for sid in style_ids:
        sid = (sid or "").strip().lower()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        deduped.append(sid)

    # filter by what user typed (match id or display name)
    if cur:
        filtered: list[str] = []
        for sid in deduped:
            s = get_style(sid)
            dn = (s.display_name.lower() if s and getattr(s, "display_name", None) else "")
            if cur in sid.lower() or (dn and cur in dn):
                filtered.append(sid)
        deduped = filtered

    return [app_commands.Choice(name=_choice_label(sid), value=sid) for sid in deduped[:AUTOCOMPLETE_MAX]]


async def ac_character_remove(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for /character remove:
    - only owned custom characters (best-effort: uses owned_custom list)
    """
    cur = (current or "").strip().lower()

    try:
        st = await load_state(user_id=interaction.user.id)
        # Only removable characters: everything except base (fun/serious/etc.)
        base = {s.lower() for s in BASE_STYLE_IDS}
        style_ids = [s.lower() for s in (list(getattr(st, "owned_custom", []) or [])) if s and s.lower() not in base]
    except Exception:
        style_ids = []

    if cur:
        filtered: list[str] = []
        for sid in style_ids:
            s = get_style(sid)
            dn = (s.display_name.lower() if s and getattr(s, "display_name", None) else "")
            if cur in sid.lower() or (dn and cur in dn):
                filtered.append(sid)
        style_ids = filtered

    return [app_commands.Choice(name=_choice_label(sid), value=sid) for sid in style_ids[:AUTOCOMPLETE_MAX]]
def _image_attachment_for_style(s) -> tuple[discord.File | None, str | None]:
    """If the style uses a local asset image, return (file, attachment_url)."""
    url = getattr(s, "image_url", None)
    if not url:
        return None, None
    if isinstance(url, str) and url.startswith("asset:"):
        rel = url[len("asset:") :].strip()
        f = get_discord_file_for_asset(rel)
        if not f:
            return None, None
        return f, f"attachment://{f.filename}"
    # Remote URL
    return None, str(url)


async def _send_kai_pity_if_applicable(
    interaction: discord.Interaction,
    rolled,
    pity_legendary_after: int = 0,
    pity_mythic_after: int = 0,
) -> None:
    """If the roll was common and pity is above threshold, send KAI kaihappy followup. Skip uncommon+."""
    try:
        r = str(getattr(rolled, "rarity", "") or "").strip().lower()
        if r in ("legendary", "mythic"):
            return
        if r != "common":
            return
        if pity_legendary_after < PITY_LEGENDARY_THRESHOLD and pity_mythic_after < PITY_MYTHIC_THRESHOLD:
            return
        msg = get_kai_pity_roll_message(pity_legendary_after, pity_mythic_after)
        await interaction.followup.send(embed=embed_kaihappy(msg), ephemeral=True)
    except Exception:
        logger.exception("KAI pity followup failed")


async def _send_kai_celebration_if_applicable(interaction: discord.Interaction, rolled) -> None:
    """If the roll was legendary or mythic, send KAI Kailove celebration followup."""
    try:
        r = str(getattr(rolled, "rarity", "") or "").strip().lower()
        if r not in ("legendary", "mythic"):
            return
        name = getattr(rolled, "display_name", None) or getattr(rolled, "style_id", "someone")
        msg = get_kai_legendary_roll_message(str(name), r)
        title = "KAI loves it!" if r == "legendary" else "KAI is amazed!"
        await interaction.followup.send(embed=embed_kailove(msg, title=title), ephemeral=True)
    except Exception:
        logger.exception("KAI celebration followup failed")


def character_embed(
    s,
    *,
    rolls_left: int | None = None,
    per_day: int | None = None,
    badges: str | None = None,
) -> tuple[discord.Embed, discord.File | None]:
    badge_txt = (badges or "").strip()
    title_suffix = f" — {badge_txt}" if badge_txt else ""
    e = discord.Embed(
        title=f"You rolled: {getattr(s, 'display_name', 'Unknown')}{title_suffix}",
        description=f"**{getattr(s, 'description', '')}**",
        color=getattr(s, "color", 0x5865F2),
    )
    rarity_raw = (getattr(s, "rarity", "") or "").lower().strip()
    emoji = RARITY_EMOJI.get(rarity_raw, "❔")
    rarity_title = rarity_raw.title() if rarity_raw else "Unknown"

    # Put rarity + rolls remaining together (user asked for rolls below rarity).
    rarity_val = f"{emoji} **{rarity_title}**"
    if rolls_left is not None and per_day is not None:
        rarity_val = f"{emoji} **{rarity_title}**\nRolls left: **{max(0, int(rolls_left))} / {int(per_day)}**"
    e.add_field(name="Rarity", value=rarity_val, inline=True)

    f, attach_url = _image_attachment_for_style(s)
    if attach_url:
        # Large, in-your-face image
        e.set_image(url=attach_url)
    return e, f


async def run_roll_reveal_animation(
    interaction: discord.Interaction,
    rolled_style_def,
    *,
    user_id: int,
    guild_id: int,
    reward_embed: discord.Embed,
    reward_file: discord.File | None = None,
    view: discord.ui.View | None = None,
) -> discord.Message | None:
    """Run spin -> open -> Open button -> reward sequence. Assumes interaction.response.is_done() (deferred).
    Returns the message that was edited to the reward, or None if animation was skipped (e.g. asset: image).
    """
    import os
    from utils.ui_sfx import play_ui_sound

    if str(getattr(rolled_style_def, "image_url", "") or "").startswith("asset:"):
        return None

    rarity = str(getattr(rolled_style_def, "rarity", "") or "").strip().lower()
    gif_map = {
        "common": "common1.gif",
        "uncommon": "uncommon1.gif",
        "rare": "rare1.gif",
        "legendary": "legendary1.gif",
        "mythic": "mythic1.gif",
    }
    spin_gif = gif_map.get(rarity, "common1.gif")
    spin_sfx = "roll2.wav" if rarity in ("rare", "legendary", "mythic") else "roll1.wav"
    open_sfx_map = {
        "common": "open1.wav",
        "uncommon": "open2.wav",
        "rare": "open3.wav",
        "legendary": "open4.wav",
        "mythic": "open5.wav",
    }
    reward_sfx_map = {
        "common": "reward1.wav",
        "uncommon": "reward2.wav",
        "rare": "reward3.wav",
        "legendary": "reward4.wav",
        "mythic": "reward5.wav",
    }
    open_sfx = open_sfx_map.get(rarity, "open1.wav")
    reward_sfx = reward_sfx_map.get(rarity, "reward1.wav")
    try:
        spin_delay_s = float((os.getenv("ROLL_SPIN_DELAY_S") or "3").strip())
    except Exception:
        spin_delay_s = 3.0
    try:
        open_delay_s = float((os.getenv("ROLL_OPEN_DELAY_S") or "2").strip())
        open_delay_s += float((os.getenv("ROLL_OPEN_EXTRA_DELAY_S") or "0").strip())
    except Exception:
        open_delay_s = 2.0

    spin_embed = discord.Embed(description="🎲 Spinning...")
    spin_embed.set_image(url=f"{ROLL_ANIMATION_UI_BASE}/{spin_gif}")
    animated_msg = await interaction.followup.send(embed=spin_embed, ephemeral=True, wait=True)
    if not animated_msg:
        return None

    asyncio.create_task(play_ui_sound(interaction, spin_sfx))
    await asyncio.sleep(max(0.5, spin_delay_s))

    opening_embed = discord.Embed(description="📦 Opening...")
    opening_embed.set_image(url=f"{ROLL_ANIMATION_UI_BASE}/box.gif")
    try:
        await animated_msg.edit(embed=opening_embed, view=None)
    except Exception:
        pass
    asyncio.create_task(play_ui_sound(interaction, open_sfx))
    await asyncio.sleep(max(0.5, open_delay_s))

    ready_embed = discord.Embed(description="📦 Ready to open!")
    ready_embed.set_image(url=f"{ROLL_ANIMATION_UI_BASE}/box.gif")
    open_evt: asyncio.Event = asyncio.Event()

    class _OpenView(discord.ui.View):
        def __init__(self, *, uid: int):
            super().__init__(timeout=60)
            self.uid = int(uid)

        @discord.ui.button(label="Open", style=discord.ButtonStyle.success)
        async def open_btn(self, interaction2: discord.Interaction, button: discord.ui.Button):
            if int(interaction2.user.id) != self.uid:
                await interaction2.response.send_message("This isn't your roll.", ephemeral=True)
                return
            try:
                if interaction2.guild is not None:
                    vc = interaction2.guild.voice_client
                    if vc and vc.is_connected():
                        try:
                            vc.stop()
                        except Exception:
                            pass
                        try:
                            await vc.disconnect(force=True)
                        except TypeError:
                            await vc.disconnect()
            except Exception:
                pass
            button.disabled = True
            try:
                await interaction2.response.edit_message(view=self)
            except Exception:
                pass
            open_evt.set()

    open_view = _OpenView(uid=user_id)
    await animated_msg.edit(embed=ready_embed, view=open_view)
    try:
        await asyncio.wait_for(open_evt.wait(), timeout=60)
    except asyncio.TimeoutError:
        for child in open_view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await animated_msg.edit(view=open_view)
        except Exception:
            pass

    try:
        if reward_sfx:
            asyncio.create_task(play_ui_sound(interaction, reward_sfx))
    except Exception:
        pass
    edit_kw: dict = {"embed": reward_embed}
    if view is not None:
        edit_kw["view"] = view
    try:
        await animated_msg.edit(**edit_kw)
    except Exception:
        try:
            await animated_msg.edit(embed=reward_embed, view=view)
        except Exception:
            pass
    return animated_msg


class RollView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        user_id: int,
        guild_id: int,
        rolled_style_id: str,
        inventory_full: bool,
        out_of_rolls: bool = False,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        self.rolled_style_id = (rolled_style_id or "").strip().lower()

        # Only show the paid "Extra Roll" button when the user is out of free rolls.
        if not out_of_rolls:
            self.remove_item(self.extra_roll_btn)

        # If inventory is full, Replace is the main action (hide Add)
        # If not full, Add is the main action (hide Replace)
        if inventory_full:
            self.remove_item(self.add_btn)      # hide Add
        else:
            self.remove_item(self.replace_btn)  # hide Replace

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _safe_ephemeral_send(interaction, "This roll isn’t yours.")
            return False
        if not await pending_is_valid(self.user_id, expected_style_id=self.rolled_style_id):
            await _safe_ephemeral_send(interaction, "That roll expired. Use `/character roll` again.")
            return False
        return True

    async def _is_pro(self) -> bool:
        tier = await get_premium_tier(self.user_id)
        return tier == "pro"

    @discord.ui.button(label="Full Image", style=discord.ButtonStyle.secondary)
    async def full_image_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        """Show the rolled character image as a standalone attachment (Discord renders it larger)."""
        try:
            await interaction.response.defer(ephemeral=True)
            if not await self._guard(interaction):
                return
            from utils.character_registry import get_style

            s = get_style(self.rolled_style_id)
            if not s:
                await interaction.response.send_message("⚠️ Character not found.", ephemeral=True)
                return

            f, attach_url = _image_attachment_for_style(s)
            # If we have a local asset file, sending it without an embed makes Discord display it big.
            if f is not None:
                if interaction.response.is_done():
                    await interaction.followup.send(file=f, ephemeral=True)
                else:
                    await interaction.response.send_message(file=f, ephemeral=True)
                return

            # Remote URL fallback
            url = getattr(s, "image_url", None)
            if isinstance(url, str) and url.strip():
                # Helpful error if this is an asset ref but the file is missing.
                if url.strip().startswith("asset:"):
                    rel = url.strip()[len("asset:") :].strip()
                    abs_path = asset_abspath(rel)
                    msg = (
                        "⚠️ I couldn’t find that asset on disk.\n"
                        f"Expected file at: `{abs_path}`\n\n"
                        "**Fix:** put the GIF/image at that path (or upload it via your pack/character image command), "
                        "then roll again. If you’re on Railway, make sure your `data/` volume is mounted and persists."
                    )
                else:
                    msg = url.strip()
            else:
                msg = "(No image set for this character.)"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            logger.exception("RollView.full_image_btn failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("⚠️ Something went wrong.", ephemeral=True)
                else:
                    await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            if not await self._guard(interaction):
                return

            # Some versions of character_store return None; some return (ok,msg).
            res = await add_style_to_inventory(
                user_id=self.user_id,
                style_id=self.rolled_style_id,
                is_pro=await self._is_pro(),
                guild_id=self.guild_id,
            )

            if isinstance(res, tuple) and len(res) == 2:
                ok, msg = bool(res[0]), str(res[1])
            else:
                ok, msg = True, "Added to your collection."

            if ok:
                await pending_clear(self.user_id)
                await interaction.followup.send(f"✅ {msg}", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"⚠️ {msg}\nUse **Replace** to swap a character.",
                    ephemeral=True,
                )

        except Exception:
            logger.exception("RollView.add_btn failed")
            try:
                await _safe_ephemeral_send(interaction, "⚠️ Something went wrong. Check logs.")
            except Exception:
                pass

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.primary)
    async def replace_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            if not await self._guard(interaction):
                return

            _ = await self._is_pro()  # kept for symmetry (and future caps if needed)

            state = await load_state(user_id=self.user_id)
            owned = list(getattr(state, "owned_custom", []) or [])
            if not owned:
                await interaction.followup.send("You have no characters to replace.", ephemeral=True)
                return

            options = [discord.SelectOption(label=_format_character_name(sid), value=sid) for sid in owned[:25]]

            class ReplaceSelect(discord.ui.Select):
                def __init__(self, owner_id: int, rolled_style_id: str):
                    super().__init__(placeholder="Choose a character to replace…", options=options)
                    self.owner_id = int(owner_id)
                    self.rolled_style_id = (rolled_style_id or "").strip().lower()

                async def callback(self, i: discord.Interaction):
                    try:
                        await i.response.defer(ephemeral=True)
                        if i.user.id != self.owner_id:
                            await i.followup.send("This menu isn’t yours.", ephemeral=True)
                            return

                        old_id = (self.values[0] or "").strip().lower()

                        res2 = await replace_style_in_inventory(
                            user_id=self.owner_id,
                            old_style_id=old_id,
                            new_style_id=self.rolled_style_id,
                        )

                        if isinstance(res2, tuple) and len(res2) == 2:
                            ok2, msg2 = bool(res2[0]), str(res2[1])
                        else:
                            ok2, msg2 = True, "Replaced."

                        if ok2:
                            await pending_clear(self.owner_id)
                            await i.followup.send(f"✅ {msg2}", ephemeral=True)
                        else:
                            await i.followup.send(f"⚠️ {msg2}", ephemeral=True)

                    except Exception:
                        logger.exception("ReplaceSelect.callback failed")
                        try:
                            await _safe_ephemeral_send(i, "⚠️ Something went wrong. Check logs.")
                        except Exception:
                            pass

            v = discord.ui.View(timeout=60)
            v.add_item(ReplaceSelect(self.user_id, self.rolled_style_id))
            await interaction.followup.send("Pick a character to replace:", view=v, ephemeral=True)

        except Exception:
            logger.exception("RollView.replace_btn failed")
            try:
                await _safe_ephemeral_send(interaction, "⚠️ Something went wrong. Check logs.")
            except Exception:
                pass

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            if not await self._guard(interaction):
                return
            await pending_clear(self.user_id)
            await interaction.followup.send("❌ Discarded.", ephemeral=True)
        except Exception:
            logger.exception("RollView.discard_btn failed")
            try:
                await _safe_ephemeral_send(interaction, "⚠️ Something went wrong. Check logs.")
            except Exception:
                pass

    @discord.ui.button(label="Roll again", style=discord.ButtonStyle.danger)
    async def roll_again_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not await self._guard(interaction):
                return

            await interaction.response.defer(ephemeral=True)
            cog = self.bot.get_cog("SlashCharacter")  # type: ignore
            if not cog:
                await interaction.followup.send("⚠️ Roll system is unavailable.", ephemeral=True)
                return
            await cog._do_roll(interaction, from_button=True)

        except Exception:
            logger.exception("RollView.roll_again_btn failed")
            try:
                await interaction.followup.send("⚠️ Something went wrong. Check logs.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label=f"Extra Roll ({EXTRA_ROLL_COST} Points)", style=discord.ButtonStyle.primary, emoji="🎲")
    async def extra_roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not await self._guard(interaction):
                return

            await interaction.response.defer(ephemeral=True)

            ok, _new_bal = await spend_points(guild_id=self.guild_id, user_id=self.user_id, cost=EXTRA_ROLL_COST, reason="extra_roll")
            if not ok:
                await interaction.followup.send(f"❌ You need **{EXTRA_ROLL_COST} points** for an extra roll.", ephemeral=True)
                return

            cog = self.bot.get_cog("SlashCharacter")  # type: ignore
            if not cog:
                await interaction.followup.send("⚠️ Roll system is unavailable.", ephemeral=True)
                return

            # Paid roll: do not consume a daily roll credit.
            await cog._do_roll(interaction, from_button=True, consume_roll_credit=False)

        except Exception:
            logger.exception("RollView.extra_roll_btn failed")
            try:
                await interaction.followup.send("⚠️ Something went wrong. Check logs.", ephemeral=True)
            except Exception:
                pass


class OutOfRollsView(discord.ui.View):
    """Shown when user has no rolls left; single button to buy one roll."""

    def __init__(self, *, bot: commands.Bot, guild_id: int, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)

    @discord.ui.button(label=f"Buy extra roll ({EXTRA_ROLL_COST} points)", style=discord.ButtonStyle.primary, emoji="🎲")
    async def buy_extra_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This isn't your panel.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True)
            ok, _new_bal = await spend_points(
                guild_id=self.guild_id, user_id=self.user_id, cost=EXTRA_ROLL_COST, reason="extra_roll"
            )
            if not ok:
                await interaction.followup.send(f"❌ You need **{EXTRA_ROLL_COST} points** for an extra roll.", ephemeral=True)
                return
            cog = self.bot.get_cog("SlashCharacter")  # type: ignore
            if not cog:
                await interaction.followup.send("⚠️ Roll system is unavailable.", ephemeral=True)
                return
            await cog._do_roll(interaction, from_button=True, consume_roll_credit=False)
        except Exception:
            logger.exception("OutOfRollsView.buy_extra_btn failed")
            try:
                await interaction.followup.send("⚠️ Something went wrong. Check logs.", ephemeral=True)
            except Exception:
                pass


class DupeRollView(discord.ui.View):
    """
    View for a DUPLICATE roll: Roll again, Close, and optionally Extra Roll (100 pts) when out of rolls.
    """

    def __init__(self, *, bot: commands.Bot, user_id: int, guild_id: int, out_of_rolls: bool = False):
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)

        # Only show the paid extra-roll button when the user has no free rolls left.
        if not out_of_rolls:
            for item in list(self.children):
                if getattr(item, "custom_id", None) == "dupe_extra_roll":
                    self.remove_item(item)
                    break

    def _guard_user(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Roll again", style=discord.ButtonStyle.danger)
    async def roll_again_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not self._guard_user(interaction):
                await interaction.response.send_message("This isn’t your roll.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            cog = self.bot.get_cog("SlashCharacter")  # type: ignore
            if not cog:
                await interaction.followup.send("⚠️ Roll system is unavailable.", ephemeral=True)
                return
            await cog._do_roll(interaction, from_button=True)

        except Exception:
            logger.exception("DupeRollView.roll_again_btn failed")
            try:
                await interaction.followup.send("⚠️ Something went wrong. Check logs.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label=f"Extra Roll ({EXTRA_ROLL_COST} Points)", style=discord.ButtonStyle.primary, emoji="🎲", custom_id="dupe_extra_roll")
    async def extra_roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not self._guard_user(interaction):
                await interaction.response.send_message("This isn’t your roll.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            ok, _new_bal = await spend_points(
                guild_id=self.guild_id, user_id=self.user_id, cost=EXTRA_ROLL_COST, reason="extra_roll"
            )
            if not ok:
                await interaction.followup.send(f"❌ You need **{EXTRA_ROLL_COST} points** for an extra roll.", ephemeral=True)
                return
            cog = self.bot.get_cog("SlashCharacter")  # type: ignore
            if not cog:
                await interaction.followup.send("⚠️ Roll system is unavailable.", ephemeral=True)
                return
            await cog._do_roll(interaction, from_button=True, consume_roll_credit=False)
        except Exception:
            logger.exception("DupeRollView.extra_roll_btn failed")
            try:
                await interaction.followup.send("⚠️ Something went wrong. Check logs.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not self._guard_user(interaction):
                await interaction.response.send_message("This isn’t your roll.", ephemeral=True)
                return
            await interaction.response.edit_message(view=None)

        except Exception:
            logger.exception("DupeRollView.close_btn failed")
            try:
                await interaction.response.send_message("⚠️ Something went wrong. Check logs.", ephemeral=True)
            except Exception:
                pass


class SlashCharacter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    character_group = app_commands.Group(name="character", description="Collect, manage, and roll characters")

    # ---------------------------
    # /character collection
    # ---------------------------
    @character_group.command(name="collection", description="View your owned characters and your selected character")
    async def character_collection(self, interaction: discord.Interaction):
        try:
            user_id = interaction.user.id
            state = await load_state(user_id=user_id)

            base = sorted([s.lower() for s in BASE_STYLE_IDS])
            owned_all = sorted([s.lower() for s in (list(getattr(state, "owned_custom", []) or []))])
            base_set = set(base)
            owned = [s for s in owned_all if s and s not in base_set]

            # Inventory cap depends on this guild's tier (free/pro)
            guild_id = int(interaction.guild.id) if interaction.guild else 0
            is_pro = (await get_premium_tier(user_id)) == "pro" if guild_id else False
            _rolls_cfg, slots = compute_limits(is_pro=is_pro)
            inv_count = len(set(owned))

            active = getattr(state, "active_style_id", None)
            if active:
                b = await badges_for_style_id(str(active))
                active_label = f"{_format_character_name(active)} — {b}"
            else:
                active_label = "(none — uses server default unless you pick one in /talk)"

            e = discord.Embed(title=f"{interaction.user.display_name}'s Characters", color=0x5865F2)
            s_obj = None
            if active:
                s_obj = get_style((active or "").lower())
                if s_obj and getattr(s_obj, "image_url", None):
                    f_img, url = _image_attachment_for_style(s_obj)
                    if url:
                        e.set_image(url=url)

            e.add_field(name="Selected character", value=active_label, inline=False)
            e.add_field(name="Base characters", value=", ".join([_format_character_name(s) for s in base]), inline=False)
            e.add_field(
                name="Owned characters",
                value=", ".join([_format_character_name(s) for s in owned]) if owned else "(none yet)",
                inline=False,
            )

            e.add_field(name="Inventory", value=f"{inv_count} / {int(slots)}", inline=True)

            # Some older code uses "shards"; your current character_store uses "points"
            shards_like = int(getattr(state, "shards", 0) or 0)
            points_like = int(getattr(state, "points", 0) or 0)
            currency = shards_like if shards_like else points_like
            e.add_field(name="Shards", value=str(currency), inline=True)

            kwargs = {"embed": e, "ephemeral": True}
            try:
                if active and s_obj and getattr(s_obj, "image_url", None):
                    f_img, _url2 = _image_attachment_for_style(s_obj)
                    if f_img is not None:
                        kwargs["file"] = f_img
            except Exception:
                pass
            await interaction.response.send_message(**kwargs)

        except Exception:
            logger.exception("character_collection failed")
            try:
                await interaction.response.send_message("⚠️ Failed to load your collection. Check logs.", ephemeral=True)
            except Exception:
                pass

    # ---------------------------
    # /character select
    # ---------------------------
    @character_group.command(name="select", description="Select your active character")
    @app_commands.describe(character="A character you own (or fun/serious).")
    @app_commands.autocomplete(character=ac_character_select)
    async def character_select(self, interaction: discord.Interaction, character: str):
        try:
            style_id = (character or "").strip().lower()
            ok, msg = await set_active_style(user_id=interaction.user.id, style_id=style_id)

            if ok:
                await interaction.response.send_message(
                    f"✅ Selected character: **{_format_character_name(style_id)}**.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("⚠️ " + msg, ephemeral=True)

        except Exception:
            logger.exception("character_select failed")
            try:
                await interaction.response.send_message("⚠️ Failed to select character. Check logs.", ephemeral=True)
            except Exception:
                pass

    # ---------------------------
    # /character unselect
    # ---------------------------
    @character_group.command(
        name="unselect",
        description="Clear your selected character (back to server default unless chosen in /talk)",
    )
    async def character_unselect(self, interaction: discord.Interaction):
        try:
            # Your character_store currently returns False if style_id is None.
            # So we pass "" which your store treats as invalid, but many versions treat as clear.
            # We'll handle both: if it fails, show a friendly message.
            ok, msg = await set_active_style(user_id=interaction.user.id, style_id="")
            if ok:
                await interaction.response.send_message("✅ Cleared your selected character.", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ Unable to clear selection in this build. (We’ll fix in character_store next.)", ephemeral=True)

        except Exception:
            logger.exception("character_unselect failed")
            try:
                await interaction.response.send_message("⚠️ Failed to unselect character. Check logs.", ephemeral=True)
            except Exception:
                pass

    # ---------------------------
    # /character remove
    # ---------------------------
    @character_group.command(name="remove", description="Remove a custom character from your collection")
    @app_commands.describe(character="A custom character you own.")
    @app_commands.autocomplete(character=ac_character_remove)
    async def character_remove(self, interaction: discord.Interaction, character: str):
        try:
            sid = (character or "").strip().lower()
            if not sid:
                await interaction.response.send_message("Pick a character to remove.", ephemeral=True)
                return

            ok, msg, old_streak = await remove_style_from_inventory(user_id=interaction.user.id, style_id=sid)
            if not ok:
                await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)
                return

            # If they removed their active style, it will have been cleared in the store.
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)

            # DM the user if they had an active streak with this character
            if old_streak > 0:
                try:
                    from utils.character_registry import get_style
                    s = get_style(sid)
                    char_name = (getattr(s, "display_name", None) if s else None) or sid.replace("_", " ").title()
                    embed = discord.Embed(
                        title="Character streak ended",
                        description=(
                            f"Your streak with **{char_name}** ({old_streak} day{'s' if old_streak != 1 else ''}) "
                            f"has ended because you removed them from your inventory."
                        ),
                        color=0xED4245,
                    )
                    await interaction.user.send(embed=embed)
                except discord.Forbidden:
                    pass  # DMs disabled
                except Exception:
                    logger.debug("Could not DM user about streak end on remove", exc_info=True)

        except Exception:
            logger.exception("character_remove failed")
            try:
                await interaction.response.send_message("⚠️ Failed to remove character. Check logs.", ephemeral=True)
            except Exception:
                pass

    # ---------------------------
    # /character roll
    # ---------------------------
    @character_group.command(name="roll", description="Roll for a random character")
    async def character_roll(self, interaction: discord.Interaction):
        await self._do_roll(interaction, from_button=False)

    # ---------------------------
    # /character reset (testing)
    # ---------------------------
    @character_group.command(name="reset", description="(Testing) Reset roll cooldown and roll counters")
    async def character_reset(self, interaction: discord.Interaction):
        deferred = False
        try:
            await interaction.response.defer(ephemeral=True)
            deferred = True
        except Exception:
            pass
        try:
            from utils.character_store import _save_state  # local import: only used here

            user_id = interaction.user.id
            # Reset Postgres: daily roll count and pity
            st = await load_state(user_id=user_id)
            st.roll_day = ""
            st.roll_used = 0
            st.pity_legendary = 0
            st.pity_mythic = 0
            await _save_state(st)
            # Reset Redis: sliding-window roll cooldown (so you get a fresh window immediately)
            await clear_roll_window(user_id=user_id)

            if deferred:
                await interaction.followup.send("✅ Roll cooldown and roll counters reset. You can roll again.", ephemeral=True)
            else:
                await interaction.response.send_message("✅ Roll cooldown and roll counters reset. You can roll again.", ephemeral=True)
        except Exception:
            logger.exception("character_reset failed")
            msg = "⚠️ Reset failed. Check logs."
            try:
                if deferred:
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

    # ---------------------------
    # Shared roll logic (used by buttons)
    # ---------------------------
    async def _do_roll(self, interaction: discord.Interaction, from_button: bool, *, consume_roll_credit: bool = True):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            if not interaction.guild:
                msg = "This command can only be used in a server."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                return

            guild_id = int(interaction.guild.id)
            user_id = int(interaction.user.id)

            is_pro = (await get_premium_tier(user_id)) == "pro"

            allowed, remaining, per_day = await can_roll_is_pro(user_id=user_id, is_pro=is_pro)
            if consume_roll_credit and not allowed:
                window_s = roll_window_seconds()
                if window_s > 0:
                    retry_s = await get_roll_retry_after_seconds(
                        user_id=user_id, tier="pro" if is_pro else "free"
                    )
                    hours = window_s // 3600
                    msg = f"🛑 You're out of rolls for this window ({per_day}/{hours}h)."
                    if retry_s > 0:
                        h, m = retry_s // 3600, (retry_s % 3600) // 60
                        msg += f" Try again in **{h}h {m}m**."
                    msg += f" Buy an extra roll below for **{EXTRA_ROLL_COST} points**."
                else:
                    msg = f"🛑 You're out of rolls for today ({per_day}/day). Buy an extra roll below for **{EXTRA_ROLL_COST} points**."
                view = OutOfRollsView(bot=self.bot, guild_id=guild_id, user_id=user_id)
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True, view=view)
                else:
                    await interaction.response.send_message(msg, ephemeral=True, view=view)
                return

            state = await load_state(user_id=user_id)

            owned_list = list(getattr(state, "owned_custom", []) or [])
            owned = set(s.lower() for s in owned_list)
            base_set = {s.lower() for s in BASE_STYLE_IDS}
            owned_nonbase = [s.lower() for s in owned_list if s and s.lower() not in base_set]

            pity_legendary = int(getattr(state, "pity_legendary", 0) or 0)
            pity_mythic = int(getattr(state, "pity_mythic", 0) or 0)

            # Pack filtering: never allow a disabled-pack character to leak into rolls.
            # If pack settings are misconfigured (e.g., enabled packs have zero characters),
            # fail with a clear message instead of falling back to a random built-in.
            enabled_packs: set[str] = await get_enabled_pack_ids(guild_id)

            # Lucky Boosters only apply when all enabled packs are official.
            # Community (user-created) packs are delinked from monetization advantages.
            from utils.packs_store import is_pack_official
            _all_official = True
            for _ep in enabled_packs:
                if not await is_pack_official(_ep):
                    _all_official = False
                    break

            legendary_mult = 1.0
            mythic_mult = 1.0
            try:
                stacks, remaining_s = await get_booster_stack(guild_id=guild_id, user_id=user_id, kind="lucky")
                if stacks > 0 and remaining_s > 0 and _all_official:
                    mult = float(1.5 ** int(stacks))
                    legendary_mult = mult
                    mythic_mult = mult
            except Exception:
                pass

            rng = random.Random()
            # Merge server-only characters into the runtime registry (pseudo pack: server_<guild_id>).
            try:
                from utils.server_chars_store import list_server_chars, to_pack_payload
                from utils.character_registry import merge_pack_payload

                server_chars = await list_server_chars(guild_id)
                if server_chars:
                    merge_pack_payload(to_pack_payload(guild_id, server_chars))
            except Exception:
                pass
            if not list_rollable(pack_ids=enabled_packs):
                await _safe_ephemeral_send(
                    interaction,
                    "⚠️ No rollable characters are available in this server’s enabled packs.\n"
                    "Ask the server owner/admin to enable a pack via **/packs browse** or **/packs enable**.",
                )
                return
            rolled = roll_style(
                pity_legendary=pity_legendary,
                pity_mythic=pity_mythic,
                rng=rng,
                legendary_mult=legendary_mult,
                mythic_mult=mythic_mult,
                pack_ids=enabled_packs,
            )

            # consume a roll (uses onboarding/bonus rolls first)
            if consume_roll_credit:
                await consume_roll(user_id=user_id)

            # product analytics
            await track_roll(guild_id=guild_id, user_id=user_id)
            await apply_pity_after_roll(
                guild_id=guild_id,
                user_id=user_id,
                rolled_rarity=rolled.rarity,
            )
            # --- Durable pity counters (used by roll_style -> choose_rarity)
            # Increment on misses; reset only the corresponding pity on hit.
            r_pity = str(getattr(rolled, "rarity", "") or "").strip().lower()
            new_pity_leg = pity_legendary
            new_pity_myth = pity_mythic
            if r_pity == "mythic":
                new_pity_leg = 0
                new_pity_myth = 0
            elif r_pity == "legendary":
                new_pity_leg = 0
                new_pity_myth = min(999, pity_mythic + 1)
            else:
                new_pity_leg = min(99, pity_legendary + 1)
                new_pity_myth = min(999, pity_mythic + 1)
            try:
                await set_pity(user_id=user_id, pity_mythic=new_pity_myth, pity_legendary=new_pity_leg)
            except Exception:
                # Pity is nice-to-have; never break rolls.
                pass


            # Rolling animation + VC SFX (spin -> open -> reward).
            # Works for both slash and button interactions.
            animated = False
            animated_msg: discord.Message | None = None
            reward_sfx = ""
            try:
                # If the rolled character image is a local 'asset:' attachment, we can't safely
                # do multi-step edits without complicating attachments. Fall back to normal flow.
                if not str(getattr(rolled, "image_url", "")).startswith("asset:"):
                    import os
                    from utils.ui_sfx import play_ui_sound

                    rarity = str(getattr(rolled, "rarity", "") or "").strip().lower()

                    gif_map = {
                        "common": "common1.gif",
                        "uncommon": "uncommon1.gif",
                        "rare": "rare1.gif",
                        "legendary": "legendary1.gif",
                        "mythic": "mythic1.gif",
                    }
                    spin_gif = gif_map.get(rarity, "common1.gif")

                    # Sound mapping (user spec)
                    spin_sfx = "roll2.wav" if rarity in ("rare", "legendary", "mythic") else "roll1.wav"
                    open_sfx_map = {
                        "common": "open1.wav",
                        "uncommon": "open2.wav",
                        "rare": "open3.wav",
                        "legendary": "open4.wav",
                        "mythic": "open5.wav",
                    }
                    reward_sfx_map = {
                        "common": "reward1.wav",
                        "uncommon": "reward2.wav",
                        "rare": "reward3.wav",
                        "legendary": "reward4.wav",
                        "mythic": "reward5.wav",
                    }
                    open_sfx = open_sfx_map.get(rarity, "open1.wav")
                    reward_sfx = reward_sfx_map.get(rarity, "reward1.wav")

                    # Timing knobs (seconds)
                    try:
                        spin_delay_s = float((os.getenv("ROLL_SPIN_DELAY_S") or "3").strip())
                    except Exception:
                        spin_delay_s = 3.0
                    try:
                        open_delay_s = float((os.getenv("ROLL_OPEN_DELAY_S") or "2").strip())
                        open_delay_s += float((os.getenv("ROLL_OPEN_EXTRA_DELAY_S") or "0").strip())
                    except Exception:
                        open_delay_s = 2.0

                    spin_embed = discord.Embed(description="🎲 Spinning...")
                    spin_embed.set_image(url=f"{ROLL_ANIMATION_UI_BASE}/{spin_gif}")

                    if interaction.response.is_done():
                        animated_msg = await interaction.followup.send(embed=spin_embed, ephemeral=True, wait=True)
                    else:
                        await interaction.response.send_message(embed=spin_embed, ephemeral=True)
                        animated_msg = await interaction.original_response()

                    animated = animated_msg is not None

                    if animated:
                        # Play spin sound while the gif is shown.
                        asyncio.create_task(play_ui_sound(interaction, spin_sfx))
                        await asyncio.sleep(max(0.5, spin_delay_s))

                        # Opening animation phase (no Open button yet)
                        opening_embed = discord.Embed(description="📦 Opening...")
                        opening_embed.set_image(url=f"{ROLL_ANIMATION_UI_BASE}/box.gif")
                        try:
                            await animated_msg.edit(embed=opening_embed, view=None)
                        except Exception:
                            pass

                        # Play opening SFX while the box.gif is shown.
                        asyncio.create_task(play_ui_sound(interaction, open_sfx))
                        await asyncio.sleep(max(0.5, open_delay_s))

                        # After the opening cooldown, show an Open button (do NOT reveal yet)
                        ready_embed = discord.Embed(description="📦 Ready to open!")
                        ready_embed.set_image(url=f"{ROLL_ANIMATION_UI_BASE}/box.gif")

                        open_evt: asyncio.Event = asyncio.Event()

                        class _OpenView(discord.ui.View):
                            def __init__(self, *, user_id: int):
                                super().__init__(timeout=60)
                                self.user_id = int(user_id)

                            @discord.ui.button(label="Open", style=discord.ButtonStyle.success)
                            async def open_btn(self, interaction2: discord.Interaction, button: discord.ui.Button):
                                if int(interaction2.user.id) != self.user_id:
                                    await interaction2.response.send_message("This isn’t your roll.", ephemeral=True)
                                    return

                                # If the opening SFX is still playing, it can block the reward SFX.
                                # Force the bot to stop and leave VC now; it will re-join for the reward sound.
                                try:
                                    if interaction2.guild is not None:
                                        vc = interaction2.guild.voice_client
                                        if vc and vc.is_connected():
                                            try:
                                                vc.stop()
                                            except Exception:
                                                pass
                                            try:
                                                await vc.disconnect(force=True)
                                            except TypeError:
                                                await vc.disconnect()
                                except Exception:
                                    pass

                                button.disabled = True
                                try:
                                    await interaction2.response.edit_message(view=self)
                                except Exception:
                                    pass
                                open_evt.set()

                        open_view = _OpenView(user_id=user_id)
                        await animated_msg.edit(embed=ready_embed, view=open_view)

                        # Wait for user to press Open (or timeout)
                        try:
                            await asyncio.wait_for(open_evt.wait(), timeout=60)
                        except asyncio.TimeoutError:
                            for child in open_view.children:
                                if isinstance(child, discord.ui.Button):
                                    child.disabled = True
                            try:
                                await animated_msg.edit(view=open_view)
                            except Exception:
                                pass


            except Exception:
                animated = False
                animated_msg = None
                reward_sfx = ""
            # Duplicate handling: ONLY Roll again + Close
            if rolled.style_id.lower() in owned:
                await award_dupe_shards(user_id=user_id, amount=1)

                badge = await badges_for_style_id(rolled.style_id)
                rolls_left_after = max(0, remaining - 1) if consume_roll_credit else max(0, remaining)
                e, f = character_embed(rolled, rolls_left=rolls_left_after, per_day=per_day, badges=badge)
                e.add_field(name="Duplicate!", value="You already own this character → **+1 shard**", inline=False)

                view = DupeRollView(bot=self.bot, user_id=user_id, guild_id=guild_id, out_of_rolls=(rolls_left_after <= 0))
                kwargs = {"embed": e, "view": view, "ephemeral": True}
                if f is not None:
                    kwargs["file"] = f

                try:
                    from utils.ui_sfx import play_ui_sound
                    if reward_sfx:
                        asyncio.create_task(play_ui_sound(interaction, reward_sfx))
                except Exception:
                    pass

                if animated and animated_msg is not None:
                    await animated_msg.edit(**{k: v for k, v in kwargs.items() if k != "ephemeral"})
                elif interaction.response.is_done():
                    await interaction.followup.send(**kwargs)
                else:
                    await interaction.response.send_message(**kwargs)

                # ---- Quests (Phase 2 points) ----
                try:
                    comps = await apply_quest_event(
                        guild_id=guild_id,
                        user_id=user_id,
                        event="roll",
                        meta={"rarity": str(rolled.rarity or "").lower()},
                    )
                    if comps:
                        lines = [f"🎁 Quest ready to claim: **{c.name}** (+{c.points} points)" for c in comps]
                        lines.append("Use `/points quests` to claim rewards.")
                        await interaction.followup.send("\n".join(lines), ephemeral=True)
                except Exception:
                    pass
                # KAI mascot: kaihappy when pity above threshold (rolled common only)
                await _send_kai_pity_if_applicable(interaction, rolled, new_pity_leg, new_pity_myth)
                # KAI mascot: Kailove celebration on legendary/mythic
                await _send_kai_celebration_if_applicable(interaction, rolled)
                return

            # Non-duplicate roll: full buttons
            # Store pending roll in Redis so buttons work across shards/instances.
            await pending_set(user_id, style_id=rolled.style_id)

            # Determine if inventory is full for UI (FIXED: uses owned_list)
            _per_day_caps, slots = compute_limits(is_pro=is_pro)
            try:
                _upgrades = int(await get_inventory_upgrades(user_id) or 0)
            except Exception:
                _upgrades = 0
            slots = int(slots) + (_upgrades * 5)
            inventory_full = len(owned_nonbase) >= int(slots)

            badge = await badges_for_style_id(rolled.style_id)
            rolls_left_after = max(0, remaining - 1) if consume_roll_credit else max(0, remaining)
            e, f = character_embed(rolled, rolls_left=rolls_left_after, per_day=per_day, badges=badge)

            # Re-check for safety (cheap)
            _state_now = await load_state(user_id=user_id)
            _owned_now = list(getattr(_state_now, "owned_custom", []) or [])
            _owned_now_nonbase = [s.lower() for s in _owned_now if s and s.lower() not in base_set]
            _per_day2, slots2 = compute_limits(is_pro=is_pro)
            slots2 = int(slots2) + (_upgrades * 5)
            inventory_full2 = len(_owned_now_nonbase) >= int(slots2)

            view = RollView(
                bot=self.bot,
                user_id=user_id,
                guild_id=guild_id,
                rolled_style_id=rolled.style_id,
                inventory_full=inventory_full2,
                out_of_rolls=(rolls_left_after <= 0),
            )

            kwargs = {"embed": e, "view": view, "ephemeral": True}
            if f is not None:
                kwargs["file"] = f

            if animated and animated_msg is not None:
                try:
                    from utils.ui_sfx import play_ui_sound
                    if reward_sfx:
                        asyncio.create_task(play_ui_sound(interaction, reward_sfx))
                except Exception:
                    pass
                await animated_msg.edit(**{k: v for k, v in kwargs.items() if k != "ephemeral"})
            elif interaction.response.is_done():
                await interaction.followup.send(**kwargs)
            else:
                await interaction.response.send_message(**kwargs)

            # ---- Quests (Phase 2 points) ----
            try:
                comps = await apply_quest_event(
                    guild_id=guild_id,
                    user_id=user_id,
                    event="roll",
                    meta={"rarity": str(rolled.rarity or "").lower()},
                )
                if comps:
                    lines = [f"🎁 Quest ready to claim: **{c.name}** (+{c.points} points)" for c in comps]
                    lines.append("Use `/points quests` to claim rewards.")
                    await interaction.followup.send("\n".join(lines), ephemeral=True)
            except Exception:
                pass
            # KAI mascot: kaihappy when pity above threshold (rolled common only)
            await _send_kai_pity_if_applicable(interaction, rolled, new_pity_leg, new_pity_myth)
            # KAI mascot: Kailove celebration on legendary/mythic
            await _send_kai_celebration_if_applicable(interaction, rolled)

        except Exception:
            logger.exception("Character roll failed")
            try:
                msg = "⚠️ Character roll failed due to an internal error. Check logs for details."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashCharacter") is None:
        await bot.add_cog(SlashCharacter(bot))