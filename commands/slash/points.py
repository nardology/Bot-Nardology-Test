# commands/slash/points.py
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.character_emotion_manifest import ASSETS_UI_BASE, ROLL_ANIMATION_UI_BASE
from utils.multi_roll import do_multi_roll
from utils.points_store import (
    claim_daily,
    get_balance,
    get_claim_status,
    get_booster_stack,
    spend_points,
    set_booster,
    build_roadmap_preview,
    restore_daily_streak,
    adjust_points,
)
from utils.pack_creator_rewards import get_pack_creator_daily_bonus

from utils.character_store import (
    grant_bonus_rolls,
    add_points as add_shards,
    get_inventory_upgrades,
    increment_inventory_upgrades,
)
from utils.character_store import (
    add_style_to_inventory,
    load_state,
    compute_limits,
    replace_style_in_inventory,
    owns_style,
    get_pity,
    set_pity,
    apply_pity_after_roll,
)
from utils.character_registry import roll_style, list_rollable, BASE_STYLE_IDS, get_style
from utils.premium import get_premium_tier
from utils.shop_store import list_shop_items, get_shop_item
from utils.pack_badges import badges_for_pack_id, badges_for_style_id
from utils.packs_store import get_custom_pack, list_custom_packs, normalize_pack_id, normalize_style_id
from utils.character_registry import merge_pack_payload
from utils.media_assets import get_discord_file_for_asset, asset_abspath
from commands.slash.character import run_roll_reveal_animation, character_embed
from utils.cosmetics import (
    COSMETIC_CATALOG,
    COSMETIC_PRICE,
    NUM_TO_COSMETIC_ID,
    cosmetic_image_url,
    cosmetic_display_name,
    default_cosmetic_image_url,
)
from utils.media_assets import resolve_embed_image_url, fetch_embed_image_as_file
from utils.cosmetics_store import get_owned, add_owned
from utils.quests import (
    apply_quest_event,
    build_quest_status_embed,
    get_claimable_quest_ids,
    quest_number,
    claim_quest_reward,
    claim_all_rewards,
)
from core.kai_mascot import (
    embed_kailove,
    embed_kaihappy,
    get_kai_claim_all_quests_message,
    get_kai_daily_claim_message,
    get_kai_daily_claim_message_varied,
    get_kai_quests_open_greeting,
    _url,
    KAI_START_IMAGE,
)
from utils.streak_reminders import (
    get_streak_reminders_enabled,
    set_streak_reminders_enabled,
    send_after_claim_dm,
)

logger = logging.getLogger("bot.points")


def _fmt_duration(seconds: int) -> str:
    s = max(0, int(seconds or 0))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _safe_guild_id(interaction: discord.Interaction) -> int | None:
    if not interaction.guild:
        return None
    return int(interaction.guild.id)


SHOP_ITEMS = {
    "extra_roll": {
        "name": "Extra Roll Credit",
        "cost": 60,
        "desc": "Adds +1 bonus roll (stored in Redis). Great for testing the economy.",
        "emoji": "🎲",
    },
    "lucky_booster": {
        "name": "Lucky Booster (1 hour)",
        "cost": 120,
        "desc": "Temporarily boosts Legendary/Mythic odds (x1.5) for 1 hour.",
        "emoji": "🍀",
    },
    "inv_upgrade": {
        "name": "📦 +5 Character Inventory 📦",
        "cost": 500,
        "desc": "Permanently increases your character inventory capacity by +5. Each purchase increases the price by 25%.",
        "emoji": "📦",
    },
    "pull_5": {
        "name": "Buy 5 Characters",
        "cost": 500,
        "desc": "Roll 5 characters at once. Pick which to add, then Apply or Deny.",
        "emoji": "5️⃣",
    },
    "pull_10": {
        "name": "Buy 10 Characters",
        "cost": 1000,
        "desc": "Roll 10 characters at once. Pick which to add, then Apply or Deny.",
        "emoji": "🔟",
    },
}


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


def _image_url_for_style(s) -> str | None:
    """Return the roll/display image URL for a style (embed-friendly; resolves asset: when ASSET_PUBLIC_BASE_URL set)."""
    url = getattr(s, "image_url", None)
    return resolve_embed_image_url(url)


def _build_pull_result_embeds(rolled: list) -> list[discord.Embed]:
    """Build one embed per rolled character for 5/10-pull, each with that character's image (same as roll)."""
    embeds: list[discord.Embed] = []
    for s in rolled:
        r = str(getattr(s, "rarity", "") or "").lower().strip()
        emoji = RARITY_EMOJI.get(r, "❔")
        name = getattr(s, "display_name", None) or getattr(s, "style_id", "?")
        e = discord.Embed(
            title=f"{emoji} {name}",
            description=f"**{r.title()}**",
            color=0xF1C40F,
        )
        img_url = _image_url_for_style(s)
        if img_url:
            e.set_image(url=img_url)
        embeds.append(e)
    if not embeds:
        embeds.append(discord.Embed(title="📦 Your pull results", description="No characters rolled.", color=0xF1C40F))
    else:
        embeds[0].set_footer(text="Select characters to add, then Apply or Deny.")
    return embeds[:10]  # Discord limit 10 embeds per message


class PullResultView(discord.ui.View):
    """Selection UI for 5/10-pull: choose which characters to add, Apply or Deny."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        rolled: list,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.rolled = list(rolled)
        self._selected: set[str] = set()

        options = []
        for s in self.rolled:
            sid = (getattr(s, "style_id", "") or "").strip().lower()
            if not sid:
                continue
            r = str(getattr(s, "rarity", "") or "").lower().strip()
            emoji = RARITY_EMOJI.get(r, "❔")
            name = getattr(s, "display_name", None) or sid
            options.append(discord.SelectOption(label=f"{emoji} {name}", value=sid))
        if options:
            sel = discord.ui.Select(
                placeholder="Select characters to add to your collection",
                min_values=0,
                max_values=min(len(options), 25),
                options=options[:25],
            )
            sel.callback = self._on_select
            self.add_item(sel)

        apply_btn = discord.ui.Button(label="Apply", style=discord.ButtonStyle.success, emoji="✅")
        apply_btn.callback = self._on_apply
        self.add_item(apply_btn)

        deny_btn = discord.ui.Button(label="Deny", style=discord.ButtonStyle.secondary, emoji="❌")
        deny_btn.callback = self._on_deny
        self.add_item(deny_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            await _safe_ephemeral_send(interaction, "This pull isn't yours.")
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        try:
            self._selected = set((v or "").strip().lower() for v in (interaction.data.get("values") or []))
            # Component (select): defer with default = message update (no defer_update in discord.py 2.x)
            await interaction.response.defer()
        except Exception as e:
            logger.warning("PullResultView._on_select defer failed: %s", e)
            try:
                await interaction.response.send_message("Selection updated. Click **Apply** when ready.", ephemeral=True)
            except Exception:
                _safe_ephemeral_send(interaction, "Selection updated. Click **Apply** when ready.")

    async def _on_apply(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        selected = self._selected
        if not selected:
            try:
                await interaction.response.defer()
                await interaction.followup.send("No characters selected. Select some and click **Apply**, or click **Deny** to discard.", ephemeral=True)
            except Exception:
                await _safe_ephemeral_send(interaction, "No characters selected. Select some and click **Apply**, or click **Deny** to discard.")
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        uid = self.user_id
        gid = self.guild_id
        try:
            tier = await get_premium_tier(self.user_id)
            is_pro = tier == "pro"
            st = await load_state(user_id=uid)
            owned = set((s or "").strip().lower() for s in (getattr(st, "owned_custom", []) or []))
            base = {s.lower() for s in (BASE_STYLE_IDS or [])}
            owned_nonbase = [s for s in owned if s and s not in base]

            _rolls_cfg, slots = compute_limits(is_pro=is_pro)
            try:
                upgrades = int(await get_inventory_upgrades(uid) or 0)
            except Exception:
                upgrades = 0
            max_slots = int(slots) + (upgrades * 5)
            current_count = len(owned_nonbase)
            free_slots = max(0, max_slots - current_count)

            # Selected characters that you already own → error, ask to deselect and try again
            dupes = [s for s in selected if s and s in owned]
            if dupes:
                names = []
                for sid in list(dupes)[:5]:
                    s = get_style(sid)
                    names.append(getattr(s, "display_name", None) or sid)
                dup_str = ", ".join(names) + (" and more" if len(dupes) > 5 else "")
                await interaction.followup.send(
                    f"**Already in your collection:** {dup_str}. Remove them from your selection and click **Apply** again.",
                    ephemeral=True,
                )
                return

            # Too many selected for current space → error, ask to select fewer or free space
            if len(selected) > free_slots:
                await interaction.followup.send(
                    f"You can only add **{free_slots}** more character(s) right now (inventory **{current_count} / {max_slots}**). "
                    f"You selected **{len(selected)}**. Select fewer and click **Apply** again, or free space via /character remove.",
                    ephemeral=True,
                )
                return

            # Add all selected to inventory
            added = 0
            for sid in selected:
                if not sid:
                    continue
                ok, _ = await add_style_to_inventory(user_id=uid, style_id=sid, is_pro=is_pro, guild_id=self.guild_id)
                if ok:
                    added += 1
            msg = f"✅ Added **{added}** character(s) to your collection."
            await interaction.followup.send(msg, ephemeral=True)
            try:
                from utils.analytics import track_funnel_event, METRIC_PULL_5, METRIC_PULL_10

                ev = METRIC_PULL_10 if len(self.rolled) >= 10 else METRIC_PULL_5
                await track_funnel_event(guild_id=gid, event=ev, user_id=uid)
            except Exception:
                pass
        except Exception:
            logger.exception("PullResultView._on_apply failed")
            try:
                await interaction.followup.send(
                    "Something went wrong while updating your collection. Please try again or free space via /character remove.",
                    ephemeral=True,
                )
            except Exception:
                pass

        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def _on_deny(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.send_message("❌ Discarded.", ephemeral=True)
        except Exception:
            await _safe_ephemeral_send(interaction, "❌ Discarded.")
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


class ReplaceSelectView(discord.ui.View):
    """Choose which owned characters to replace when inventory overflows."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        to_add: list[str],
        free_slots: int,
        need_replace: int,
        owned_nonbase: list[str],
        is_pro: bool,
        pull_count: int = 5,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.to_add = list(to_add)
        self.free_slots = int(free_slots)
        self.need_replace = int(need_replace)
        self.owned_nonbase = list(owned_nonbase)[:25]
        self.is_pro = is_pro
        self.pull_count = int(pull_count)

        if len(self.owned_nonbase) < need_replace:
            return
        options = []
        for sid in self.owned_nonbase:
            s = get_style(sid)
            name = (s.display_name if s else sid) or sid
            if len(name) > 100:
                name = name[:97] + "..."
            options.append(discord.SelectOption(label=name, value=sid))
        sel = discord.ui.Select(
            placeholder=f"Choose {need_replace} character(s) to replace",
            min_values=need_replace,
            max_values=need_replace,
            options=options,
        )
        sel.callback = self._on_apply
        self.add_item(sel)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            await _safe_ephemeral_send(interaction, "This isn't yours.")
            return False
        return True

    async def _on_apply(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        to_replace = list(interaction.data.get("values") or [])[: self.need_replace]
        if len(to_replace) != self.need_replace:
            await _safe_ephemeral_send(
                interaction,
                f"Please select exactly {self.need_replace} character(s) to replace.",
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        uid = self.user_id
        to_add = self.to_add
        free_slots = self.free_slots
        need_replace = self.need_replace
        try:
            replaced = 0
            for i, old_id in enumerate(to_replace):
                if i >= len(to_add):
                    break
                new_id = to_add[i]
                ok, _ = await replace_style_in_inventory(
                    user_id=uid, old_style_id=old_id, new_style_id=new_id
                )
                if ok:
                    replaced += 1
            added = 0
            for sid in to_add[need_replace:]:
                if added >= free_slots:
                    break
                ok, _ = await add_style_to_inventory(user_id=uid, style_id=sid, is_pro=self.is_pro, guild_id=self.guild_id)
                if ok:
                    added += 1
            discarded = max(0, len(to_add) - need_replace - added)
            msg = f"✅ Replaced **{replaced}** character(s) and added **{replaced + added}** to your collection."
            if discarded > 0:
                msg += f" **{discarded}** could not fit (collection full)."
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            logger.exception("ReplaceSelectView._on_apply failed")
            try:
                await interaction.followup.send(
                    "Something went wrong while updating your collection. Please try again or remove some characters first.",
                    ephemeral=True,
                )
            except Exception:
                pass
        try:
            from utils.analytics import track_funnel_event, METRIC_PULL_5, METRIC_PULL_10

            ev = METRIC_PULL_10 if self.pull_count >= 10 else METRIC_PULL_5
            await track_funnel_event(guild_id=self.guild_id, event=ev, user_id=self.user_id)
        except Exception:
            pass
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


class PointsShopView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        guild_id: int,
        dynamic_items: list[dict] | None = None,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.dynamic_items = list(dynamic_items or [])

        # Add one button per built-in item
        for key, item in SHOP_ITEMS.items():
            emoji = item.get("emoji")
            # Emoji-only button (cost is shown in the embed)
            label = ""
            btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=label,
                emoji=emoji,
                custom_id=f"points_shop_buy:{key}",
            )
            btn.callback = self._make_buy_cb(key)  # type: ignore
            self.add_item(btn)

        # Add dynamic (owner) shop items (LIMITED offers). Keep under component limits.
        for it in self.dynamic_items[:20]:
            iid = str(it.get("item_id") or "").strip()
            if not iid:
                continue
            emoji = str(it.get("button_emoji") or "").strip() or "🎁"
            label = str(it.get("button_label") or "").strip()[:24]
            btn = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label=label,
                emoji=emoji,
                custom_id=f"points_shop_dyn:{iid}",
            )
            btn.callback = self._make_dynamic_buy_cb(iid)  # type: ignore
            self.add_item(btn)

    def _make_buy_cb(self, key: str):
        async def _cb(interaction: discord.Interaction):
            try:
                uid = int(interaction.user.id)
                if key not in SHOP_ITEMS:
                    await _safe_ephemeral_send(interaction, "Unknown shop item.")
                    return

                it = SHOP_ITEMS[key]
                cost = int(it["cost"])

                # Inventory upgrades scale per user: base 500, then +25% each purchase (compounding).
                if key == "inv_upgrade":
                    try:
                        upg = int(await get_inventory_upgrades(uid) or 0)
                        cost = int(math.ceil(500 * (1.25 ** upg)))
                    except Exception:
                        cost = int(it["cost"])

                # Lucky booster stacks: each additional purchase costs +50% (compounding).
                if key == "lucky_booster":
                    try:
                        stacks_now, _exp = await get_booster_stack(guild_id=self.guild_id, user_id=uid, kind="lucky")
                        if stacks_now > 0:
                            cost = int(round(cost * (1.5 ** stacks_now)))
                    except Exception:
                        pass

                ok, new_bal = await spend_points(
                    guild_id=self.guild_id,
                    user_id=uid,
                    cost=cost,
                    reason="shop_purchase",
                    meta={"item": key, "name": it["name"], "cost": cost},
                )
                if not ok:
                    await _safe_ephemeral_send(
                        interaction,
                        f"Not enough points. You have **{new_bal}**, need **{cost}**.",
                    )
                    return

                # 5-pull and 10-pull: defer, roll, show opening + selection UI
                if key in ("pull_5", "pull_10"):
                    count = 10 if key == "pull_10" else 5
                    try:
                        await interaction.response.defer(ephemeral=True)
                    except discord.NotFound:
                        return
                    rolled, err = await do_multi_roll(guild_id=self.guild_id, user_id=uid, count=count)
                    if err and not rolled:
                        await adjust_points(
                            guild_id=self.guild_id,
                            user_id=uid,
                            delta=cost,
                            reason="refund_pull_failed",
                            meta={"item": key, "original_cost": cost},
                        )
                        await interaction.followup.send(f"⚠️ {err}", ephemeral=True)
                        return
                    # Count shop pulls toward Rolls leaderboard (global + server)
                    try:
                        from utils.analytics import track_roll
                        await track_roll(guild_id=self.guild_id, user_id=uid, count=len(rolled))
                    except Exception:
                        pass
                    opening_embed = discord.Embed(description="📦 Opening...")
                    base = (ROLL_ANIMATION_UI_BASE or "").rstrip("/")
                    if base:
                        opening_embed.set_image(url=f"{base}/box.gif")
                    msg = await interaction.followup.send(embed=opening_embed, ephemeral=True, wait=True)
                    await asyncio.sleep(2.5)
                    result_embeds = _build_pull_result_embeds(rolled)
                    view = PullResultView(
                        bot=self.bot,
                        guild_id=self.guild_id,
                        user_id=uid,
                        rolled=rolled,
                    )
                    await msg.edit(embeds=result_embeds, view=view)
                    return

                note = ""
                if key == "extra_roll":
                    await grant_bonus_rolls(user_id=uid, amount=1, ttl_days=30)
                    note = "✅ Granted **+1 bonus roll** (expires in 30 days)."
                elif key == "lucky_booster":
                    # Lucky stacks compound but all stacks expire 1 hour after the FIRST purchase.
                    await set_booster(
                        guild_id=self.guild_id,
                        user_id=uid,
                        kind="lucky",
                        duration_s=3600,
                        stack=True,
                        extend_expiry=False,
                    )
                    try:
                        stacks_now, _ = await get_booster_stack(guild_id=self.guild_id, user_id=uid, kind="lucky")
                        note = f"✅ Booster active: **Lucky x{stacks_now}** (expires in 1 hour from first purchase)."
                    except Exception:
                        note = "✅ Booster active: **Lucky** (1 hour from first purchase)."
                elif key == "inv_upgrade":
                    await increment_inventory_upgrades(uid, delta=1)
                    new_upg = int(await get_inventory_upgrades(uid) or 0)
                    note = f"✅ Inventory increased by **+5 slots** (upgrades: {new_upg})."

                await _safe_ephemeral_send(
                    interaction,
                    f"✅ Purchased **{it['name']}** for **{cost}** points. {note}\nNew balance: **{new_bal}**.",
                )
            except Exception:
                logger.exception("shop button purchase failed")
                await _safe_ephemeral_send(interaction, "⚠️ Purchase failed. Check logs.")

        return _cb

    def _make_dynamic_buy_cb(self, item_id: str):
        async def _cb(interaction: discord.Interaction):
            try:
                uid = int(interaction.user.id)
                iid = str(item_id or "").strip()
                it = await get_shop_item(iid)
                if not it or not bool(it.get("active", True)):
                    await _safe_ephemeral_send(interaction, "This item is no longer available.")
                    return

                cost = int(it.get("cost") or 0)
                if cost <= 0:
                    await _safe_ephemeral_send(interaction, "This item is misconfigured (cost).")
                    return

                kind = str(it.get("kind") or "")
                # Pre-check: inventory capacity for any item that grants characters.
                if kind in {"pack_roll", "character_grant"}:
                    # Tier is per user
                    tier = await get_premium_tier(int(interaction.user.id))
                    is_pro = tier == "pro"
                    st = await load_state(user_id=uid)
                    base = set([s.lower() for s in (BASE_STYLE_IDS or [])])
                    owned_now = [str(x).lower() for x in (list(getattr(st, "owned_custom", []) or [])) if x]
                    owned_nonbase = [x for x in owned_now if x not in base]
                    _rolls_cfg, slots = compute_limits(is_pro=is_pro)
                    try:
                        _upgrades = int(await get_inventory_upgrades(uid) or 0)
                    except Exception:
                        _upgrades = 0
                    slots = int(slots) + (_upgrades * 5)
                    if len(set(owned_nonbase)) >= int(slots):
                        await _safe_ephemeral_send(interaction, "Your collection is full. Remove or replace a character before buying this.")
                        return

                ok, new_bal = await spend_points(
                    guild_id=self.guild_id,
                    user_id=uid,
                    cost=cost,
                    reason="shop_purchase_dynamic",
                    meta={"item": iid, "kind": kind, "cost": cost, "title": it.get("title")},
                )
                if not ok:
                    await _safe_ephemeral_send(
                        interaction,
                        f"Not enough points. You have **{new_bal}**, need **{cost}**.",
                    )
                    return

                # For pack_roll and character_grant: defer and run roll animation + reward embed.
                if kind in {"pack_roll", "character_grant"}:
                    try:
                        await interaction.response.defer(ephemeral=True)
                    except Exception:
                        pass
                    style_def = None
                    badge = ""
                    if kind == "pack_roll":
                        rolled, msg = await self._purchase_pack_roll(uid, it)
                        style_def = rolled
                        if style_def:
                            badge = await badges_for_pack_id(str(it.get("pack_id") or ""))
                    else:
                        msg = await self._purchase_character_grant(uid, it)
                        sid = str(it.get("style_id") or "").strip().lower()
                        if sid:
                            style_def = get_style(sid)
                            if style_def:
                                badge = await badges_for_style_id(sid)

                    if style_def is not None:
                        e, f = character_embed(style_def, badges=badge)
                        # Only run the spinning animation for pack rolls, not direct character buys
                        if kind == "pack_roll":
                            try:
                                animated_msg = await run_roll_reveal_animation(
                                    interaction,
                                    style_def,
                                    user_id=uid,
                                    guild_id=self.guild_id,
                                    reward_embed=e,
                                    reward_file=f,
                                    view=None,
                                )
                                if animated_msg is not None:
                                    return
                            except Exception:
                                logger.exception("roll reveal animation failed for shop purchase")
                        else:
                            # character_grant: show the character embed directly, no animation
                            try:
                                await interaction.followup.send(
                                    content=f"✅ Purchased **{it.get('title') or iid}** for **{cost}** points. New balance: **{new_bal}**.",
                                    embed=e,
                                    file=f,
                                    ephemeral=True,
                                )
                                return
                            except Exception:
                                logger.exception("character grant embed send failed")
                    await _safe_ephemeral_send(
                        interaction,
                        f"✅ Purchased **{it.get('title') or iid}** for **{cost}** points.\n{msg}\nNew balance: **{new_bal}**.",
                    )
                    return

                # Other kinds (not pack_roll / character_grant)
                msg = "⚠️ This item kind is not supported yet."
                await _safe_ephemeral_send(
                    interaction,
                    f"✅ Purchased **{it.get('title') or iid}** for **{cost}** points.\n{msg}\nNew balance: **{new_bal}**.",
                )
            except Exception:
                logger.exception("dynamic shop purchase failed")
                await _safe_ephemeral_send(interaction, "⚠️ Purchase failed. Check logs.")

        return _cb

    async def _purchase_character_grant(self, user_id: int, it: dict) -> str:
        sid = str(it.get("style_id") or "").strip().lower()
        from utils.packs_store import is_pack_official
        from utils.character_registry import get_style
        _sd = get_style(sid)
        _pack_of_char = getattr(_sd, "pack_id", "core") if _sd else "core"
        if not await is_pack_official(_pack_of_char):
            return "⚠️ This character cannot be sold in the shop (non-official pack)."

        tier = await get_premium_tier(user_id)
        is_pro = tier == "pro"
        ok, msg = await add_style_to_inventory(user_id=user_id, style_id=sid, is_pro=is_pro, guild_id=self.guild_id)
        if ok:
            badge = await badges_for_style_id(sid)
            return f"🎁 Granted **{sid}** — {badge}".strip()
        return f"⚠️ {msg}"

    async def _purchase_pack_roll(self, user_id: int, it: dict) -> tuple[object | None, str]:
        """Returns (rolled_style_def, status_msg). rolled_style_def is None on error."""
        pid = str(it.get("pack_id") or "").strip().lower()
        if not pid:
            return None, "⚠️ Misconfigured item (no pack_id)."
        from utils.packs_store import is_pack_official
        if not await is_pack_official(pid):
            return None, "⚠️ This pack cannot be sold in the shop (non-official pack)."
        if not list_rollable(pack_ids={pid}):
            return None, "⚠️ No rollable characters in that pack (or pack disabled)."
        pity_mythic, pity_legendary = await get_pity(user_id=user_id)
        legendary_mult = mythic_mult = 1.0
        try:
            stacks, _ = await get_booster_stack(guild_id=self.guild_id, user_id=user_id, kind="lucky")
            if stacks > 0 and await is_pack_official(pid):
                mult = float(1.5 ** int(stacks))
                legendary_mult = mythic_mult = mult
        except Exception:
            pass
        rng = random.Random()
        rolled = roll_style(
            pity_legendary=pity_legendary,
            pity_mythic=pity_mythic,
            rng=rng,
            legendary_mult=legendary_mult,
            mythic_mult=mythic_mult,
            pack_ids={pid},
        )
        await apply_pity_after_roll(
            guild_id=self.guild_id,
            user_id=user_id,
            rolled_rarity=rolled.rarity,
        )
        r_pity = str(getattr(rolled, "rarity", "") or "").strip().lower()
        if r_pity == "mythic":
            new_leg, new_myth = 0, 0
        elif r_pity == "legendary":
            new_leg, new_myth = 0, min(999, pity_mythic + 1)
        else:
            new_leg, new_myth = min(99, pity_legendary + 1), min(999, pity_mythic + 1)
        try:
            await set_pity(user_id=user_id, pity_mythic=new_myth, pity_legendary=new_leg)
        except Exception:
            pass
        tier = await get_premium_tier(user_id)
        is_pro = tier == "pro"
        ok, msg = await add_style_to_inventory(user_id=user_id, style_id=rolled.style_id, is_pro=is_pro, guild_id=self.guild_id)
        badge = await badges_for_pack_id(pid)
        if ok:
            return rolled, f"🎲 Rolled **{rolled.display_name}** — {badge}"
        return rolled, f"🎲 Rolled **{rolled.display_name}** — {badge}\n⚠️ {msg}"


# Number emoji for cosmetic shop buttons (1–7)
_COSMETIC_NUM_EMOJI = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣")


class CosmeticsShopView(discord.ui.View):
    """Cosmetic shop: 7 buttons (1–7) to buy cosmetics for 500 points each."""

    def __init__(self, *, bot: commands.Bot, guild_id: int, user_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        for num in range(1, 8):
            emoji = _COSMETIC_NUM_EMOJI[num - 1]
            btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=str(num),
                emoji=emoji,
                custom_id=f"cosmetic_shop:{num}",
            )
            btn.callback = self._make_buy_cb(num)  # type: ignore
            self.add_item(btn)

    def _make_buy_cb(self, num: int):
        async def _cb(interaction: discord.Interaction):
            if int(interaction.user.id) != self.user_id:
                await _safe_ephemeral_send(interaction, "This shop isn't yours.")
                return
            cosmetic_id = NUM_TO_COSMETIC_ID.get(num)
            if not cosmetic_id:
                await _safe_ephemeral_send(interaction, "Invalid cosmetic.")
                return
            uid = self.user_id
            gid = self.guild_id
            owned = await get_owned(uid)
            if cosmetic_id in owned:
                await _safe_ephemeral_send(
                    interaction,
                    f"You already own **{cosmetic_display_name(cosmetic_id)}**. Use `/cosmetic select` to equip it.",
                )
                return
            cost = COSMETIC_PRICE
            ok, new_bal = await spend_points(
                guild_id=gid,
                user_id=uid,
                cost=cost,
                reason="cosmetic_purchase",
                meta={"cosmetic_id": cosmetic_id, "name": cosmetic_display_name(cosmetic_id)},
            )
            if not ok:
                await _safe_ephemeral_send(
                    interaction,
                    f"Not enough points. You have **{new_bal}**, need **{cost}**.",
                )
                return
            await add_owned(uid, cosmetic_id)
            await _safe_ephemeral_send(
                interaction,
                f"✅ Purchased **{cosmetic_display_name(cosmetic_id)}** for **{cost}** points. Use `/cosmetic select` to show it on your profile.\nNew balance: **{new_bal}**.",
            )

        return _cb


class PointsQuestsView(discord.ui.View):
    def __init__(self, *, bot: commands.Bot, guild_id: int, user_id: int, claimable_ids: list[str], timeout: float = 180):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)

        # Claim all button first
        if claimable_ids:
            btn_all = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="Claim All",
                emoji="🎁",
                custom_id="points_quests_claim_all",
            )
            btn_all.callback = self._claim_all_cb  # type: ignore
            self.add_item(btn_all)

        # One button per claimable quest (max 24 remaining slots)
        for qid in claimable_ids[:24]:
            num = quest_number(qid)
            btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=f"Claim {num}" if num > 0 else "Claim",
                emoji="🎁",
                custom_id=f"points_quests_claim:{qid}",
            )
            btn.callback = self._make_claim_one_cb(qid)  # type: ignore
            self.add_item(btn)

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        """Rebuild the quests embed + view after claiming."""
        try:
            embed = await build_quest_status_embed(guild_id=self.guild_id, user_id=self.user_id)
            claimable = await get_claimable_quest_ids(guild_id=self.guild_id, user_id=self.user_id)
            view = PointsQuestsView(bot=self.bot, guild_id=self.guild_id, user_id=self.user_id, claimable_ids=claimable)
            if interaction.response.is_done():
                await interaction.message.edit(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
        except discord.NotFound:
            # Message was deleted (e.g. user closed the panel); nothing to refresh.
            logger.debug("Quests message no longer exists, skipping refresh")
        except Exception:
            logger.exception("Failed refreshing quests message")

    async def _claim_all_cb(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.user_id:
            await _safe_ephemeral_send(interaction, "This isn't your quest panel.")
            return
        try:
            total, new_bal, claimed_defs = await claim_all_rewards(guild_id=self.guild_id, user_id=self.user_id)
            if total <= 0:
                await _safe_ephemeral_send(interaction, "Nothing to claim right now.")
            else:
                names = ", ".join([c.name for c in claimed_defs[:4]])
                more = f" +{len(claimed_defs)-4} more" if len(claimed_defs) > 4 else ""
                await _safe_ephemeral_send(
                    interaction,
                    f"🎁 Claimed **{total}** points ({names}{more}). New balance: **{new_bal}**.",
                )
                # KAI mascot: kaihappy on claim all
                try:
                    kai_msg = get_kai_claim_all_quests_message(total, new_bal)
                    await interaction.followup.send(embed=embed_kaihappy(kai_msg), ephemeral=True)
                except Exception:
                    logger.exception("KAI claim-all followup failed")
            await self._refresh_message(interaction)
        except Exception:
            logger.exception("Claim all failed")
            await _safe_ephemeral_send(interaction, "⚠️ Claim failed. Check logs.")

    def _make_claim_one_cb(self, quest_id: str):
        async def _cb(interaction: discord.Interaction):
            if int(interaction.user.id) != self.user_id:
                await _safe_ephemeral_send(interaction, "This isn't your quest panel.")
                return
            try:
                ok, msg, awarded, new_bal = await claim_quest_reward(
                    guild_id=self.guild_id,
                    user_id=self.user_id,
                    quest_id=quest_id,
                )
                if not ok:
                    await _safe_ephemeral_send(interaction, msg)
                else:
                    await _safe_ephemeral_send(
                        interaction,
                        f"🎁 {msg} **+{awarded}** points. New balance: **{new_bal}**.",
                    )
                    # KAI mascot: kaihappy only when claiming multiple (see claim-all below)
                await self._refresh_message(interaction)
            except Exception:
                logger.exception("Claim one failed")
                await _safe_ephemeral_send(interaction, "⚠️ Claim failed. Check logs.")

        return _cb




class StreakRestoreView(discord.ui.View):
    def __init__(self, bot: commands.Bot, *, guild_id: int, user_id: int, cost: int = 500):
        super().__init__(timeout=7 * 24 * 60 * 60)  # 7 days
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.cost = int(cost or 500)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            try:
                await interaction.response.send_message("Only the original claimant can use this button.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="Keep streak (500)", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def restore_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            ok, msg, new_balance, new_streak = await restore_daily_streak(
                guild_id=self.guild_id, user_id=self.user_id, cost=self.cost
            )
            if not ok:
                await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
                return

            # Disable after use
            button.disabled = True

            e = discord.Embed(title="🛡️ Streak protected!", description=f"-{self.cost} points", color=0x2ECC71)
            e.add_field(name="Balance", value=f"**{new_balance}** points", inline=True)
            e.add_field(name="Streak", value=f"**{new_streak}** day(s)", inline=True)
            await interaction.followup.send(embed=e, ephemeral=True)
        except Exception:
            logger.exception("restore_btn failed")
            try:
                await interaction.followup.send("⚠️ Streak restore failed. Check logs.", ephemeral=True)
            except Exception:
                pass



class ClaimDailyView(discord.ui.View):
    def __init__(self, *, bot: commands.Bot, guild_id: int, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message("❌ This button isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Claim Daily", style=discord.ButtonStyle.primary, emoji="🎁")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Claim the daily reward and update the original message.
        res = await claim_daily(guild_id=self.guild_id, user_id=self.user_id)
        bal = await get_balance(guild_id=self.guild_id, user_id=self.user_id)

        # Pack creator bonus (only when they actually claimed today; server packs grant no bonus)
        pack_bonus = 0
        if res.awarded > 0:
            try:
                pack_bonus, _breakdown = await get_pack_creator_daily_bonus(
                    self.bot,
                    self.user_id,
                )
                if pack_bonus > 0:
                    await adjust_points(
                        guild_id=0,
                        user_id=self.user_id,
                        delta=pack_bonus,
                        reason="pack_creator_daily",
                        meta={"source": "pack_creator_rewards"},
                    )
                    bal = await get_balance(guild_id=self.guild_id, user_id=self.user_id)
            except Exception:
                logger.exception("Pack creator daily bonus failed")

        # Build description (DailyResult has no .message)
        desc = f"You claimed **{res.awarded}** points! Streak: **{res.streak}** day(s)."
        if getattr(res, "first_bonus_awarded", 0) > 0:
            desc += f" (Including **{res.first_bonus_awarded}** first-time bonus!)"
        if pack_bonus > 0:
            desc += f" + **{pack_bonus}** pack creator bonus!"

        e = discord.Embed(title="Daily Reward", description=desc)
        e.add_field(name="Balance", value=f"{bal} points", inline=True)
        e.add_field(name="Streak", value=str(res.streak), inline=True)

        # Disable the button after claim.
        button.disabled = True
        await interaction.response.edit_message(embed=e, view=self)

        # KAI mascot: Kailove on daily claim
        try:
            kai_msg = get_kai_daily_claim_message(
                res.awarded, res.streak, getattr(res, "first_bonus_awarded", 0),
            )
            kai_embed = embed_kailove(kai_msg, title=get_kai_daily_claim_message_varied(res.streak))
            await interaction.followup.send(embed=kai_embed, ephemeral=True)
        except Exception:
            logger.exception("KAI daily followup failed")

        # After-claim DM (reminder users only); best-effort, don't block
        try:
            asyncio.create_task(
                send_after_claim_dm(self.bot, self.user_id, res.streak),
            )
        except Exception:
            logger.exception("After-claim DM schedule failed")

        # If a restore is available, offer it as a follow-up.
        if getattr(res, "restore_available", False):
            restore_view = StreakRestoreView(
                interaction.client,
                guild_id=self.guild_id,
                user_id=self.user_id,
                cost=getattr(res, "restore_cost", 500),
            )
            await interaction.followup.send(
                "Your streak can be restored for a cost (limited time):",
                ephemeral=True,
                view=restore_view,
            )


class SlashPoints(commands.Cog):
    """Points system: daily claims, streaks, and a small test shop."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    points = app_commands.Group(name="points", description="Daily points, streaks, and shop")

    @points.command(name="daily", description="Claim your daily points reward")
    async def points_daily(self, interaction: discord.Interaction):
        """Show daily status and let the user claim via a button."""
        gid = int(interaction.guild_id or 0)
        uid = int(interaction.user.id)

        claimed, next_in_s, streak = await get_claim_status(guild_id=gid, user_id=uid)
        bal = await get_balance(guild_id=gid, user_id=uid)

        if claimed:
            desc = "✅ You already claimed your daily today."
        else:
            desc = "Press **Claim Daily** to collect your reward."

        e = discord.Embed(title="Daily Reward", description=desc)
        e.add_field(name="Balance", value=f"{bal} points", inline=True)
        e.add_field(name="Streak", value=str(streak), inline=True)
        if claimed and next_in_s is not None:
            e.add_field(name="Next claim", value=f"in {int(next_in_s)}s", inline=False)

        view = None if claimed else ClaimDailyView(bot=self.bot, guild_id=gid, user_id=uid)

        if view is None:
            await interaction.response.send_message(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True, view=view)

    @points.command(name="balance", description="View your points balance and streak")
    async def points_balance(self, interaction: discord.Interaction):
        try:
            gid = _safe_guild_id(interaction)
            if gid is None:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
                return
            uid = int(interaction.user.id)

            bal = await get_balance(guild_id=gid, user_id=uid)
            claimed, next_in, streak = await get_claim_status(guild_id=gid, user_id=uid)

            e = discord.Embed(
                title="🏦 Your Points",
                description=f"Balance: **{bal}** points",
                color=0x3498DB,
            )
            e.add_field(name="Streak", value=f"**{streak}** day(s)", inline=True)
            if claimed:
                e.add_field(name="Next claim", value=f"In **{_fmt_duration(next_in)}** (UTC reset)", inline=True)
            else:
                e.add_field(name="Next claim", value="✅ Available now (use `/points daily`)", inline=True)

            preview = build_roadmap_preview(current_streak=max(1, streak), days=10)
            e.add_field(
                name="10-day roadmap",
                value="\n".join([f"Day {i+1}: **{amt}**" for i, amt in enumerate(preview)]),
                inline=False,
            )

            await interaction.response.send_message(embed=e, ephemeral=True)
        except Exception:
            logger.exception("/points balance failed")
            try:
                await interaction.response.send_message("⚠️ Balance lookup failed. Check logs.", ephemeral=True)
            except Exception:
                pass

    @points.command(name="reminders", description="Turn streak reminder DMs on or off (daily + 90-min warning)")
    @app_commands.choices(
        toggle=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ],
    )
    async def points_reminders(self, interaction: discord.Interaction, toggle: app_commands.Choice[str]):
        """Set whether to receive daily streak reminder DMs and 90-min-before-midnight warning."""
        try:
            uid = int(interaction.user.id)
            enabled = toggle.value.lower() == "on"
            await set_streak_reminders_enabled(uid, enabled)
            status = "on" if enabled else "off"
            if enabled:
                msg = "Streak reminder DMs are now **on**. You'll get a daily reminder and a 90-minute warning before midnight UTC if you haven't claimed."
            else:
                msg = "Streak reminder DMs are now **off**. You won't receive reminder or warning DMs."
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            logger.exception("/points reminders failed")
            try:
                await interaction.response.send_message("⚠️ Could not update setting. Try again later.", ephemeral=True)
            except Exception:
                pass

    @points.command(name="luck", description="See how many Lucky boosters you have stacked")
    async def points_luck(self, interaction: discord.Interaction):
        try:
            gid = _safe_guild_id(interaction)
            if gid is None:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
                return
            uid = int(interaction.user.id)
            stacks, exp = await get_booster_stack(guild_id=gid, user_id=uid, kind="lucky")
            if stacks <= 0 or not exp:
                await interaction.response.send_message("🍀 You have **0** Lucky boosters active.", ephemeral=True)
                return
            # remaining seconds
            try:
                rem = int((exp - _now_utc()).total_seconds())
            except Exception:
                rem = 0
            msg = f"🍀 Lucky boosters stacked: **{stacks}**\nExpires in **{_fmt_duration(max(0, rem))}**."
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            logger.exception("/points luck failed")
            try:
                await interaction.response.send_message("⚠️ Luck lookup failed. Check logs.", ephemeral=True)
            except Exception:
                pass

    @points.command(name="shop", description="Browse the points shop")
    async def points_shop(self, interaction: discord.Interaction):
        try:
            gid = _safe_guild_id(interaction)
            if gid is None:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
                return
            uid = int(interaction.user.id)
            bal = await get_balance(guild_id=gid, user_id=uid)
            dyn_items = await list_shop_items()

            e = discord.Embed(
                title="🛒 Points Shop (test)",
                description=f"Balance: **{bal}** points\n\nTap a button to purchase.",
                color=0xF1C40F,
            )

            for key, item in SHOP_ITEMS.items():
                shown_cost = int(item['cost'])
                extra = ""
                if key == "lucky_booster":
                    try:
                        stacks_now, _exp = await get_booster_stack(guild_id=gid, user_id=uid, kind="lucky")
                        if stacks_now > 0:
                            shown_cost = int(round(shown_cost * (1.5 ** stacks_now)))
                            extra = f" (next x{stacks_now+1})"
                    except Exception:
                        pass
                elif key == "inv_upgrade":
                    try:
                        upg = int(await get_inventory_upgrades(uid) or 0)
                        shown_cost = int(math.ceil(500 * (1.25 ** upg)))
                        extra = f" (purchased {upg}x)"
                    except Exception:
                        # Fall back to base cost
                        shown_cost = int(item.get("cost") or 500)
                e.add_field(
                    name=f"{item.get('emoji','')}{item['name']}{item.get('emoji','')} — {shown_cost} points{extra}",
                    value=f"`{key}`\n{item['desc']}",
                    inline=False,
                )

            embeds: list[discord.Embed] = [e]
            files: list[discord.File] = []

            if dyn_items:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                # Cap at 9 limited embeds so total embeds <= 10
                for it in dyn_items[:9]:
                    kind = str(it.get("kind") or "")
                    title = str(it.get("title") or it.get("item_id"))
                    cost = int(it.get("cost") or 0)
                    desc = str(it.get("description") or "").strip()
                    iid = str(it.get("item_id") or "")
                    extra = ""
                    image_url: str | None = None
                    if kind == "pack_roll":
                        pid = normalize_pack_id(str(it.get("pack_id") or ""))
                        if pid:
                            pack = await get_custom_pack(pid)
                            if pack and isinstance(pack, dict):
                                try:
                                    merge_pack_payload(pack)
                                except Exception:
                                    pass
                                image_url = pack.get("image_url") if isinstance(pack.get("image_url"), str) else None
                            badge = await badges_for_pack_id(pid) if pid else ""
                            extra = f"\nPack: `{pid}` — {badge}" if pid else ""
                    elif kind == "character_grant":
                        sid = normalize_style_id(str(it.get("style_id") or ""))
                        if sid:
                            style = get_style(sid)
                            if style is None:
                                try:
                                    packs = await list_custom_packs(limit=600, include_internal=True, include_shop_only=True)
                                    for p in packs or []:
                                        if not isinstance(p, dict):
                                            continue
                                        for c in p.get("characters") or []:
                                            if not isinstance(c, dict):
                                                continue
                                            if normalize_style_id(str(c.get("id") or c.get("style_id") or "")) == sid:
                                                try:
                                                    merge_pack_payload(p)
                                                except Exception:
                                                    pass
                                                break
                                        if get_style(sid):
                                            break
                                except Exception:
                                    pass
                            style = get_style(sid)
                            if style:
                                image_url = getattr(style, "image_url", None)
                            extra = f"\nCharacter: `{sid}`"

                    le = discord.Embed(
                        title=f"🕒 {title} — {cost} points",
                        description=f"`{iid}`\n{desc}{extra}".strip() or "\u200b",
                        color=0xF1C40F,
                    )
                    if image_url:
                        if isinstance(image_url, str) and image_url.strip().lower().startswith("asset:"):
                            rel = image_url[6:].strip().lstrip("/")
                            if rel:
                                abs_path = asset_abspath(rel)
                                if os.path.isfile(abs_path):
                                    att_name = f"shop_{iid}_{len(files)}.png"
                                    files.append(discord.File(abs_path, filename=att_name))
                                    le.set_image(url=f"attachment://{att_name}")
                        elif isinstance(image_url, str) and image_url.strip().lower().startswith("http"):
                            le.set_image(url=image_url.strip())
                    embeds.append(le)

                view = PointsShopView(bot=self.bot, guild_id=gid, dynamic_items=dyn_items)
                send_kw: dict = {"embeds": embeds, "view": view, "ephemeral": True}
                if files:
                    send_kw["files"] = files
                await interaction.followup.send(**send_kw)
            else:
                view = PointsShopView(bot=self.bot, guild_id=gid, dynamic_items=dyn_items)
                await interaction.response.send_message(embed=e, view=view, ephemeral=True)
        except Exception:
            logger.exception("/points shop failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("⚠️ Shop failed. Check logs.", ephemeral=True)
                else:
                    await interaction.response.send_message("⚠️ Shop failed. Check logs.", ephemeral=True)
            except Exception:
                pass

    @points.command(name="cosmetic-shop", description="Browse and buy cosmetics for your profile (inspect)")
    async def points_cosmetic_shop(self, interaction: discord.Interaction):
        try:
            gid = _safe_guild_id(interaction)
            if gid is None:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
                return
            # Defer immediately so the 3s interaction window doesn't expire while fetching images.
            await interaction.response.defer(ephemeral=True)
            uid = int(interaction.user.id)
            bal = await get_balance(guild_id=gid, user_id=uid)
            owned = await get_owned(uid)
            # Fetch all cosmetic images in parallel; if primary URL (e.g. R2) returns 404, fall back to default CDN.
            catalog_list = list(COSMETIC_CATALOG)
            img_urls = [cosmetic_image_url(cid) for _num, cid, _ in catalog_list]
            attach_names = [f"cosmetic_{cid}.png" for _num, cid, _ in catalog_list]

            async def _fetch_one(primary_url: str | None, cosmetic_id: str, filename: str):
                f = await fetch_embed_image_as_file(primary_url, filename=filename)
                if f is not None:
                    return f
                fallback = default_cosmetic_image_url(cosmetic_id)
                return await fetch_embed_image_as_file(fallback, filename=filename)

            fetched = await asyncio.gather(
                *[
                    _fetch_one(url, cid, name)
                    for (_, cid, _), url, name in zip(catalog_list, img_urls, attach_names)
                ],
            )
            files = [f for f in fetched if f is not None]
            embeds = []
            for idx, (num, cosmetic_id, display_name) in enumerate(catalog_list):
                img_url = img_urls[idx]
                if fetched[idx] is not None:
                    img_url = f"attachment://{attach_names[idx]}"
                e = discord.Embed(
                    title=f"{_COSMETIC_NUM_EMOJI[num - 1]} {num} — {display_name}",
                    description=f"**{COSMETIC_PRICE}** points",
                    color=0x9B59B6,
                )
                if img_url:
                    e.set_image(url=img_url)
                if cosmetic_id in owned:
                    e.set_footer(text="✓ Owned — use /cosmetic select to equip")
                embeds.append(e)
            view = CosmeticsShopView(bot=self.bot, guild_id=gid, user_id=uid)
            send_kw: dict = {
                "content": f"🪞 **Cosmetic Shop** — Balance: **{bal}** points. Tap a button to purchase.",
                "embeds": embeds[:10],
                "view": view,
                "ephemeral": True,
            }
            if files:
                send_kw["files"] = files
            await interaction.followup.send(**send_kw)
        except Exception:
            logger.exception("/points cosmetic-shop failed")
            try:
                await interaction.followup.send("⚠️ Cosmetic shop failed. Check logs.", ephemeral=True)
            except Exception:
                pass

    @points.command(name="quests", description="View your daily/weekly quests and progress")
    async def points_quests(self, interaction: discord.Interaction):
        try:
            gid = _safe_guild_id(interaction)
            if gid is None:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
                return
            uid = int(interaction.user.id)

            embed = await build_quest_status_embed(guild_id=gid, user_id=uid)
            # KAI mascot: start.png + greeting when opening quests
            embed.set_image(url=_url(KAI_START_IMAGE))
            greeting = get_kai_quests_open_greeting()
            if embed.description:
                embed.description = f"**KAI says:** {greeting}\n\n{embed.description}"
            else:
                embed.description = f"**KAI says:** {greeting}"
            claimable = await get_claimable_quest_ids(guild_id=gid, user_id=uid)
            view = PointsQuestsView(bot=self.bot, guild_id=gid, user_id=uid, claimable_ids=claimable)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception:
            logger.exception("/points quests failed")
            try:
                await interaction.response.send_message("⚠️ Quests failed. Check logs.", ephemeral=True)
            except Exception:
                pass

    @points.command(name="buy", description="Buy an item from the points shop")
    @app_commands.describe(item="Shop item id (use /points shop)")
    async def points_buy(self, interaction: discord.Interaction, item: str):
        try:
            gid = _safe_guild_id(interaction)
            if gid is None:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
                return
            uid = int(interaction.user.id)

            key = (item or "").strip().lower()

            # Dynamic items (owner-curated limited offers)
            dyn = await get_shop_item(key)
            if dyn and bool(dyn.get("active", True)):
                kind = str(dyn.get("kind") or "")
                cost = int(dyn.get("cost") or 0)
                if cost <= 0:
                    await interaction.response.send_message("This item is misconfigured.", ephemeral=True)
                    return

                # Pre-check inventory capacity for grants
                if kind in {"pack_roll", "character_grant"}:
                    tier = await get_premium_tier(int(interaction.user.id))
                    is_pro = tier == "pro"
                    st = await load_state(user_id=uid)
                    base = set([s.lower() for s in (BASE_STYLE_IDS or [])])
                    owned_now = [str(x).lower() for x in (list(getattr(st, "owned_custom", []) or [])) if x]
                    owned_nonbase = [x for x in owned_now if x not in base]
                    _rolls_cfg, slots = compute_limits(is_pro=is_pro)
                    try:
                        _upgrades = int(await get_inventory_upgrades(uid) or 0)
                    except Exception:
                        _upgrades = 0
                    slots = int(slots) + (_upgrades * 5)
                    if len(set(owned_nonbase)) >= int(slots):
                        await interaction.response.send_message("Your collection is full.", ephemeral=True)
                        return

                ok, new_bal = await spend_points(
                    guild_id=gid,
                    user_id=uid,
                    cost=cost,
                    reason="shop_purchase_dynamic",
                    meta={"item": key, "kind": kind, "cost": cost, "title": dyn.get("title")},
                )
                if not ok:
                    await interaction.response.send_message(
                        f"Not enough points. You have **{new_bal}**, need **{cost}**.",
                        ephemeral=True,
                    )
                    return

                note = ""
                if kind == "character_grant":
                    sid = str(dyn.get("style_id") or "").strip().lower()
                    tier = await get_premium_tier(int(interaction.user.id))
                    is_pro = tier == "pro"
                    ok2, msg2 = await add_style_to_inventory(user_id=uid, style_id=sid, is_pro=is_pro, guild_id=gid)
                    badge = await badges_for_style_id(sid)
                    note = f"🎁 Granted **{sid}** — {badge}" if ok2 else f"⚠️ {msg2}"
                elif kind == "pack_roll":
                    pid = str(dyn.get("pack_id") or "").strip().lower()
                    if not list_rollable(pack_ids={pid}):
                        note = "⚠️ No rollable characters in that pack."
                    else:
                        pity_mythic, pity_legendary = await get_pity(user_id=uid)
                        leg_mult = myth_mult = 1.0
                        try:
                            stacks, _ = await get_booster_stack(guild_id=gid, user_id=uid, kind="lucky")
                            if stacks > 0:
                                leg_mult = myth_mult = float(1.5 ** int(stacks))
                        except Exception:
                            pass
                        rng = random.Random()
                        rolled = roll_style(
                            pity_legendary=pity_legendary,
                            pity_mythic=pity_mythic,
                            rng=rng,
                            legendary_mult=leg_mult,
                            mythic_mult=myth_mult,
                            pack_ids={pid},
                        )
                        await apply_pity_after_roll(guild_id=gid, user_id=uid, rolled_rarity=rolled.rarity)
                        r_p = str(getattr(rolled, "rarity", "") or "").strip().lower()
                        if r_p == "mythic":
                            nleg, nmyth = 0, 0
                        elif r_p == "legendary":
                            nleg, nmyth = 0, min(999, pity_mythic + 1)
                        else:
                            nleg, nmyth = min(99, pity_legendary + 1), min(999, pity_mythic + 1)
                        try:
                            await set_pity(user_id=uid, pity_mythic=nmyth, pity_legendary=nleg)
                        except Exception:
                            pass
                        tier = await get_premium_tier(int(interaction.user.id))
                        is_pro = tier == "pro"
                        ok2, msg2 = await add_style_to_inventory(user_id=uid, style_id=rolled.style_id, is_pro=is_pro, guild_id=gid)
                        badge = await badges_for_pack_id(pid)
                        note = f"🎲 Rolled **{rolled.display_name}** — {badge}" if ok2 else f"🎲 Rolled **{rolled.display_name}** — {badge}\n⚠️ {msg2}"
                else:
                    note = "⚠️ Unsupported item kind."

                e = discord.Embed(
                    title="✅ Purchase successful",
                    description=f"Bought **{dyn.get('title') or key}** for **{cost}** points.\n{note}",
                    color=0x2ECC71,
                )
                e.add_field(name="New balance", value=f"**{new_bal}** points", inline=False)
                await interaction.response.send_message(embed=e, ephemeral=True)
                return

            # Hidden test item: /points buy secret (costs 10, no effect)
            if key == "secret":
                cost = 10
                ok, new_bal = await spend_points(
                    guild_id=gid,
                    user_id=uid,
                    cost=cost,
                    reason="shop_purchase",
                    meta={"item": "secret", "name": "Secret", "cost": cost},
                )
                if not ok:
                    await interaction.response.send_message(
                        f"Not enough points. You have **{new_bal}**, need **{cost}**.",
                        ephemeral=True,
                    )
                    return

                e = discord.Embed(
                    title="✅ Purchase successful",
                    description=f"Bought **Secret** for **{cost}** points. (No effect yet)",
                    color=0x2ECC71,
                )
                e.add_field(name="New balance", value=f"**{new_bal}** points", inline=False)
                await interaction.response.send_message(embed=e, ephemeral=True)
                return

            if key not in SHOP_ITEMS:
                await interaction.response.send_message("Unknown item. Use `/points shop`.", ephemeral=True)
                return

            it = SHOP_ITEMS[key]
            cost = int(it["cost"])

            ok, new_bal = await spend_points(
                guild_id=gid,
                user_id=uid,
                cost=cost,
                reason="shop_purchase",
                meta={"item": key, "name": it["name"], "cost": cost},
            )
            if not ok:
                await interaction.response.send_message(
                    f"Not enough points. You have **{new_bal}**, need **{cost}**.",
                    ephemeral=True,
                )
                return

            # Apply purchase effects
            note = ""
            if key == "extra_roll":
                await grant_bonus_rolls(user_id=uid, amount=1, ttl_days=30)
                note = "✅ Granted **+1 bonus roll** (expires in 30 days)."
            elif key == "lucky_booster":
                await set_booster(guild_id=gid, user_id=uid, kind="lucky", duration_s=3600, stack=True)
                note = "✅ Booster active: **Lucky** (1 hour)."
            elif key == "shard_pack":
                # Note: shards are stored in CharacterUserState.points (legacy naming)
                await add_shards(user_id=uid, amount=3)
                note = "✅ Granted **+3 shards**."

            e = discord.Embed(
                title="✅ Purchase successful",
                description=f"Bought **{it['name']}** for **{cost}** points.\n\n{note}",
                color=0x2ECC71,
            )
            e.add_field(name="New balance", value=f"**{new_bal}** points", inline=False)
            await interaction.response.send_message(embed=e, ephemeral=True)

        except Exception:
            logger.exception("/points buy failed")
            try:
                await interaction.response.send_message("⚠️ Purchase failed. Check logs.", ephemeral=True)
            except Exception:
                pass

    # /points roadmap removed (redundant with /points daily)


    @points.command(name="convert", description="Convert between shards and points (50 points per shard).")
    @app_commands.describe(direction="Conversion direction", shards="How many shards to convert")
    @app_commands.choices(direction=[
        app_commands.Choice(name="Shards → Points", value="shards_to_points"),
        app_commands.Choice(name="Points → Shards", value="points_to_shards"),
    ])
    async def points_convert(self, interaction: discord.Interaction, direction: app_commands.Choice[str], shards: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid = int(interaction.guild_id or 0)
        uid = int(interaction.user.id)
        shards = int(shards)
        if shards <= 0:
            await interaction.followup.send("Amount must be a positive integer.", ephemeral=True)
            return
        rate = 50
        if direction.value == "points_to_shards":
            cost = shards * rate
            ok, msg = await spend_points(guild_id=gid, user_id=uid, amount=cost, reason="convert_points_to_shards")
            if not ok:
                await interaction.followup.send(msg or "Not enough points.", ephemeral=True)
                return
            await add_shards(uid, shards)
            await interaction.followup.send(f"Converted **{cost}** points into **{shards}** shards.", ephemeral=True)
            return
        # shards -> points
        st = await load_state(uid)
        if int(st.points) < shards:
            await interaction.followup.send("Not enough shards.", ephemeral=True)
            return
        # spend shards by adding negative shards
        await add_shards(uid, -shards)
        gained = shards * rate
        await add_points(guild_id=gid, user_id=uid, amount=gained, reason="convert_shards_to_points")
        await interaction.followup.send(f"Converted **{shards}** shards into **{gained}** points.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(SlashPoints(bot))