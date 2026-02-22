# commands/slash/owner.py
from __future__ import annotations

import logging
import time
import json

import discord
from discord import app_commands
from discord.ext import commands

from utils.owner import is_bot_owner
from utils.storage import set_guild_setting
from utils.premium import get_premium_tier
from utils.db import get_sessionmaker
from utils.models import AnalyticsDailyMetric
from utils.models import GuildSetting, PremiumEntitlement, UserPremiumEntitlement, BondState, VoiceSound
from utils.models import CharacterUserState, CharacterOwnedStyle, CharacterCustomStyle, UserFirstSeen, UserActivityDay
from utils.models import PointsWallet, PointsLedger, QuestProgress, QuestClaim
from utils.points_store import adjust_points
from utils.packs_store import (
    get_custom_pack,
    upsert_custom_pack,
    delete_custom_pack,
    normalize_pack_id,
    normalize_style_id,
    list_custom_packs,
    add_featured_pack,
    remove_featured_pack,
    get_featured_pack_ids,
)
from utils.packs_builtin import get_builtin_pack
from utils.media_assets import save_attachment_image
from utils.pack_security import hash_pack_password
from utils.character_store import give_style_to_user, nuke_style_globally
from utils.character_registry import merge_pack_payload
from utils.shop_store import (
    list_shop_items,
    upsert_shop_item,
    delete_shop_item,
    get_shop_item,
    normalize_item_id,
    set_limited_style,
)
from utils.shop_character_helpers import build_limited_character_payload
from utils.analytics import (
    utc_day_str,
    METRIC_PULL_5,
    METRIC_PULL_10,
    METRIC_TRIAL_START,
    METRIC_CONVERSION,
)
from utils.backpressure import is_open
from utils.ai_kill import is_disabled as ai_disabled_runtime, disable as ai_disable_runtime, enable as ai_enable_runtime, get_disable_meta
from utils.backpressure import get_redis_or_none

try:
    from sqlalchemy import select, func, delete  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore
    func = None  # type: ignore
    delete = None  # type: ignore
from utils.backpressure import get_redis

logger = logging.getLogger("bot.owner")


# Hidden internal pack used to store "packless" single-character shop items.
# Users never browse/enable this pack; it exists only so custom characters can
# live in the same storage format as normal pack characters.
SHOP_SINGLES_PACK_ID = "system_shop_singles"


async def _ac_shop_item_id(interaction: discord.Interaction, current: str):
    cur = (current or "").lower().strip()
    items = await list_shop_items()
    out: list[app_commands.Choice[str]] = []
    for it in items:
        iid = str(it.get("item_id") or "")
        if not iid:
            continue
        title = str(it.get("title") or it.get("name") or iid)
        name = f"{iid} — {title}"[:100]
        if not cur or cur in iid.lower() or cur in title.lower():
            out.append(app_commands.Choice(name=name, value=iid))
        if len(out) >= 25:
            break
    return out


async def _ac_shop_item_id_all(interaction: discord.Interaction, current: str):
    """Autocomplete for shop item_id including inactive (for deactivate/reactivate)."""
    cur = (current or "").lower().strip()
    items = await list_shop_items(include_inactive=True)
    out: list[app_commands.Choice[str]] = []
    for it in items:
        iid = str(it.get("item_id") or "")
        if not iid:
            continue
        title = str(it.get("title") or it.get("name") or iid)
        active = bool(it.get("active", True))
        suffix = " (inactive)" if not active else ""
        name = f"{iid} — {title}{suffix}"[:100]
        if not cur or cur in iid.lower() or cur in title.lower():
            out.append(app_commands.Choice(name=name, value=iid))
        if len(out) >= 25:
            break
    return out


async def _ac_shop_exclusive_pack_id(interaction: discord.Interaction, current: str):
    """Autocomplete pack_ids for EXCLUSIVE packs.

    Includes:
      - any pack_id referenced by an existing shop pack-roll item
      - any custom pack explicitly flagged as exclusive

    This lets you add characters into an exclusive pack even if you haven't
    created its shop listing yet.
    """
    cur = (current or "").lower().strip()

    # 1) pack-roll items
    seen: set[str] = set()
    choices: list[tuple[str, str]] = []  # (pid, title)
    items = await list_shop_items()
    for it in items:
        if str(it.get("kind") or "") != "pack_roll":
            continue
        pid = normalize_pack_id(str(it.get("pack_id") or ""))
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = str(it.get("title") or pid)
        choices.append((pid, title))

    # 2) custom packs flagged exclusive
    try:
        packs = await list_custom_packs(limit=200)
    except Exception:
        packs = []
    for p in packs or []:
        if not isinstance(p, dict):
            continue
        if not bool(p.get("exclusive")):
            continue
        pid = normalize_pack_id(str(p.get("pack_id") or ""))
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = str(p.get("name") or pid)
        choices.append((pid, title))

    out: list[app_commands.Choice[str]] = []

    # Special "none" option for packless single-character shop items.
    if not cur or cur in {"n", "no", "non", "none", "packless", "single"}:
        out.append(app_commands.Choice(name="(none) — packless direct-buy", value="none"))
    for pid, title in choices:
        name = f"{pid} — {title}"[:100]
        if not cur or cur in pid.lower() or cur in title.lower():
            out.append(app_commands.Choice(name=name, value=pid))
        if len(out) >= 25:
            break
    return out


async def _ac_limited_pack_id(interaction: discord.Interaction, current: str):
    """Autocomplete pack_ids for limited packs (shop_only or exclusive)."""
    cur = (current or "").lower().strip()
    seen: set[str] = set()
    choices: list[tuple[str, str]] = []
    items = await list_shop_items()
    for it in items:
        if str(it.get("kind") or "") != "pack_roll":
            continue
        pid = normalize_pack_id(str(it.get("pack_id") or ""))
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = str(it.get("title") or pid)
        choices.append((pid, title))
    packs = await list_custom_packs(limit=200, include_shop_only=True)
    for p in packs or []:
        if not isinstance(p, dict):
            continue
        is_lim = bool(p.get("exclusive")) or bool(p.get("shop_only"))
        if not is_lim:
            continue
        pid = normalize_pack_id(str(p.get("pack_id") or ""))
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = str(p.get("name") or pid)
        choices.append((pid, title))
    out = []
    for pid, title in choices:
        name = f"{pid} — {title}"[:100]
        if not cur or cur in pid.lower() or cur in title.lower():
            out.append(app_commands.Choice(name=name, value=pid))
        if len(out) >= 25:
            break
    return out


async def _shop_pack_roll_exists(pack_id: str) -> bool:
    pid = normalize_pack_id(pack_id)
    if not pid:
        return False
    items = await list_shop_items()
    for it in items:
        if str(it.get("kind") or "") != "pack_roll":
            continue
        if normalize_pack_id(str(it.get("pack_id") or "")) == pid:
            return True
    return False


async def _get_or_create_shop_singles_pack(owner_user_id: int) -> dict:
    """Create the hidden shop singles pack if it doesn't exist."""
    pid = normalize_pack_id(SHOP_SINGLES_PACK_ID)
    p = await get_custom_pack(pid)
    if p and isinstance(p, dict):
        # Ensure it stays hidden/internal.
        p.setdefault("internal_shop", True)
        p.setdefault("private", True)
        p.setdefault("official", True)
        p.setdefault("exclusive", True)
        return p

    p = {
        "type": "pack",
        "pack_id": pid,
        "name": "_SYSTEM_SHOP_SINGLES",
        "description": "Internal pack for single-character shop items (not rollable / not user-visible).",
        "created_by_guild": 0,
        "created_by_user": int(owner_user_id),
        "characters": [],
        "private": True,
        "nsfw": False,
        "official": True,
        "exclusive": True,
        "internal_shop": True,
    }
    await upsert_custom_pack(p)
    try:
        merge_pack_payload(p)
    except Exception:
        pass
    return p


def _parse_bond_cap(value: str | None) -> int | None:
    v = (value or "").strip().lower()
    if not v or v in {"max", "none"}:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None


def _cache_key(name: str) -> str:
    return f"analytics:cache:{name}"


async def _cache_get(name: str) -> dict | None:
    r = await get_redis_or_none()
    if r is None:
        return None
    try:
        raw = await r.get(_cache_key(name))
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        return json.loads(str(raw))
    except Exception:
        return None


async def _cache_set(name: str, payload: dict, *, ttl_s: int = 900) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.set(_cache_key(name), json.dumps(payload, separators=(",", ":")), ex=int(ttl_s))
    except Exception:
        return


async def _ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        logger.exception("Failed sending owner response")


def _owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_bot_owner(getattr(interaction.user, "id", 0))

    return app_commands.check(predicate)


class SlashOwner(commands.Cog):
    """Owner-only tools for maintenance, support, and Stripe integration."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Keep the owner commands grouped under a "z_" prefix so normal users don't
    # accidentally discover them without scrolling.
    # Split into 3 top-level groups to stay under Discord's 8000-char limit per command.
    owner = app_commands.Group(name="z_owner", description="Owner-only tools")
    premium = app_commands.Group(name="premium", description="Premium entitlements", parent=owner)
    ai = app_commands.Group(name="ai", description="AI kill switch", parent=owner)
    data = app_commands.Group(name="data", description="Delete stored data", parent=owner)
    points = app_commands.Group(name="points", description="Points tools", parent=owner)

    # Pack/shop admin commands (split out to avoid size limit)
    packs_top = app_commands.Group(name="z_packs", description="Owner pack & shop admin")
    packadmin = app_commands.Group(name="packadmin", description="Pack admin", parent=packs_top)
    shop = app_commands.Group(name="shop", description="Shop items", parent=packs_top)

    # Analytics & global stats (split out to avoid size limit)
    stats_top = app_commands.Group(name="z_stats", description="Owner analytics & global stats")
    analytics = app_commands.Group(name="analytics", description="Analytics", parent=stats_top)
    global_ = app_commands.Group(name="global", description="Global stats", parent=stats_top)

    # ----------------------------
    # Character owner utilities
    # ----------------------------

    @owner.command(name="give_character", description="Grant a character to a user by character id")
    async def give_character(
        self,
        interaction: discord.Interaction,
        character_id: str,
        user: discord.Member | None = None,
    ):
        # Owner-only guard (fix: _owner_only is a decorator factory, not a callable)
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        target = user or interaction.user
        ok, msg = await give_style_to_user(int(target.id), character_id)
        prefix = "✅" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)

    @owner.command(name="delete_character", description="Remove a character by id")
    async def delete_character(self, interaction: discord.Interaction, character_id: str):
        # Owner-only guard (fix: _owner_only is a decorator factory, not a callable)
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        stats = await nuke_style_globally(character_id)
        sid = stats.get("style_id")
        if stats.get("ok"):
            await interaction.response.send_message(
                f"✅ Nuked `{sid}`. Removed from inventories: {stats.get('owned_removed', 0)}, removed from packs: {stats.get('packs_touched', 0)}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(f"❌ {stats.get('reason')}", ephemeral=True)

    # ----------------------------
    # Data deletion helpers
    # ----------------------------

    def _days_to_day_strs(self, days: int) -> list[str]:
        """Return a list of UTC day strings (YYYYMMDD) for the last N days incl today."""
        n = max(0, int(days or 0))
        if n <= 0:
            return []
        out: list[str] = []
        now = int(time.time())
        for i in range(n):
            out.append(utc_day_str(now - i * 86400))
        return out

    async def _clear_analytics_redis(self, *, guild_id: int | None, days: int) -> None:
        """Best-effort delete analytics Redis keys.

        If days==0, SCAN-delete analytics:* (global) or analytics:*:<guild> patterns.
        If days>0, delete the known key names for today..N days ago.
        """
        r = await get_redis_or_none()
        if r is None:
            return

        # Known metrics list
        from utils.analytics import (
            METRIC_DAILY_ROLLS,
            METRIC_DAILY_TALK_CALLS,
            METRIC_DAILY_SCENE_CALLS,
            METRIC_DAILY_AI_CALLS,
            METRIC_DAILY_AI_TOKEN_BUDGET,
        )

        metrics = [
            METRIC_DAILY_ROLLS,
            METRIC_DAILY_TALK_CALLS,
            METRIC_DAILY_SCENE_CALLS,
            METRIC_DAILY_AI_CALLS,
            METRIC_DAILY_AI_TOKEN_BUDGET,
        ]

        if int(days or 0) > 0 and guild_id is not None:
            # Precise delete for a guild in a day range
            for day in self._days_to_day_strs(int(days)):
                keys = [
                    f"analytics:active:{day}:{int(guild_id)}",
                    f"analytics:seen:{int(guild_id)}",
                ]
                for m in metrics:
                    keys.append(f"analytics:count:{day}:{int(guild_id)}:{m}")
                try:
                    await r.delete(*keys)
                except Exception:
                    pass
                try:
                    # Also clear dirty queue entries (safe even if absent)
                    await r.srem(f"analytics:dirty:{day}", str(int(guild_id)))
                except Exception:
                    pass
            return

        # Fallback: SCAN delete
        pattern = "analytics:*" if guild_id is None else f"analytics:*:{int(guild_id)}*"
        try:
            # Use SCAN to avoid blocking Redis.
            cursor = 0
            while True:
                cursor, batch = await r.scan(cursor=cursor, match=pattern, count=500)
                if batch:
                    try:
                        await r.delete(*batch)
                    except Exception:
                        pass
                if int(cursor) == 0:
                    break
        except Exception:
            return

    async def _clear_incidents_redis(self) -> None:
        r = await get_redis_or_none()
        if r is None:
            return
        try:
            await r.delete("incidents:global", "incidents:global:seq")
        except Exception:
            return

    async def _wipe_all_redis_data(self) -> int:
        """Delete all Redis keys used by analytics, leaderboard, incidents, guild settings, and leaderboard reset.
        Returns total keys deleted. Use for full reset so no data clashes.
        """
        r = await get_redis_or_none()
        if r is None:
            return 0
        patterns = [
            "analytics:*",
            "leaderboard:*",
            "leaderboard_reset:*",
            "incidents:*",
            "guild:*",
        ]
        deleted = 0
        try:
            for pattern in patterns:
                cursor = 0
                for _ in range(1000):
                    cursor, keys = await r.scan(cursor, match=pattern, count=200)
                    if keys:
                        await r.delete(*keys)
                        deleted += len(keys)
                    if cursor == 0:
                        break
        except Exception:
            logger.exception("_wipe_all_redis_data failed")
        return deleted

    # ----------------------------
    # /owner status
    # ----------------------------

    @owner.command(name="status", description="Bot status overview")
    @_owner_only()
    async def owner_status(self, interaction: discord.Interaction):
        # Uptime
        started = float(getattr(self.bot, "start_time", 0.0) or 0.0)
        uptime_s = int(max(0.0, time.time() - started)) if started > 0 else 0
        uptime = f"{uptime_s//3600}h {(uptime_s%3600)//60}m" if uptime_s else "unknown"

        # AI breaker
        breaker_rem = int(await is_open() or 0)
        breaker = f"OPEN ({breaker_rem}s)" if breaker_rem > 0 else "closed"

        runtime_disabled = bool(await ai_disabled_runtime())
        disabled_at, disabled_reason, disabled_ttl = await get_disable_meta()
        meta_line = ""
        if disabled_reason:
            meta_line = f"\nLast disable: `{disabled_reason}`"

        msg = (
            f"✅ Online as **{self.bot.user}**\n"
            f"Guilds: **{len(self.bot.guilds)}** • Shards: **{getattr(self.bot, 'shard_count', 'auto')}**\n"
            f"Latency: **{int(getattr(self.bot, 'latency', 0.0) * 1000)}ms** • Uptime: **{uptime}**\n\n"
            f"AI_DISABLED (env): **{bool(getattr(__import__('config'), 'AI_DISABLED', False))}**\n"
            f"AI_DISABLED (runtime): **{runtime_disabled}**{meta_line}\n"
            f"Circuit breaker: **{breaker}**"
        )
        await _ephemeral(interaction, msg)

    # ----------------------------
    # /owner packs ...
    # ----------------------------

    @packadmin.command(name="set_official", description="Mark pack official/community")
    @_owner_only()
    @app_commands.describe(pack_id="Pack id", official="true=OFFICIAL, false=COMMUNITY")
    async def owner_packs_set_official(self, interaction: discord.Interaction, pack_id: str, official: bool):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "Built-in packs are always OFFICIAL.")
            return
        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found (custom packs only).")
            return
        if not isinstance(p, dict):
            await _ephemeral(interaction, "Pack payload invalid.")
            return
        p["official"] = bool(official)
        ok = await upsert_custom_pack(p)
        await _ephemeral(interaction, "✅ Updated." if ok else "⚠️ Update failed.")

    @packadmin.command(name="set_exclusive", description="Mark pack exclusive or clear")
    @_owner_only()
    @app_commands.describe(pack_id="Pack id", exclusive="true=EXCLUSIVE, false=not exclusive")
    async def owner_packs_set_exclusive(self, interaction: discord.Interaction, pack_id: str, exclusive: bool):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "Built-in packs can't be marked exclusive.")
            return
        p = await get_custom_pack(pid)
        if not p or not isinstance(p, dict):
            await _ephemeral(interaction, "Pack not found (custom packs only).")
            return
        p["exclusive"] = bool(exclusive)
        # Exclusive packs are effectively official.
        if bool(exclusive):
            p["official"] = True
        ok = await upsert_custom_pack(p)
        await _ephemeral(interaction, "✅ Updated." if ok else "⚠️ Update failed.")

    @packadmin.command(name="delete_pack", description="Delete a custom pack by id")
    @_owner_only()
    @app_commands.describe(pack_id="Pack id to delete (custom packs only; built-in cannot be deleted)")
    async def owner_packadmin_delete_pack(self, interaction: discord.Interaction, pack_id: str):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "Built-in packs cannot be deleted.")
            return
        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found (custom packs only).")
            return
        ok = await delete_custom_pack(pid)
        await _ephemeral(
            interaction,
            f"✅ Pack `{pid}` deleted." if ok else "⚠️ Delete failed.",
        )

    @packadmin.command(name="set_character_exclusive", description="Mark character exclusive/limited")
    @_owner_only()
    @app_commands.describe(style_id="Character id", exclusive="true=EXCLUSIVE/LIMITED, false=clear")
    async def owner_character_set_exclusive(self, interaction: discord.Interaction, style_id: str, exclusive: bool):
        sid = normalize_style_id(style_id)
        if not sid:
            await _ephemeral(interaction, "Invalid style id.")
            return
        ok = await set_limited_style(sid, bool(exclusive))
        await _ephemeral(interaction, "✅ Updated." if ok else "⚠️ Update failed.")

    @packadmin.command(name="set_character_official", description="Mark character official/community")
    @_owner_only()
    @app_commands.describe(character_id="Character id", official="true=OFFICIAL, false=COMMUNITY")
    async def owner_character_set_official(self, interaction: discord.Interaction, character_id: str, official: bool):
        cid = normalize_style_id(character_id)
        if not cid:
            await _ephemeral(interaction, "Invalid character id.")
            return

        # Search all custom packs for this character
        packs = await list_custom_packs(limit=500, include_shop_only=True)
        found_pack = None
        found_char = None
        found_idx = -1
        for pk in packs:
            if not isinstance(pk, dict):
                continue
            chars = pk.get("characters")
            if not isinstance(chars, list):
                continue
            for i, c in enumerate(chars):
                if isinstance(c, dict) and str(c.get("id") or "").strip().lower() == cid:
                    found_pack = pk
                    found_char = c
                    found_idx = i
                    break
            if found_pack:
                break

        if not found_pack or found_char is None:
            await _ephemeral(interaction, f"⚠️ Character `{cid}` not found in any custom pack.")
            return

        found_char["official"] = bool(official)
        found_pack["characters"][found_idx] = found_char
        ok = await upsert_custom_pack(found_pack)
        try:
            merge_pack_payload(found_pack)
        except Exception:
            pass

        label = "OFFICIAL" if official else "COMMUNITY"
        await _ephemeral(interaction, f"✅ Character `{cid}` marked as **{label}**." if ok else "⚠️ Update failed.")

    @packadmin.command(name="featured_add", description="Add pack to featured list")
    @_owner_only()
    @app_commands.describe(pack_id="Pack id to feature")
    async def owner_packadmin_featured_add(self, interaction: discord.Interaction, pack_id: str):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        ok = await add_featured_pack(pid)
        await _ephemeral(interaction, f"✅ Added `{pid}` to featured." if ok else "⚠️ Failed.")

    @packadmin.command(name="featured_remove", description="Remove a pack from featured")
    @_owner_only()
    @app_commands.describe(pack_id="Pack id to unfeature")
    async def owner_packadmin_featured_remove(self, interaction: discord.Interaction, pack_id: str):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        ok = await remove_featured_pack(pid)
        await _ephemeral(interaction, f"✅ Removed `{pid}` from featured." if ok else "⚠️ Failed.")

    @packadmin.command(name="featured_list", description="List currently featured packs")
    @_owner_only()
    async def owner_packadmin_featured_list(self, interaction: discord.Interaction):
        ids = await get_featured_pack_ids()
        if not ids:
            await _ephemeral(interaction, "No featured packs.")
            return
        lines = [f"• `{pid}`" for pid in sorted(ids)]
        await _ephemeral(interaction, "**Featured packs:**\n" + "\n".join(lines))

    # ----------------------------
    # /owner packadmin shop_* (convenience wrappers)
    # ----------------------------

    @packadmin.command(name="shop_pack_create", description="Create/update an exclusive pack-roll shop item")
    @_owner_only()
    @app_commands.describe(
        item_id="Shop item id (short)",
        pack_id="Custom pack id to roll from",
        pack_name="Pack display name",
        pack_description="Pack description",
        cost="Points per roll",
        shop_title="Title shown in /points shop",
        shop_description="Description shown in /points shop",
        private="Hide pack from browse/enable",
        password="(Optional) Password for private packs",
        image="(Optional) Pack image for shop display",
        image_url="(Optional) Direct https image URL for pack",
        button_label="Button label (optional)",
        button_emoji="Button emoji (optional)",
    )
    async def owner_shop_pack_create(
        self,
        interaction: discord.Interaction,
        item_id: str,
        pack_id: str,
        pack_name: str,
        pack_description: str,
        cost: int,
        shop_title: str,
        shop_description: str,
        private: bool = False,
        password: str | None = None,
        image: discord.Attachment | None = None,
        image_url: str | None = None,
        button_label: str | None = None,
        button_emoji: str | None = None,
    ):
        iid = normalize_item_id(item_id)
        pid = normalize_pack_id(pack_id)
        if not iid or not pid:
            await _ephemeral(interaction, "Invalid item_id or pack_id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "Use a custom pack id (built-ins are always available).")
            return

        p = await get_custom_pack(pid)
        if not p or not isinstance(p, dict):
            p = {
                "type": "pack",
                "pack_id": pid,
                "name": str(pack_name or pid)[:64],
                "description": str(pack_description or "").strip()[:800],
                "created_by_guild": 0,
                "created_by_user": int(interaction.user.id),
                "characters": [],
                "private": bool(private),
                "nsfw": False,
            }
        else:
            if isinstance(pack_name, str) and pack_name.strip():
                p["name"] = pack_name.strip()[:64]
            if isinstance(pack_description, str):
                p["description"] = pack_description.strip()[:800]
            p["private"] = bool(private)
            p["nsfw"] = False

        # Password handling for private packs
        if isinstance(password, str) and password.strip():
            pw_salt, pw_hash = hash_pack_password(password.strip())
            p["password_salt"] = pw_salt
            p["password_hash"] = pw_hash

        if image is not None:
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=image,
                rel_dir=f"packs/{pid}",
                basename="pack",
                max_bytes=20 * 1024 * 1024,
                upscale_min_px=1024,
            )
            if ok_img and rel:
                p["image_url"] = rel if str(rel).startswith("http") else f"asset:{rel}"
            elif not ok_img and msg_img:
                await _ephemeral(interaction, f"⚠️ Pack image: {msg_img}")
                return
        elif isinstance(image_url, str) and image_url.strip().lower().startswith("http"):
            p["image_url"] = image_url.strip()

        # Limited packs are official.
        p["official"] = True
        p["exclusive"] = True

        ok_pack = await upsert_custom_pack(p)
        try:
            merge_pack_payload(p)
        except Exception:
            pass

        payload = {
            "item_id": iid,
            "kind": "pack_roll",
            "pack_id": pid,
            "title": str(shop_title or pid)[:64],
            "description": str(shop_description or "").strip()[:800],
            "cost": int(cost or 0),
            "button_label": str(button_label or "")[:24],
            "button_emoji": (str(button_emoji).strip() if button_emoji else ""),
            "active": True,
        }
        ok_item = await upsert_shop_item(payload)
        await _ephemeral(interaction, "✅ Created." if (ok_pack and ok_item) else "⚠️ Failed (check logs / Redis).")

    @packadmin.command(
        name="limited_pack_create",
        description="Create a limited pack for shop rolls only",
    )
    @_owner_only()
    @app_commands.describe(
        item_id="Shop item id (short)",
        pack_id="Custom pack id to roll from",
        pack_name="Pack display name",
        pack_description="Pack description",
        cost="Points per roll",
        shop_title="Title shown in /points shop",
        shop_description="Description shown in /points shop",
        private="Hide pack from browse/enable",
        password="(Optional) Password for private packs",
        image="(Optional) Pack image for shop display",
        image_url="(Optional) Direct https image URL for pack",
        button_label="Button label (optional)",
        button_emoji="Button emoji (optional)",
    )
    async def owner_limited_pack_create(
        self,
        interaction: discord.Interaction,
        item_id: str,
        pack_id: str,
        pack_name: str,
        pack_description: str,
        cost: int,
        shop_title: str,
        shop_description: str,
        private: bool = False,
        password: str | None = None,
        image: discord.Attachment | None = None,
        image_url: str | None = None,
        button_label: str | None = None,
        button_emoji: str | None = None,
    ):
        iid = normalize_item_id(item_id)
        pid = normalize_pack_id(pack_id)
        if not iid or not pid:
            await _ephemeral(interaction, "Invalid item_id or pack_id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "Use a custom pack id (built-ins are always available).")
            return

        p = await get_custom_pack(pid)
        if not p or not isinstance(p, dict):
            p = {
                "type": "pack",
                "pack_id": pid,
                "name": str(pack_name or pid)[:64],
                "description": str(pack_description or "").strip()[:800],
                "created_by_guild": 0,
                "created_by_user": int(interaction.user.id),
                "characters": [],
                "private": bool(private),
                "nsfw": False,
            }
        else:
            if isinstance(pack_name, str) and pack_name.strip():
                p["name"] = pack_name.strip()[:64]
            if isinstance(pack_description, str):
                p["description"] = pack_description.strip()[:800]
            p["private"] = bool(private)
            p["nsfw"] = False

        # Password handling for private packs
        if isinstance(password, str) and password.strip():
            pw_salt, pw_hash = hash_pack_password(password.strip())
            p["password_salt"] = pw_salt
            p["password_hash"] = pw_hash

        if image is not None:
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=image,
                rel_dir=f"packs/{pid}",
                basename="pack",
                max_bytes=20 * 1024 * 1024,
                upscale_min_px=1024,
            )
            if ok_img and rel:
                p["image_url"] = rel if str(rel).startswith("http") else f"asset:{rel}"
            elif not ok_img and msg_img:
                await _ephemeral(interaction, f"⚠️ Pack image: {msg_img}")
                return
        elif isinstance(image_url, str) and image_url.strip().lower().startswith("http"):
            p["image_url"] = image_url.strip()

        p["official"] = True
        p["exclusive"] = True
        p["shop_only"] = True

        ok_pack = await upsert_custom_pack(p)
        try:
            merge_pack_payload(p)
        except Exception:
            pass

        payload = {
            "item_id": iid,
            "kind": "pack_roll",
            "pack_id": pid,
            "title": str(shop_title or pid)[:64],
            "description": str(shop_description or "").strip()[:800],
            "cost": int(cost or 0),
            "button_label": str(button_label or "")[:24],
            "button_emoji": (str(button_emoji).strip() if button_emoji else ""),
            "active": True,
        }
        ok_item = await upsert_shop_item(payload)
        await _ephemeral(interaction, "✅ Created." if (ok_pack and ok_item) else "⚠️ Failed (check logs / Redis).")

    @packadmin.command(
        name="limited_character_add",
        description="Add/update a character in a limited pack",
    )
    @_owner_only()
    @app_commands.describe(
        pack_id="Limited pack to add the character to",
        character_id="Unique character id",
        display_name="Display name",
        description="Short description",
        prompt="Personality/system prompt",
        rarity="common/uncommon/rare/legendary/mythic (default: rare)",
        image="(Optional) Upload an image/GIF",
        emotion_neutral="(Optional) Upload a NEUTRAL emotion image",
        emotion_happy="(Optional) Upload a HAPPY emotion image",
        emotion_sad="(Optional) Upload a SAD emotion image",
        emotion_mad="(Optional) Upload a MAD/ANGRY emotion image",
        emotion_confused="(Optional) Upload a CONFUSED emotion image",
        emotion_excited="(Optional) Upload an EXCITED emotion image",
        emotion_affectionate="(Optional) Upload an AFFECTIONATE emotion image",
        bond1="(Optional) Upload bond image tier 1 (Friend)",
        bond2="(Optional) Upload bond image tier 2 (Trusted)",
        bond3="(Optional) Upload bond image tier 3 (Close Companion)",
        bond4="(Optional) Upload bond image tier 4 (Devoted)",
        bond5="(Optional) Upload bond image tier 5 (Soulbound)",
        image_url="(Optional) Direct https image/gif URL",
        traits="Optional comma-separated tags/traits",
        max_bond_cap="Optional max bond cap",
    )
    @app_commands.autocomplete(pack_id=_ac_limited_pack_id)
    async def owner_limited_character_add(
        self,
        interaction: discord.Interaction,
        pack_id: str,
        character_id: str,
        display_name: str,
        description: str,
        prompt: str,
        rarity: str = "rare",
        image: discord.Attachment | None = None,
        emotion_neutral: discord.Attachment | None = None,
        emotion_happy: discord.Attachment | None = None,
        emotion_sad: discord.Attachment | None = None,
        emotion_mad: discord.Attachment | None = None,
        emotion_confused: discord.Attachment | None = None,
        emotion_excited: discord.Attachment | None = None,
        emotion_affectionate: discord.Attachment | None = None,
        bond1: discord.Attachment | None = None,
        bond2: discord.Attachment | None = None,
        bond3: discord.Attachment | None = None,
        bond4: discord.Attachment | None = None,
        bond5: discord.Attachment | None = None,
        image_url: str | None = None,
        traits: str | None = None,
        max_bond_cap: str | None = None,
    ):
        # Defer immediately: image attachments take time to download/save
        await interaction.response.defer(ephemeral=True)

        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack_id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "Use a limited (custom) pack id.")
            return
        p = await get_custom_pack(pid)
        if not p or not isinstance(p, dict):
            await _ephemeral(interaction, "Pack not found. Create it with /owner packadmin limited_pack_create first.")
            return
        is_limited = bool(p.get("exclusive")) or bool(p.get("shop_only"))
        if not is_limited:
            await _ephemeral(interaction, "Pack must be limited (shop_only) or exclusive.")
            return

        char_payload, _, err = await build_limited_character_payload(
            character_id=character_id,
            display_name=display_name,
            description=description,
            prompt=prompt,
            rarity=rarity,
            pack_id=pid,
            image=image,
            image_url=image_url,
            traits=traits,
            max_bond_cap=max_bond_cap,
            rollable=True,
            emotion_neutral=emotion_neutral,
            emotion_happy=emotion_happy,
            emotion_sad=emotion_sad,
            emotion_mad=emotion_mad,
            emotion_confused=emotion_confused,
            emotion_excited=emotion_excited,
            emotion_affectionate=emotion_affectionate,
            bond1=bond1,
            bond2=bond2,
            bond3=bond3,
            bond4=bond4,
            bond5=bond5,
        )
        if err:
            await _ephemeral(interaction, f"⚠️ {err}")
            return

        cid = char_payload.get("id") or normalize_style_id(character_id)
        chars = p.get("characters")
        if not isinstance(chars, list):
            chars = []
        replaced = False
        for i, c in enumerate(chars):
            if isinstance(c, dict) and str(c.get("id") or "").strip().lower() == cid:
                chars[i] = char_payload
                replaced = True
                break
        if not replaced:
            chars.append(char_payload)
        p["characters"] = chars

        ok_pack = await upsert_custom_pack(p)
        try:
            merge_pack_payload(p)
        except Exception:
            pass
        ok_limited = await set_limited_style(cid, True)
        await _ephemeral(
            interaction,
            "✅ Character added/updated." if (ok_pack and ok_limited) else "⚠️ Failed (check logs / Redis).",
        )

    @packadmin.command(
        name="limited_character_create_direct",
        description="Create a limited direct-buy character",
    )
    @_owner_only()
    @app_commands.describe(
        item_id="Shop item id (short)",
        character_id="Unique character id",
        display_name="Display name",
        description="Short description",
        prompt="Personality/system prompt",
        cost="Points cost",
        rarity="common/uncommon/rare/legendary/mythic (default: rare)",
        shop_title="Title shown in shop (optional)",
        shop_description="Description shown in shop (optional)",
        image="(Optional) Upload an image/GIF",
        emotion_neutral="(Optional) Upload a NEUTRAL emotion image",
        emotion_happy="(Optional) Upload a HAPPY emotion image",
        emotion_sad="(Optional) Upload a SAD emotion image",
        emotion_mad="(Optional) Upload a MAD/ANGRY emotion image",
        emotion_confused="(Optional) Upload a CONFUSED emotion image",
        emotion_excited="(Optional) Upload an EXCITED emotion image",
        emotion_affectionate="(Optional) Upload an AFFECTIONATE emotion image",
        bond1="(Optional) Upload bond image tier 1 (Friend)",
        bond2="(Optional) Upload bond image tier 2 (Trusted)",
        bond3="(Optional) Upload bond image tier 3 (Close Companion)",
        bond4="(Optional) Upload bond image tier 4 (Devoted)",
        bond5="(Optional) Upload bond image tier 5 (Soulbound)",
        traits="Optional comma-separated tags/traits",
        max_bond_cap="Optional max bond cap",
        button_label="Button label (optional)",
    )
    async def owner_limited_character_create_direct(
        self,
        interaction: discord.Interaction,
        item_id: str,
        character_id: str,
        display_name: str,
        description: str,
        prompt: str,
        cost: int,
        rarity: str = "rare",
        shop_title: str | None = None,
        shop_description: str | None = None,
        image: discord.Attachment | None = None,
        emotion_neutral: discord.Attachment | None = None,
        emotion_happy: discord.Attachment | None = None,
        emotion_sad: discord.Attachment | None = None,
        emotion_mad: discord.Attachment | None = None,
        emotion_confused: discord.Attachment | None = None,
        emotion_excited: discord.Attachment | None = None,
        emotion_affectionate: discord.Attachment | None = None,
        bond1: discord.Attachment | None = None,
        bond2: discord.Attachment | None = None,
        bond3: discord.Attachment | None = None,
        bond4: discord.Attachment | None = None,
        bond5: discord.Attachment | None = None,
        traits: str | None = None,
        max_bond_cap: str | None = None,
        button_label: str | None = None,
    ):
        # Defer immediately: image attachments take time to download/save
        await interaction.response.defer(ephemeral=True)

        iid = normalize_item_id(item_id)
        if not iid:
            await _ephemeral(interaction, "Invalid item_id.")
            return

        p = await _get_or_create_shop_singles_pack(int(interaction.user.id))
        pid = normalize_pack_id(SHOP_SINGLES_PACK_ID)

        char_payload, _, err = await build_limited_character_payload(
            character_id=character_id,
            display_name=display_name,
            description=description,
            prompt=prompt,
            rarity=rarity,
            pack_id=pid,
            image=image,
            image_url=None,
            traits=traits,
            max_bond_cap=max_bond_cap,
            rollable=False,
            emotion_neutral=emotion_neutral,
            emotion_happy=emotion_happy,
            emotion_sad=emotion_sad,
            emotion_mad=emotion_mad,
            emotion_confused=emotion_confused,
            emotion_excited=emotion_excited,
            emotion_affectionate=emotion_affectionate,
            bond1=bond1,
            bond2=bond2,
            bond3=bond3,
            bond4=bond4,
            bond5=bond5,
        )
        if err:
            await _ephemeral(interaction, f"⚠️ {err}")
            return

        cid = char_payload.get("id") or normalize_style_id(character_id)
        chars = p.get("characters")
        if not isinstance(chars, list):
            chars = []
        replaced = False
        for i, c in enumerate(chars):
            if isinstance(c, dict) and str(c.get("id") or "").strip().lower() == cid:
                chars[i] = char_payload
                replaced = True
                break
        if not replaced:
            chars.append(char_payload)
        p["characters"] = chars

        ok_pack = await upsert_custom_pack(p)
        try:
            merge_pack_payload(p)
        except Exception:
            pass
        ok_limited = await set_limited_style(cid, True)

        item_payload = {
            "item_id": iid,
            "kind": "character_grant",
            "style_id": cid,
            "title": str((shop_title or display_name or cid))[:64],
            "description": str((shop_description or description or "")).strip()[:800],
            "cost": int(cost or 0),
            "button_label": str(button_label or "")[:24],
            "button_emoji": "",
            "active": True,
            "exclusive": True,
        }
        ok_item = await upsert_shop_item(item_payload)

        await _ephemeral(
            interaction,
            "✅ Created." if (ok_pack and ok_item and ok_limited) else "⚠️ Failed (check logs / Redis).",
        )

    @packadmin.command(
        name="character_edit",
        description="Edit a shop character",
    )
    @_owner_only()
    @app_commands.describe(
        item_id="Shop item id (autocomplete)",
        character_id="(Optional) Character id -- required for pack_roll items, auto-detected for direct-buy",
        display_name="(Optional) New display name",
        description="(Optional) New character description",
        prompt="(Optional) New personality / system prompt",
        rarity="(Optional) New rarity: common/uncommon/rare/legendary/mythic",
        traits="(Optional) New comma-separated tags/traits (replaces existing)",
        max_bond_cap="(Optional) New max bond level",
        image="(Optional) New main image",
        emotion_neutral="(Optional) New NEUTRAL emotion image",
        emotion_happy="(Optional) New HAPPY emotion image",
        emotion_sad="(Optional) New SAD emotion image",
        emotion_mad="(Optional) New MAD/ANGRY emotion image",
        emotion_confused="(Optional) New CONFUSED emotion image",
        emotion_excited="(Optional) New EXCITED emotion image",
        emotion_affectionate="(Optional) New AFFECTIONATE emotion image",
        bond1="(Optional) New bond image tier 1 (Friend)",
        bond2="(Optional) New bond image tier 2 (Trusted)",
        bond3="(Optional) New bond image tier 3 (Close Companion)",
        bond4="(Optional) New bond image tier 4 (Devoted)",
        bond5="(Optional) New bond image tier 5 (Soulbound)",
        shop_title="(Optional) New shop item title",
        shop_description="(Optional) New shop item description",
        cost="(Optional) New shop item cost",
    )
    @app_commands.autocomplete(item_id=_ac_shop_item_id)
    async def owner_packadmin_character_edit(
        self,
        interaction: discord.Interaction,
        item_id: str,
        character_id: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        prompt: str | None = None,
        rarity: str | None = None,
        traits: str | None = None,
        max_bond_cap: str | None = None,
        image: discord.Attachment | None = None,
        emotion_neutral: discord.Attachment | None = None,
        emotion_happy: discord.Attachment | None = None,
        emotion_sad: discord.Attachment | None = None,
        emotion_mad: discord.Attachment | None = None,
        emotion_confused: discord.Attachment | None = None,
        emotion_excited: discord.Attachment | None = None,
        emotion_affectionate: discord.Attachment | None = None,
        bond1: discord.Attachment | None = None,
        bond2: discord.Attachment | None = None,
        bond3: discord.Attachment | None = None,
        bond4: discord.Attachment | None = None,
        bond5: discord.Attachment | None = None,
        shop_title: str | None = None,
        shop_description: str | None = None,
        cost: int | None = None,
    ):
        from utils.character_registry import RARITY_ORDER

        iid = normalize_item_id(item_id)
        it = await get_shop_item(iid)
        if not it or not isinstance(it, dict):
            await _ephemeral(interaction, "Shop item not found.")
            return

        kind = str(it.get("kind") or "")

        # Resolve pack and character id
        if kind == "character_grant":
            cid = normalize_style_id(str(it.get("style_id") or ""))
            pid = normalize_pack_id(SHOP_SINGLES_PACK_ID)
            if character_id:
                cid = normalize_style_id(character_id) or cid
        elif kind == "pack_roll":
            pid = normalize_pack_id(str(it.get("pack_id") or ""))
            if not character_id:
                await _ephemeral(interaction, "⚠️ For pack_roll items, you must provide `character_id` to identify which character to edit.")
                return
            cid = normalize_style_id(character_id)
        else:
            await _ephemeral(interaction, f"⚠️ Unsupported shop item kind: `{kind}`.")
            return

        if not cid:
            await _ephemeral(interaction, "⚠️ Could not determine character id.")
            return
        if not pid:
            await _ephemeral(interaction, "⚠️ Could not determine pack id.")
            return

        # Load the pack
        p = await get_custom_pack(pid)
        if not p or not isinstance(p, dict):
            await _ephemeral(interaction, "⚠️ Underlying pack not found in Redis.")
            return

        # Find the character in the pack
        chars = p.get("characters")
        if not isinstance(chars, list):
            chars = []
        char_dict = None
        char_idx = -1
        for i, c in enumerate(chars):
            if isinstance(c, dict) and str(c.get("id") or "").strip().lower() == cid:
                char_dict = c
                char_idx = i
                break
        if char_dict is None:
            await _ephemeral(interaction, f"⚠️ Character `{cid}` not found in pack `{pid}`.")
            return

        await interaction.response.defer(ephemeral=True)

        # Validate rarity if provided
        if rarity is not None:
            rar = rarity.strip().lower()
            if rar not in set(RARITY_ORDER):
                await interaction.followup.send(f"⚠️ Invalid rarity: `{rar}`. Use: {', '.join(RARITY_ORDER)}", ephemeral=True)
                return
            char_dict["rarity"] = rar

        # Update simple text fields
        if display_name is not None:
            char_dict["display_name"] = str(display_name).strip()[:64]
        if description is not None:
            char_dict["description"] = str(description).strip()[:400]
        if prompt is not None:
            char_dict["prompt"] = str(prompt).strip()[:6000]
        if traits is not None:
            char_dict["tags"] = [t.strip() for t in traits.split(",") if t.strip()]
        if max_bond_cap is not None:
            from utils.shop_character_helpers import _parse_bond_cap
            char_dict["max_bond_level"] = _parse_bond_cap(max_bond_cap)

        # Helper to save an image attachment
        async def _save_img(att: discord.Attachment, rel_dir: str, basename: str) -> tuple[str | None, str | None]:
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=att,
                rel_dir=rel_dir,
                basename=basename,
                max_bytes=20 * 1024 * 1024,
                upscale_min_px=1024,
            )
            if not ok_img:
                return None, msg_img or "Image save failed."
            if not rel:
                return None, None
            return (rel if rel.startswith("http") else f"asset:{rel}"), None

        # Main image
        if image is not None:
            ref, err = await _save_img(image, f"packs/{pid}", cid)
            if err:
                await interaction.followup.send(f"⚠️ Main image: {err}", ephemeral=True)
                return
            if ref:
                char_dict["image_url"] = ref

        # Emotion images -- merge into existing dict
        emotion_inputs: dict[str, discord.Attachment | None] = {
            "neutral": emotion_neutral,
            "happy": emotion_happy,
            "sad": emotion_sad,
            "mad": emotion_mad,
            "confused": emotion_confused,
            "excited": emotion_excited,
            "affectionate": emotion_affectionate,
        }
        existing_emotions = char_dict.get("emotion_images") or {}
        if not isinstance(existing_emotions, dict):
            existing_emotions = {}
        emotions_changed = False
        for key, att in emotion_inputs.items():
            if att is None:
                continue
            ref, err = await _save_img(att, f"packs/{pid}/{cid}/emotions", key)
            if err:
                await interaction.followup.send(f"⚠️ Emotion image ({key}): {err}", ephemeral=True)
                return
            if ref:
                existing_emotions[key] = ref
                emotions_changed = True
        if emotions_changed:
            char_dict["emotion_images"] = existing_emotions

        # Bond images -- merge into existing list
        bond_atts = [bond1, bond2, bond3, bond4, bond5]
        existing_bonds = char_dict.get("bond_images") or []
        if not isinstance(existing_bonds, list):
            existing_bonds = []
        bonds_changed = False
        for idx, att in enumerate(bond_atts, start=1):
            if att is None:
                continue
            ref, err = await _save_img(att, f"packs/{pid}/{cid}/bonds", f"bond{idx}")
            if err:
                await interaction.followup.send(f"⚠️ Bond image (tier {idx}): {err}", ephemeral=True)
                return
            if ref:
                while len(existing_bonds) < idx:
                    existing_bonds.append("")
                existing_bonds[idx - 1] = ref
                bonds_changed = True
        if bonds_changed:
            # Clean trailing empty entries
            while existing_bonds and not existing_bonds[-1]:
                existing_bonds.pop()
            char_dict["bond_images"] = existing_bonds

        # Write character back into the pack
        chars[char_idx] = char_dict
        p["characters"] = chars
        ok_pack = await upsert_custom_pack(p)
        try:
            merge_pack_payload(p)
        except Exception:
            pass

        # Update shop item metadata if provided
        shop_changed = False
        if shop_title is not None and shop_title.strip():
            it["title"] = shop_title.strip()[:64]
            shop_changed = True
        if shop_description is not None:
            d = shop_description.strip()
            if d:
                it["description"] = d[:800]
                shop_changed = True
        if cost is not None:
            it["cost"] = max(0, int(cost))
            shop_changed = True

        ok_item = True
        if shop_changed:
            ok_item = await upsert_shop_item(it)

        if ok_pack and ok_item:
            await interaction.followup.send(f"✅ Character `{cid}` updated.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Partial failure (check logs).", ephemeral=True)

    # ----------------------------
    # /owner shop ...
    # ----------------------------

    @shop.command(name="list", description="List shop items (optionally include deactivated)")
    @_owner_only()
    @app_commands.describe(show_inactive="Include deactivated items in the list")
    async def owner_shop_list(
        self, interaction: discord.Interaction, show_inactive: bool = False
    ):
        items = await list_shop_items(include_inactive=show_inactive)
        if not items:
            await _ephemeral(interaction, "(none)")
            return
        lines = []
        for it in items[:40]:
            kind = str(it.get("kind") or "")
            title = str(it.get("title") or it.get("name") or it.get("item_id"))
            cost = int(it.get("cost") or 0)
            extra = ""
            if kind == "pack_roll":
                extra = f" pack=`{it.get('pack_id')}`"
            elif kind == "character_grant":
                extra = f" char=`{it.get('style_id')}`"
            active = bool(it.get("active", True))
            if not active:
                extra = " **[inactive]**" + extra
            lines.append(f"• `{it.get('item_id')}` — **{title}** ({cost} pts){extra}")
        await _ephemeral(interaction, "**Shop items:**\n" + "\n".join(lines))

    @shop.command(name="deactivate", description="Hide a shop item")
    @_owner_only()
    @app_commands.describe(item_id="Shop item id")
    @app_commands.autocomplete(item_id=_ac_shop_item_id_all)
    async def owner_shop_deactivate(self, interaction: discord.Interaction, item_id: str):
        iid = normalize_item_id(item_id)
        it = await get_shop_item(iid)
        if not it or not isinstance(it, dict):
            await _ephemeral(interaction, "Shop item not found.")
            return
        it["active"] = False
        ok = await upsert_shop_item(it)
        await _ephemeral(interaction, "✅ Deactivated (hidden from shop)." if ok else "⚠️ Update failed.")

    @shop.command(name="reactivate", description="Show a deactivated shop item in the shop again")
    @_owner_only()
    @app_commands.describe(item_id="Shop item id")
    @app_commands.autocomplete(item_id=_ac_shop_item_id_all)
    async def owner_shop_reactivate(self, interaction: discord.Interaction, item_id: str):
        iid = normalize_item_id(item_id)
        it = await get_shop_item(iid)
        if not it or not isinstance(it, dict):
            await _ephemeral(interaction, "Shop item not found.")
            return
        it["active"] = True
        ok = await upsert_shop_item(it)
        await _ephemeral(interaction, "✅ Reactivated (visible in shop)." if ok else "⚠️ Update failed.")

    @shop.command(name="remove", description="Permanently remove a shop item")
    @_owner_only()
    @app_commands.describe(item_id="Shop item id")
    async def owner_shop_remove(self, interaction: discord.Interaction, item_id: str):
        iid = normalize_item_id(item_id)
        ok = await delete_shop_item(iid)
        await _ephemeral(interaction, "✅ Removed." if ok else "⚠️ Remove failed.")

    @shop.command(name="remove_by_pack", description="Remove shop items by pack id")
    @_owner_only()
    @app_commands.describe(pack_id="Pack id")
    async def owner_shop_remove_by_pack(self, interaction: discord.Interaction, pack_id: str):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        items = await list_shop_items()
        removed = 0
        for it in items:
            if str(it.get("kind") or "") == "pack_roll" and normalize_pack_id(str(it.get("pack_id") or "")) == pid:
                if await delete_shop_item(str(it.get("item_id") or "")):
                    removed += 1
        await _ephemeral(interaction, f"✅ Removed **{removed}** shop item(s) for pack `{pid}`.")

    @shop.command(name="remove_by_character", description="Remove shop items by character id")
    @_owner_only()
    @app_commands.describe(style_id="Character id")
    async def owner_shop_remove_by_character(self, interaction: discord.Interaction, style_id: str):
        sid = normalize_style_id(style_id)
        if not sid:
            await _ephemeral(interaction, "Invalid style id.")
            return
        items = await list_shop_items()
        removed = 0
        for it in items:
            if str(it.get("kind") or "") == "character_grant" and normalize_style_id(str(it.get("style_id") or "")) == sid:
                if await delete_shop_item(str(it.get("item_id") or "")):
                    removed += 1
        # Best-effort keep the LIMITED marker consistent.
        await set_limited_style(sid, False)
        await _ephemeral(interaction, f"✅ Removed **{removed}** shop item(s) for character `{sid}`.")

    @shop.command(name="edit", description="Edit a shop item (pack-roll or character-grant)")
    @_owner_only()
    @app_commands.describe(
        item_id="Shop item id",
        title="(Optional) New title",
        cost="(Optional) New cost",
        description="(Optional) New description",
        button_label="(Optional) Button label",
        button_emoji="(Optional) Button emoji",
        active="(Optional) Activate/deactivate item",
        pack_name="(Optional) New pack display name (pack-roll items only)",
        pack_description="New pack description (optional)",
        pack_private="(Optional) Make pack private/public (pack-roll items only)",
        pack_password="(Optional) Set/rotate pack password (pack-roll items only)",
        image="(Optional) Upload a new pack cover image (pack-roll items only)",
        image_url="(Optional) Direct https image URL for pack cover (pack-roll items only)",
    )
    @app_commands.autocomplete(item_id=_ac_shop_item_id)
    async def owner_shop_edit(
        self,
        interaction: discord.Interaction,
        item_id: str,
        title: str | None = None,
        cost: int | None = None,
        description: str | None = None,
        button_label: str | None = None,
        button_emoji: str | None = None,
        active: bool | None = None,
        pack_name: str | None = None,
        pack_description: str | None = None,
        pack_private: bool | None = None,
        pack_password: str | None = None,
        image: discord.Attachment | None = None,
        image_url: str | None = None,
    ):
        iid = normalize_item_id(item_id)
        it = await get_shop_item(iid)
        if not it or not isinstance(it, dict):
            await _ephemeral(interaction, "Shop item not found.")
            return

        # Shop item metadata updates
        if isinstance(title, str) and title.strip():
            it["title"] = title.strip()[:64]
        if isinstance(description, str):
            d = description.strip()
            if d:
                it["description"] = d[:800]
        if cost is not None:
            it["cost"] = max(0, int(cost))
        if button_label is not None:
            it["button_label"] = str(button_label or "")[:24]
        if button_emoji is not None:
            it["button_emoji"] = (str(button_emoji).strip() if button_emoji else "")
        if active is not None:
            it["active"] = bool(active)

        ok = await upsert_shop_item(it)

        # Pack-level updates (only for pack_roll items)
        pack_updated = False
        pack_fields_given = any(x is not None for x in [pack_name, pack_description, pack_private, pack_password, image, image_url])
        if pack_fields_given:
            kind = str(it.get("kind") or "")
            if kind != "pack_roll":
                await _ephemeral(interaction, "⚠️ Pack-level fields (pack_name, pack_description, etc.) only apply to pack-roll items. Shop item metadata was saved.")
                return

            pid = normalize_pack_id(str(it.get("pack_id") or ""))
            if not pid:
                await _ephemeral(interaction, "⚠️ Shop item has no pack_id. Cannot update pack.")
                return

            p = await get_custom_pack(pid)
            if not p or not isinstance(p, dict):
                await _ephemeral(interaction, "⚠️ Underlying pack not found in Redis. Shop item metadata was saved.")
                return

            if isinstance(pack_name, str) and pack_name.strip():
                p["name"] = pack_name.strip()[:64]
                pack_updated = True
            if isinstance(pack_description, str) and pack_description.strip():
                p["description"] = pack_description.strip()[:800]
                pack_updated = True
            if pack_private is not None:
                p["private"] = bool(pack_private)
                if not bool(pack_private):
                    p.pop("password_hash", None)
                    p.pop("password_salt", None)
                pack_updated = True
            if isinstance(pack_password, str) and pack_password.strip():
                pw_salt, pw_hash = hash_pack_password(pack_password.strip())
                p["password_salt"] = pw_salt
                p["password_hash"] = pw_hash
                pack_updated = True

            # Pack cover image
            if image is not None:
                ok_img, msg_img, rel = await save_attachment_image(
                    attachment=image,
                    rel_dir=f"packs/{pid}",
                    basename="pack",
                    max_bytes=20 * 1024 * 1024,
                    upscale_min_px=1024,
                )
                if not ok_img:
                    await _ephemeral(interaction, f"⚠️ Pack image: {msg_img}. Shop item metadata was saved.")
                    return
                if rel:
                    p["image_url"] = rel if rel.startswith("http") else f"asset:{rel}"
                    pack_updated = True
            elif isinstance(image_url, str) and image_url.strip():
                u = image_url.strip()
                if not (u.startswith("http://") or u.startswith("https://")):
                    await _ephemeral(interaction, "⚠️ image_url must start with http:// or https://. Shop item metadata was saved.")
                    return
                p["image_url"] = u
                pack_updated = True

            if pack_updated:
                ok_pack = await upsert_custom_pack(p)
                try:
                    merge_pack_payload(p)
                except Exception:
                    pass
                if not ok_pack:
                    await _ephemeral(interaction, "⚠️ Shop item updated, but pack update failed.")
                    return

        await _ephemeral(interaction, "✅ Updated." if ok else "⚠️ Update failed.")

    # ----------------------------
    # Takedown infrastructure
    # ----------------------------

    @packadmin.command(name="takedown", description="Remove a character from a pack (copyright/policy takedown)")
    @_owner_only()
    @app_commands.describe(
        pack_id="Pack containing the character",
        character_id="Character id to remove",
        reason="Reason for takedown (shown to creator)",
    )
    async def owner_takedown(
        self,
        interaction: discord.Interaction,
        pack_id: str,
        character_id: str,
        reason: str = "Violation of original-character policy",
    ):
        from utils.packs_store import remove_character_from_pack
        from utils.verification import increment_trust_denial
        from utils.backpressure import get_redis_or_none

        pid = normalize_pack_id(pack_id)
        cid = normalize_style_id(character_id)
        if not pid or not cid:
            await _ephemeral(interaction, "Invalid pack_id or character_id.")
            return

        pack = await get_custom_pack(pid)
        if not pack or not isinstance(pack, dict):
            await _ephemeral(interaction, "Pack not found.")
            return

        chars = pack.get("characters") or []
        target_char = None
        for c in (chars if isinstance(chars, list) else []):
            if isinstance(c, dict):
                sid = normalize_style_id(str(c.get("id") or c.get("style_id") or ""))
                if sid == cid:
                    target_char = c
                    break

        if not target_char:
            await _ephemeral(interaction, f"Character `{cid}` not found in pack `{pid}`.")
            return

        ok, msg = await remove_character_from_pack(pid, cid)
        if not ok:
            await _ephemeral(interaction, f"Failed to remove: {msg}")
            return

        try:
            merge_pack_payload(await get_custom_pack(pid) or {})
        except Exception:
            pass

        creator_uid = int(pack.get("created_by_user") or 0)
        creator_gid = int(pack.get("created_by_guild") or 0)
        if creator_uid:
            await increment_trust_denial(guild_id=creator_gid, user_id=creator_uid)

        r = await get_redis_or_none()
        if r:
            import json as _json
            log_entry = _json.dumps({
                "pack_id": pid,
                "character_id": cid,
                "display_name": str(target_char.get("display_name") or cid),
                "reason": str(reason)[:500],
                "removed_by": int(interaction.user.id),
                "timestamp": int(time.time()),
                "creator_user": creator_uid,
                "creator_guild": creator_gid,
            })
            try:
                await r.rpush("takedowns:log", log_entry)
                await r.expire("takedowns:log", 86400 * 365)
            except Exception:
                pass

        try:
            if creator_uid:
                u = self.bot.get_user(creator_uid) or await self.bot.fetch_user(creator_uid)
                if u:
                    await u.send(
                        f"**Content Takedown Notice**\n\n"
                        f"Your character **{target_char.get('display_name') or cid}** (`{cid}`) "
                        f"in pack `{pid}` has been removed.\n"
                        f"**Reason:** {reason}\n\n"
                        "Custom characters must be entirely original and not based on any real person, "
                        "public figure, copyrighted character, or trademarked property. "
                        "Repeat violations may result in account restrictions."
                    )
        except Exception:
            pass

        await _ephemeral(
            interaction,
            f"Removed **{target_char.get('display_name') or cid}** (`{cid}`) from `{pid}`.\n"
            f"Reason: {reason}\nCreator trust score decremented.",
        )

    @packadmin.command(name="takedown_search", description="Search all custom packs for a character name/id")
    @_owner_only()
    @app_commands.describe(query="Name or id substring to search for")
    async def owner_takedown_search(self, interaction: discord.Interaction, query: str):
        q = (query or "").strip().lower()
        if not q or len(q) < 2:
            await _ephemeral(interaction, "Query too short (min 2 chars).")
            return

        packs = await list_custom_packs(limit=600, include_internal=True, include_shop_only=True)
        results: list[str] = []
        for p in packs or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("pack_id") or "")
            chars = p.get("characters") or []
            if not isinstance(chars, list):
                continue
            for c in chars:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or c.get("style_id") or "").lower()
                cname = str(c.get("display_name") or "").lower()
                if q in cid or q in cname:
                    results.append(f"• `{pid}` / `{cid}` — {c.get('display_name', cid)}")

        if not results:
            await _ephemeral(interaction, f"No characters matching **{query}** found.")
            return

        header = f"**Search results for \"{query}\"** ({len(results)} found)\n"
        body = "\n".join(results[:40])
        if len(results) > 40:
            body += f"\n... and {len(results) - 40} more"
        await _ephemeral(interaction, (header + body)[:1950])

    # ----------------------------
    # /owner points ...
    # ----------------------------

    @points.command(name="give", description="Give points to a user (defaults to you)")
    @_owner_only()
    @app_commands.describe(amount="Points to add", user="Optional target user")
    async def points_give(self, interaction: discord.Interaction, amount: int, user: discord.Member | None = None):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this inside a server.")
            return
        gid = int(interaction.guild.id)
        target = user or interaction.user
        uid = int(getattr(target, "id", 0) or 0)
        amt = max(0, int(amount or 0))
        if amt <= 0:
            await _ephemeral(interaction, "Amount must be > 0.")
            return
        try:
            new_bal = await adjust_points(
                guild_id=gid,
                user_id=uid,
                delta=amt,
                reason="owner_give",
                meta={"by": int(interaction.user.id), "amount": amt},
            )
            await _ephemeral(interaction, f"✅ Added **{amt}** points to <@{uid}>. New balance: **{new_bal}**.")
        except Exception:
            logger.exception("owner points give failed")
            await _ephemeral(interaction, "⚠️ Failed to give points. Check logs.")

    @points.command(name="take", description="Take points from a user (defaults to you)")
    @_owner_only()
    @app_commands.describe(amount="Points to remove", user="Optional target user")
    async def points_take(self, interaction: discord.Interaction, amount: int, user: discord.Member | None = None):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this inside a server.")
            return
        gid = int(interaction.guild.id)
        target = user or interaction.user
        uid = int(getattr(target, "id", 0) or 0)
        amt = max(0, int(amount or 0))
        if amt <= 0:
            await _ephemeral(interaction, "Amount must be > 0.")
            return
        try:
            new_bal = await adjust_points(
                guild_id=gid,
                user_id=uid,
                delta=-amt,
                reason="owner_take",
                meta={"by": int(interaction.user.id), "amount": amt},
            )
            await _ephemeral(interaction, f"✅ Removed **{amt}** points from <@{uid}>. New balance: **{new_bal}**.")
        except Exception:
            logger.exception("owner points take failed")
            await _ephemeral(interaction, "⚠️ Failed to take points. Check logs.")

    # ----------------------------
    # /owner ai ...
    # ----------------------------

    @ai.command(name="disable", description="Disable AI globally (runtime) for a duration")
    @_owner_only()
    @app_commands.describe(minutes="How long to disable (minutes)", reason="Why you're disabling")
    async def ai_disable(self, interaction: discord.Interaction, minutes: int = 60, reason: str = "manual"):
        ttl_s = max(300, int(minutes) * 60)
        await ai_disable_runtime(reason=f"Owner disable: {reason}", ttl_s=ttl_s)
        await _ephemeral(interaction, f"⛔ AI disabled (runtime) for ~{int(ttl_s/60)} minutes. Reason: `{reason}`")

    @ai.command(name="enable", description="Re-enable AI globally (runtime)")
    @_owner_only()
    async def ai_enable(self, interaction: discord.Interaction):
        await ai_enable_runtime()
        await _ephemeral(interaction, "✅ AI re-enabled (runtime).")

    # ----------------------------
    # /owner data ... (dangerous)
    # ----------------------------

    @data.command(name="clear_global", description="DANGEROUS: Clear stored data globally")
    @_owner_only()
    @app_commands.describe(days="How many days of analytics to delete (0 = all)")
    async def data_clear_global(self, interaction: discord.Interaction, days: int = 0):
        """Clear data across the whole bot.

        This deletes analytics + incidents and also clears durable tables that are not guild-scoped.
        """
        if delete is None:
            await _ephemeral(interaction, "⚠️ SQLAlchemy delete unavailable in this build.")
            return

        days_i = max(0, int(days or 0))

        # 1) DB deletes
        try:
            Session = get_sessionmaker()
            async with Session() as session:
                if days_i > 0:
                    day_strs = self._days_to_day_strs(days_i)
                    await session.execute(delete(AnalyticsDailyMetric).where(AnalyticsDailyMetric.day_utc.in_(day_strs)))
                else:
                    await session.execute(delete(AnalyticsDailyMetric))

                # first-seen and activity-day tables (analytics-related)
                if days_i <= 0:
                    await session.execute(delete(UserFirstSeen))
                    try:
                        await session.execute(delete(UserActivityDay))
                    except Exception:
                        pass  # table may not exist in older migrations

                # Global durable tables (use carefully)
                # Note: This is a full reset for Phase 2 economies too.
                # We clear points wallets/ledgers and quest progress/claims.
                await session.execute(delete(PointsLedger))
                await session.execute(delete(PointsWallet))
                await session.execute(delete(QuestClaim))
                await session.execute(delete(QuestProgress))

                if days_i <= 0:
                    await session.execute(delete(CharacterOwnedStyle))
                    await session.execute(delete(CharacterCustomStyle))
                    await session.execute(delete(CharacterUserState))

                await session.commit()
        except Exception:
            logger.exception("data_clear_global DB delete failed")
            await _ephemeral(interaction, "⚠️ Failed to clear DB tables. Check logs.")
            return

        # 2) Redis deletes (best-effort)
        await self._clear_analytics_redis(guild_id=None, days=days_i)
        await self._clear_incidents_redis()
        if days_i <= 0:
            from utils.leaderboard import reset_all_leaderboard_data
            await reset_all_leaderboard_data()

        await _ephemeral(interaction, f"✅ Cleared global data. days={days_i} (0 means all).")

    @data.command(name="clear_all", description="DANGEROUS: Reset all data")
    @_owner_only()
    async def data_clear_all(self, interaction: discord.Interaction):
        """Wipe every analytics, points, quests, characters, bonds, guild settings, leaderboard, and incidents.
        Use on a test server for a clean slate. Global commands and leaderboard will show zeros until new activity.
        """
        if delete is None:
            await _ephemeral(interaction, "⚠️ SQLAlchemy delete unavailable in this build.")
            return

        # 1) DB: full clear of all state tables
        try:
            Session = get_sessionmaker()
            async with Session() as session:
                await session.execute(delete(AnalyticsDailyMetric))
                await session.execute(delete(UserFirstSeen))
                try:
                    await session.execute(delete(UserActivityDay))
                except Exception:
                    pass
                await session.execute(delete(PointsLedger))
                await session.execute(delete(PointsWallet))
                await session.execute(delete(QuestClaim))
                await session.execute(delete(QuestProgress))
                await session.execute(delete(CharacterOwnedStyle))
                await session.execute(delete(CharacterCustomStyle))
                await session.execute(delete(CharacterUserState))
                await session.execute(delete(GuildSetting))
                await session.execute(delete(PremiumEntitlement))
                await session.execute(delete(BondState))
                await session.execute(delete(VoiceSound))
                await session.commit()
        except Exception:
            logger.exception("data_clear_all DB delete failed")
            await _ephemeral(interaction, "⚠️ Failed to clear DB tables. Check logs.")
            return

        # 2) Redis: wipe analytics, leaderboard, incidents, guild settings, leaderboard_reset
        redis_deleted = await self._wipe_all_redis_data()

        await _ephemeral(
            interaction,
            f"✅ **All data reset.** DB tables cleared; **{redis_deleted}** Redis keys deleted.\n\n"
            "Leaderboard and /owner global commands will show zeros until you use the bot again (rolls, /talk, /points daily). "
            "Then run `/owner global flush` to push Redis→Postgres if needed.",
        )

    @data.command(name="clear_guild", description="DANGEROUS: Clear stored data for THIS server")
    @_owner_only()
    @app_commands.describe(days="How many days of analytics to delete (0 = all)")
    async def data_clear_guild(self, interaction: discord.Interaction, days: int = 0):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this inside a server.")
            return
        if delete is None:
            await _ephemeral(interaction, "⚠️ SQLAlchemy delete unavailable in this build.")
            return

        gid = int(interaction.guild.id)
        days_i = max(0, int(days or 0))

        try:
            Session = get_sessionmaker()
            async with Session() as session:
                # analytics
                if days_i > 0:
                    day_strs = self._days_to_day_strs(days_i)
                    await session.execute(
                        delete(AnalyticsDailyMetric)
                        .where(AnalyticsDailyMetric.guild_id == gid)
                        .where(AnalyticsDailyMetric.day_utc.in_(day_strs))
                    )
                else:
                    await session.execute(delete(AnalyticsDailyMetric).where(AnalyticsDailyMetric.guild_id == gid))

                # per-guild tables
                await session.execute(delete(GuildSetting).where(GuildSetting.guild_id == gid))
                await session.execute(delete(PremiumEntitlement).where(PremiumEntitlement.guild_id == gid))
                await session.execute(delete(BondState).where(BondState.guild_id == gid))
                await session.execute(delete(VoiceSound).where(VoiceSound.guild_id == gid))

                # Phase 2 economy + quests (guild-scoped)
                await session.execute(delete(PointsLedger).where(PointsLedger.guild_id == gid))
                await session.execute(delete(PointsWallet).where(PointsWallet.guild_id == gid))
                await session.execute(delete(QuestClaim).where(QuestClaim.guild_id == gid))
                await session.execute(delete(QuestProgress).where(QuestProgress.guild_id == gid))

                await session.commit()
        except Exception:
            logger.exception("data_clear_guild DB delete failed")
            await _ephemeral(interaction, "⚠️ Failed to clear guild data. Check logs.")
            return

        await self._clear_analytics_redis(guild_id=gid, days=days_i)
        await _ephemeral(interaction, f"✅ Cleared data for guild **{gid}**. days={days_i} (0 = all analytics rows).")

    @data.command(name="leaderboard_opt_in", description="Force a user (or yourself) back onto leaderboards")
    @_owner_only()
    @app_commands.describe(user="User to opt in (leave empty for yourself)")
    async def data_leaderboard_opt_in(self, interaction: discord.Interaction, user: discord.User | None = None):
        """If someone (e.g. the bot owner) opted out and doesn't appear on leaderboards, force opt-in."""
        from utils.leaderboard import set_opt_out

        target_id = int((user or interaction.user).id)
        success = await set_opt_out(target_id, opt_out=False)
        if success:
            await _ephemeral(interaction, f"✅ User <@{target_id}> is now opted **in** to leaderboards. New activity will count.")
        else:
            await _ephemeral(interaction, "⚠️ Failed to update (Redis may be unavailable).")

    @data.command(name="leaderboard_reset", description="DANGEROUS: Wipe all leaderboard data")
    @_owner_only()
    async def data_leaderboard_reset(self, interaction: discord.Interaction):
        """Delete every leaderboard key in Redis. All rankings and opt-out state are lost. New activity will repopulate."""
        from utils.leaderboard import reset_all_leaderboard_data

        deleted = await reset_all_leaderboard_data()
        await _ephemeral(interaction, f"✅ Leaderboard full reset done. Deleted **{deleted}** Redis keys. Everyone is opted in; new activity will show on leaderboards.")

    @data.command(name="leaderboard_sync", description="Backfill leaderboards from DB")
    @_owner_only()
    async def data_leaderboard_sync(self, interaction: discord.Interaction):
        """Repopulate Points (global) and Characters Owned (global) from the database. Use when leaderboards are empty but DB has data."""
        await interaction.response.defer(ephemeral=True)
        from utils.leaderboard import update_all_periods, CATEGORY_POINTS, CATEGORY_CHARACTERS, GLOBAL_GUILD_ID
        from utils.character_store import get_all_owned_style_ids

        points_updated = 0
        chars_updated = 0
        try:
            Session = get_sessionmaker()
            # Points: all global wallets
            async with Session() as session:
                res = await session.execute(
                    select(PointsWallet.user_id, PointsWallet.balance).where(PointsWallet.guild_id == 0)
                )
                rows = res.all()
            for uid, bal in rows or []:
                try:
                    await update_all_periods(
                        category=CATEGORY_POINTS,
                        guild_id=GLOBAL_GUILD_ID,
                        user_id=int(uid),
                        value=float(int(bal or 0)),
                    )
                    points_updated += 1
                except Exception:
                    pass
            # Characters: all users who have at least one owned or custom style (global + every guild bot is in)
            async with Session() as session:
                owned = await session.execute(select(CharacterOwnedStyle.user_id).distinct())
                custom = await session.execute(select(CharacterCustomStyle.user_id).distinct())
                user_ids = set(r[0] for r in (owned.all() or [])) | set(r[0] for r in (custom.all() or []))
            guild_ids = [GLOBAL_GUILD_ID]
            for g in list(getattr(self.bot, "guilds", []) or []):
                gid = int(getattr(g, "id", 0) or 0)
                if gid and gid != GLOBAL_GUILD_ID:
                    guild_ids.append(gid)
            for uid in user_ids or []:
                try:
                    count = len(await get_all_owned_style_ids(int(uid)))
                    for gid in guild_ids:
                        await update_all_periods(
                            category=CATEGORY_CHARACTERS,
                            guild_id=gid,
                            user_id=int(uid),
                            value=float(count),
                        )
                    chars_updated += 1
                except Exception:
                    pass
            await interaction.followup.send(
                f"✅ Leaderboard sync done. **Points (global):** {points_updated} users. **Characters Owned (global + server):** {chars_updated} users.\n\n"
                "**Points (Server)** is only updated when someone claims daily in that server — have users run `/points daily` in the server to populate it.",
                ephemeral=True,
            )
        except Exception:
            logger.exception("leaderboard_sync failed")
            await interaction.followup.send("⚠️ Sync failed. Check logs.", ephemeral=True)

    @ai.command(name="why", description="Show last runtime AI disable reason")
    @_owner_only()
    async def ai_why(self, interaction: discord.Interaction):
        at, reason, ttl_s = await get_disable_meta()
        if not reason:
            await _ephemeral(interaction, "No recent runtime disable reason found.")
            return
        await _ephemeral(interaction, f"Last runtime disable: `{reason}`")

    # ----------------------------
    # /owner global ...
    # ----------------------------

    @global_.command(name="today", description="Global counters today")
    @_owner_only()
    async def global_today(self, interaction: discord.Interaction):
        from utils.analytics import read_daily_counters, _k_dirty, _k_count
        from utils.backpressure import get_redis_or_none

        day = utc_day_str()
        totals = {
            "daily_rolls": 0,
            "daily_talk_calls": 0,
            "daily_scene_calls": 0,
            "daily_ai_calls": 0,
            "daily_ai_token_budget": 0,
            "daily_active_users": 0,
            METRIC_PULL_5: 0,
            METRIC_PULL_10: 0,
            METRIC_TRIAL_START: 0,
            METRIC_CONVERSION: 0,
        }
        per_guild: list[tuple[int, dict[str, int]]] = []

        # Try to get guild IDs from Redis (dirty set) first, fallback to bot.guilds.
        # Always include guild_id=0 (global) since rolls and some metrics are stored there.
        guild_ids_to_check = {0}
        r = await get_redis_or_none()
        if r:
            try:
                # Get guilds from dirty set (guilds with activity today)
                dirty_guilds = await r.smembers(_k_dirty(day))
                for gid_raw in dirty_guilds or []:
                    try:
                        gid = int(gid_raw.decode("utf-8", errors="ignore") if isinstance(gid_raw, (bytes, bytearray)) else str(gid_raw))
                        if gid > 0:
                            guild_ids_to_check.add(gid)
                    except Exception:
                        continue
            except Exception:
                pass

        # Also add all cached guilds from bot
        for g in list(getattr(self.bot, "guilds", []) or []):
            try:
                gid = int(getattr(g, "id", 0) or 0)
                if gid > 0:
                    guild_ids_to_check.add(gid)
            except Exception:
                continue

        # If we have Redis, also scan for any guilds with counters today
        if r:
            try:
                # Scan for analytics:count keys for today
                cursor = 0
                for _ in range(20):  # Limit scans
                    cursor, keys = await r.scan(cursor, match=f"analytics:count:{day}:*", count=100)
                    for key_raw in keys or []:
                        try:
                            key = key_raw.decode("utf-8", errors="ignore") if isinstance(key_raw, (bytes, bytearray)) else str(key_raw)
                            # Format: analytics:count:YYYYMMDD:guild_id:metric
                            parts = key.split(":")
                            if len(parts) >= 4:
                                gid = int(parts[3])
                                if gid > 0:
                                    guild_ids_to_check.add(gid)
                        except Exception:
                            continue
                    if cursor == 0:
                        break
            except Exception:
                pass

        # Process all found guilds
        for gid in guild_ids_to_check:
            try:
                c = await read_daily_counters(day_utc=day, guild_id=gid)
                per_guild.append((gid, c))
                totals["daily_rolls"] += int(c.get("daily_rolls", 0) or 0)
                totals["daily_talk_calls"] += int(c.get("daily_talk_calls", 0) or 0)
                totals["daily_scene_calls"] += int(c.get("daily_scene_calls", 0) or 0)
                totals["daily_ai_calls"] += int(c.get("daily_ai_calls", 0) or 0)
                totals["daily_ai_token_budget"] += int(c.get("daily_ai_token_budget", 0) or 0)
                totals["daily_active_users"] += int(c.get("daily_active_users", 0) or 0)
                totals[METRIC_PULL_5] += int(c.get(METRIC_PULL_5, 0) or 0)
                totals[METRIC_PULL_10] += int(c.get(METRIC_PULL_10, 0) or 0)
                totals[METRIC_TRIAL_START] += int(c.get(METRIC_TRIAL_START, 0) or 0)
                totals[METRIC_CONVERSION] += int(c.get(METRIC_CONVERSION, 0) or 0)
            except Exception:
                continue

        # Top 5 guilds by tokens (quick anomaly sniff)
        top = sorted(
            per_guild,
            key=lambda it: int(it[1].get("daily_ai_token_budget", 0) or 0),
            reverse=True,
        )[:5]
        top_lines = []
        for gid, c in top:
            top_lines.append(
                f"- `{gid}` tokens={int(c.get('daily_ai_token_budget', 0) or 0)} talk={int(c.get('daily_talk_calls', 0) or 0)} rolls={int(c.get('daily_rolls', 0) or 0)}"
            )
        top_block = "\n".join(top_lines) if top_lines else "(none)"

        msg = (
            f"UTC day: `{day}`\n"
            f"Guilds seen: `{len(per_guild)}`\n\n"
            f"**Totals (sum across guilds)**\n"
            f"Rolls: `{totals['daily_rolls']}`\n"
            f"Talk calls: `{totals['daily_talk_calls']}`\n"
            f"Scene calls: `{totals['daily_scene_calls']}`\n"
            f"AI calls (talk+scene): `{totals['daily_ai_calls']}`\n"
            f"Active users (sum of per-guild actives; may double-count): `{totals['daily_active_users']}`\n"
            f"Tokens (total): `{totals['daily_ai_token_budget']}`\n"
            f"5-pulls: `{totals.get(METRIC_PULL_5, 0)}` | 10-pulls: `{totals.get(METRIC_PULL_10, 0)}`\n"
            f"Trials: `{totals.get(METRIC_TRIAL_START, 0)}` | Conversions: `{totals.get(METRIC_CONVERSION, 0)}`\n\n"
            f"**Top guilds by tokens**\n{top_block}"
        )
        if r is None:
            msg += "\n\n⚠️ **Redis is unavailable.** Counts are from Redis; run `/owner health` to check."
        elif totals.get("daily_rolls", 0) == 0 and totals.get("daily_talk_calls", 0) == 0:
            msg += "\n\n💡 If you expect activity: ensure Redis is connected and do a roll or /talk, then check again."
        await _ephemeral(interaction, msg)

    @global_.command(name="flush", description="Force Redis->Postgres flush")
    @_owner_only()
    async def global_flush(self, interaction: discord.Interaction):
        """Manual flush to help debug "all zeros" right after deployment.

        Normally the background flush loop does this automatically every ~minute.
        """
        from utils.analytics import utc_day_str, flush_day_to_db
        from utils.analytics import GLOBAL_GUILD_ID as ANALYTICS_GLOBAL_GID

        if select is None:
            await _ephemeral(interaction, "SQL support not available.")
            return

        now_ts = int(__import__("time").time())
        today = utc_day_str(now_ts)
        yesterday = utc_day_str(now_ts - 86400)

        flushed = 0
        for day in (today, yesterday):
            # Flush guild_id=0 first (global rolls and any metrics stored under 0) so /owner global total has data
            try:
                await flush_day_to_db(day_utc=day, guild_id=ANALYTICS_GLOBAL_GID)
                flushed += 1
            except Exception:
                pass
            for g in list(getattr(self.bot, "guilds", []) or []):
                gid = int(getattr(g, "id", 0) or 0)
                if not gid:
                    continue
                try:
                    await flush_day_to_db(day_utc=day, guild_id=gid)
                    flushed += 1
                except Exception:
                    continue

        await _ephemeral(interaction, f"✅ Flush complete. Upserted ~{flushed} guild-days into Postgres (includes global guild_id=0).")

    @global_.command(name="last7", description="Global last 7 days metrics (Postgres aggregate)")
    @_owner_only()
    async def global_last7(self, interaction: discord.Interaction):
        if select is None or func is None:
            await _ephemeral(interaction, "SQL support not available.")
            return

        # last 7 UTC days including today (totals only)
        now = int(__import__("time").time())
        days = [utc_day_str(now - 86400 * i) for i in range(0, 7)]

        cache_key = f"global_last7:{days[0]}"
        cache = await _cache_get(cache_key)
        if cache:
            totals = cache.get("totals") or {}
        else:
            try:
                Session = get_sessionmaker()
                async with Session() as session:
                    rows = await session.execute(
                        select(
                            AnalyticsDailyMetric.metric,
                            func.sum(AnalyticsDailyMetric.value),
                        )
                        .where(AnalyticsDailyMetric.day_utc.in_(days))
                        .group_by(AnalyticsDailyMetric.metric)
                    )
                    data = rows.all()
                totals = {str(m): int(v or 0) for (m, v) in data}
                await _cache_set(cache_key, {"totals": totals}, ttl_s=300)
            except Exception:
                logger.exception("Failed to query global_last7")
                totals = {}

        msg = (
            f"UTC last 7 days (including today): `{days[-1]}` → `{days[0]}`\n\n"
            f"Rolls: `{totals.get('daily_rolls', 0)}`\n"
            f"Talk calls: `{totals.get('daily_talk_calls', 0)}`\n"
            f"Scene calls: `{totals.get('daily_scene_calls', 0)}`\n"
            f"AI calls (talk+scene): `{totals.get('daily_ai_calls', 0)}`\n"
            f"Active users (sum of per-guild actives): `{totals.get('daily_active_users', 0)}`\n"
            f"Tokens: `{totals.get('daily_ai_token_budget', 0)}`\n"
            f"5-pulls: `{totals.get(METRIC_PULL_5, 0)}` | 10-pulls: `{totals.get(METRIC_PULL_10, 0)}`\n"
            f"Trials: `{totals.get(METRIC_TRIAL_START, 0)}` | Conversions: `{totals.get(METRIC_CONVERSION, 0)}`"
        )
        if all(int(totals.get(k, 0) or 0) == 0 for k in ("daily_ai_calls", "daily_rolls", "daily_ai_token_budget")):
            msg += "\n\nNote: Durable stats come from the Redis→Postgres flush loop. If you just deployed or just started using the bot, give it a couple minutes after some activity."

        await _ephemeral(interaction, msg)

    @global_.command(name="month", description="Global totals this month")
    @_owner_only()
    async def global_month(self, interaction: discord.Interaction):
        if select is None or func is None:
            await _ephemeral(interaction, "SQL support not available.")
            return

        month_prefix = time.strftime("%Y%m", time.gmtime())
        like = f"{month_prefix}%"

        cache_name = f"global_month:{month_prefix}"
        cached = await _cache_get(cache_name)
        if cached:
            totals = cached.get("totals") or {}
            top_tokens = cached.get("top_tokens") or []
            top_calls = cached.get("top_calls") or []
        else:
            try:
                Session = get_sessionmaker()
                async with Session() as session:
                    rows = await session.execute(
                        select(
                            AnalyticsDailyMetric.metric,
                            func.sum(AnalyticsDailyMetric.value),
                        )
                        .where(AnalyticsDailyMetric.day_utc.like(like))
                        .group_by(AnalyticsDailyMetric.metric)
                    )
                    data = rows.all()
                    totals = {str(m): int(v or 0) for (m, v) in data}

                    # Top 10 guilds by tokens this month
                    rows2 = await session.execute(
                        select(
                            AnalyticsDailyMetric.guild_id,
                            func.sum(AnalyticsDailyMetric.value).label("v"),
                        )
                        .where(AnalyticsDailyMetric.day_utc.like(like))
                        .where(AnalyticsDailyMetric.metric == "daily_ai_token_budget")
                        .group_by(AnalyticsDailyMetric.guild_id)
                        .order_by(func.sum(AnalyticsDailyMetric.value).desc())
                        .limit(10)
                    )
                    top_tokens = [(int(gid), int(v or 0)) for (gid, v) in rows2.all()]

                    # Top 10 guilds by AI calls this month
                    rows3 = await session.execute(
                        select(
                            AnalyticsDailyMetric.guild_id,
                            func.sum(AnalyticsDailyMetric.value).label("v"),
                        )
                        .where(AnalyticsDailyMetric.day_utc.like(like))
                        .where(AnalyticsDailyMetric.metric == "daily_ai_calls")
                        .group_by(AnalyticsDailyMetric.guild_id)
                        .order_by(func.sum(AnalyticsDailyMetric.value).desc())
                        .limit(10)
                    )
                    top_calls = [(int(gid), int(v or 0)) for (gid, v) in rows3.all()]

                # Cache for 15 minutes (cheap + prevents heavy month scans)
                await _cache_set(cache_name, {"totals": totals, "top_tokens": top_tokens, "top_calls": top_calls}, ttl_s=900)
            except Exception:
                logger.exception("Failed to query global_month")
                totals = {}
                top_tokens = []
                top_calls = []

        top_tokens_block = "\n".join([f"{i+1}. `{gid}` — `{v}` tokens" for i, (gid, v) in enumerate(top_tokens)]) or "(none)"
        top_calls_block = "\n".join([f"{i+1}. `{gid}` — `{v}` AI calls" for i, (gid, v) in enumerate(top_calls)]) or "(none)"

        msg = (
            f"UTC month: `{month_prefix}`\n\n"
            f"Rolls: `{totals.get('daily_rolls', 0)}`\n"
            f"Talk calls: `{totals.get('daily_talk_calls', 0)}`\n"
            f"Scene calls: `{totals.get('daily_scene_calls', 0)}`\n"
            f"AI calls (talk+scene): `{totals.get('daily_ai_calls', 0)}`\n"
            f"Active users (sum of per-guild actives): `{totals.get('daily_active_users', 0)}`\n"
            f"Tokens: `{totals.get('daily_ai_token_budget', 0)}`\n\n"
            f"**Top 10 guilds by tokens (month)**\n{top_tokens_block}\n\n"
            f"**Top 10 guilds by AI calls (month)**\n{top_calls_block}"
        )
        if all(int(totals.get(k, 0) or 0) == 0 for k in ("daily_ai_calls", "daily_rolls", "daily_ai_token_budget")):
            msg += "\n\nNote: If you just deployed or haven’t had activity yet, monthly totals will be 0 until the Redis→Postgres flush writes data."
        await _ephemeral(interaction, msg)

    @global_.command(name="total", description="Global totals for all time (Postgres aggregate)")
    @_owner_only()
    async def global_total(self, interaction: discord.Interaction):
        if select is None or func is None:
            await _ephemeral(interaction, "SQL support not available.")
            return

        try:
            # Flush Redis → Postgres for today/yesterday (guild 0 + all guilds) so totals are up to date
            from utils.analytics import (
                utc_day_str,
                flush_day_to_db,
                read_daily_counters,
                GLOBAL_GUILD_ID as ANALYTICS_GLOBAL_GID,
            )
            from utils.backpressure import get_redis_or_none

            await interaction.response.defer(ephemeral=True)
            redis_available = (await get_redis_or_none()) is not None
            logger.info("global_total: Redis available=%s", redis_available)

            now_ts = int(__import__("time").time())
            today = utc_day_str(now_ts)
            yesterday = utc_day_str(now_ts - 86400)
            guilds = list(getattr(self.bot, "guilds", []) or [])
            logger.info("global_total: flushing today=%s yesterday=%s guild_0 + %s guilds", today, yesterday, len(guilds))

            for day in (today, yesterday):
                try:
                    await flush_day_to_db(day_utc=day, guild_id=ANALYTICS_GLOBAL_GID)
                except Exception as e:
                    logger.exception("global_total: flush guild 0 day=%s failed: %s", day, e)
                for g in guilds:
                    gid = int(getattr(g, "id", 0) or 0)
                    if gid:
                        try:
                            await flush_day_to_db(day_utc=day, guild_id=gid)
                        except Exception as e:
                            logger.exception("global_total: flush guild %s day=%s failed: %s", gid, day, e)

            # Snapshot Redis (guild 0 today) so we can show user if Redis has data but DB doesn't
            redis_snapshot = {}
            try:
                redis_snapshot = await read_daily_counters(day_utc=today, guild_id=ANALYTICS_GLOBAL_GID)
            except Exception as e:
                logger.warning("global_total: read_daily_counters(guild 0) failed: %s", e)

            Session = get_sessionmaker()
            async with Session() as session:
                rows = await session.execute(
                    select(
                        AnalyticsDailyMetric.metric,
                        func.sum(AnalyticsDailyMetric.value),
                    ).group_by(AnalyticsDailyMetric.metric)
                )
                data = rows.all()

            totals = {str(m): int(v or 0) for (m, v) in data}
            msg = (
                "**All-time totals (Postgres)**\n\n"
                f"Rolls: `{totals.get('daily_rolls', 0)}`\n"
                f"Talk calls: `{totals.get('daily_talk_calls', 0)}`\n"
                f"Scene calls: `{totals.get('daily_scene_calls', 0)}`\n"
                f"AI calls (talk+scene): `{totals.get('daily_ai_calls', 0)}`\n"
                f"Active users (sum of per-guild actives): `{totals.get('daily_active_users', 0)}`\n"
                f"Tokens: `{totals.get('daily_ai_token_budget', 0)}`"
            )
            if all(int(totals.get(k, 0) or 0) == 0 for k in ("daily_rolls", "daily_talk_calls", "daily_ai_token_budget")):
                msg += "\n\n💡 Run a roll or /talk, then run this command again (it flushes Redis→DB first)."
                if not redis_available:
                    msg += " **Redis is not connected** — metrics are not being recorded."
                else:
                    # Show Redis snapshot so user can see if data exists but didn't make it to DB
                    r_rolls = int(redis_snapshot.get("daily_rolls", 0) or 0)
                    r_talk = int(redis_snapshot.get("daily_talk_calls", 0) or 0)
                    msg += f"\n_Redis (guild 0 today): rolls={r_rolls} talk={r_talk} (talk is per-server, so 0 here)_"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            logger.exception("Failed to query global_total")
            try:
                await interaction.followup.send("⚠️ Failed to query totals. Check logs.", ephemeral=True)
            except Exception:
                await _ephemeral(interaction, "⚠️ Failed to query totals. Check logs.")

    @global_.command(name="incidents", description="Recent incident log")
    @_owner_only()
    async def global_incidents(self, interaction: discord.Interaction):
        from utils.incidents import list_recent_incidents

        items = await list_recent_incidents(limit=15)
        if not items:
            await _ephemeral(interaction, "No recent incidents found (or Redis unavailable).")
            return

        lines = []
        for d in items:
            t = int(d.get("t") or 0)
            ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) if t else "(time?)"
            kind = str(d.get("kind") or "incident")
            reason = str(d.get("reason") or "")
            lines.append(f"- `{ts}` **{kind}** — {reason}"[:1900])

        await _ephemeral(interaction, "\n".join(lines[:15]))

    # ----------------------------
    # /owner health
    # ----------------------------

    @owner.command(name="health", description="Check bot connectivity (Redis + Postgres)")
    @_owner_only()
    async def owner_health(self, interaction: discord.Interaction):
        redis_ok = False
        db_ok = False
        details: list[str] = []

        # Redis
        try:
            r = await get_redis()
            redis_ok = bool(await r.ping())
        except Exception as e:
            details.append(f"Redis error: {type(e).__name__}: {e}")

        # Postgres
        try:
            Session = get_sessionmaker()
            async with Session() as session:
                # simple query
                await session.get(PremiumEntitlement, 0)
            db_ok = True
        except Exception as e:
            details.append(f"DB error: {type(e).__name__}: {e}")

        status = (
            f"✅ Redis: {'OK' if redis_ok else 'FAIL'}\n"
            f"✅ Postgres: {'OK' if db_ok else 'FAIL'}"
        )
        if details:
            status += "\n\n" + "\n".join(details[:5])
        await _ephemeral(interaction, status)

    # ----------------------------
    # /z_owner shop_sync
    # ----------------------------

    @owner.command(name="shop_sync", description="Re-sync JSON shop items to Redis")
    @_owner_only()
    async def owner_shop_sync(self, interaction: discord.Interaction):
        from utils.shop_store import sync_shop_items_from_registry
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await sync_shop_items_from_registry()
            await interaction.followup.send(
                f"Shop sync complete: **{synced}** item(s) pushed to Redis from JSON.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"Shop sync failed: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )

    # ----------------------------
    # /z_owner world_event ...
    # ----------------------------

    @owner.command(name="world_events_list", description="List all world events")
    @_owner_only()
    async def owner_world_events_list(self, interaction: discord.Interaction):
        from utils.world_events import get_all_events
        await interaction.response.defer(ephemeral=True)
        events = get_all_events(include_inactive=True)
        if not events:
            await interaction.followup.send("No world events found.", ephemeral=True)
            return
        lines: list[str] = []
        for ev in events:
            status = "\u2705" if ev.get("active", True) else "\u274c"
            eid = ev.get("id", "?")
            summary = ev.get("summary", "No summary")[:80]
            expires = ev.get("expires", "never")
            chars = ", ".join(ev.get("affects", {}).keys()) or "none"
            lines.append(f"{status} **{eid}** | expires: {expires} | chars: {chars}\n> {summary}")
        text = "\n\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n\n... (truncated)"
        await interaction.followup.send(text, ephemeral=True)

    @owner.command(name="world_event_toggle", description="Activate or deactivate a world event")
    @_owner_only()
    @app_commands.describe(
        event_id="The id of the event to toggle",
        active="True to activate, False to deactivate",
    )
    async def owner_world_event_toggle(
        self,
        interaction: discord.Interaction,
        event_id: str,
        active: bool,
    ):
        from utils.world_events import toggle_event
        if toggle_event(event_id, active):
            state = "activated" if active else "deactivated"
            await _ephemeral(interaction, f"World event **{event_id}** {state}.")
        else:
            await _ephemeral(interaction, f"Event **{event_id}** not found.")

    @owner.command(name="world_event_remove", description="Permanently delete a world event")
    @_owner_only()
    @app_commands.describe(event_id="The id of the event to remove")
    async def owner_world_event_remove(self, interaction: discord.Interaction, event_id: str):
        from utils.world_events import remove_event
        if remove_event(event_id):
            await _ephemeral(interaction, f"World event **{event_id}** removed.")
        else:
            await _ephemeral(interaction, f"Event **{event_id}** not found.")

    @owner.command(name="world_events_reload", description="Reload world events from data/world_events.json")
    @_owner_only()
    async def owner_world_events_reload(self, interaction: discord.Interaction):
        from utils.world_events import reload_events
        count = reload_events()
        await _ephemeral(interaction, f"Reloaded **{count}** world event(s) from disk.")

    @owner.command(name="world_event_add", description="Add a quick world event at runtime")
    @_owner_only()
    @app_commands.describe(
        event_id="Unique event id (lowercase, underscores ok)",
        summary="Short public summary of the event",
        character_id="Character affected (single character for quick-add)",
        context="The narrative context injected into that character's prompt",
        expires="Expiration date YYYY-MM-DD (optional)",
    )
    async def owner_world_event_add(
        self,
        interaction: discord.Interaction,
        event_id: str,
        summary: str,
        character_id: str,
        context: str,
        expires: str | None = None,
    ):
        from utils.world_events import add_event
        ev: dict = {
            "id": event_id.strip().lower(),
            "date": str(__import__("datetime").date.today()),
            "summary": summary,
            "affects": {character_id.strip().lower(): context},
            "active": True,
        }
        if expires:
            ev["expires"] = expires.strip()
        if add_event(ev):
            await _ephemeral(
                interaction,
                f"World event **{ev['id']}** added affecting **{character_id}**.\n"
                f"Expires: {ev.get('expires', 'never')}",
            )
        else:
            await _ephemeral(interaction, f"Event id **{ev['id']}** already exists.")

    # ----------------------------
    # /owner premium ...
    # ----------------------------

    @premium.command(name="get", description="Show premium tier for a user")
    @_owner_only()
    @app_commands.describe(user_id="Discord user id to look up")
    async def premium_get(self, interaction: discord.Interaction, user_id: str | None = None):
        uid: int | None
        if user_id and user_id.strip().isdigit():
            uid = int(user_id.strip())
        else:
            uid = int(interaction.user.id)

        if not uid:
            await _ephemeral(interaction, "Provide a user_id.")
            return

        tier = await get_premium_tier(uid)

        source = "unknown"
        try:
            Session = get_sessionmaker()
            async with Session() as session:
                ent = await session.get(UserPremiumEntitlement, uid)
                if ent:
                    source = getattr(ent, "source", "unknown")
        except Exception:
            source = "db_error"

        await _ephemeral(interaction, f"User `{uid}` premium tier: `{tier}` (source: `{source}`)")

    @premium.command(name="set", description="Set premium tier for a user (manual override)")
    @_owner_only()
    @app_commands.describe(
        tier="free or pro",
        user_id="Discord user id to set",
        source="Optional label: manual/stripe/gift/etc",
    )
    @app_commands.choices(tier=[
        app_commands.Choice(name="free", value="free"),
        app_commands.Choice(name="pro", value="pro"),
    ])
    async def premium_set(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        user_id: str | None = None,
        source: str | None = "manual",
    ):
        uid: int | None
        if user_id and user_id.strip().isdigit():
            uid = int(user_id.strip())
        else:
            uid = int(interaction.user.id)

        if not uid:
            await _ephemeral(interaction, "Provide a user_id.")
            return

        from datetime import datetime, timezone as tz
        now = datetime.now(tz.utc)

        try:
            Session = get_sessionmaker()
            async with Session() as session:
                ent = await session.get(UserPremiumEntitlement, uid)
                if ent:
                    ent.tier = tier.value
                    ent.source = (source or "manual")[:32]
                    ent.updated_at = now
                else:
                    ent = UserPremiumEntitlement(user_id=uid, tier=tier.value, source=(source or "manual")[:32], updated_at=now)
                    session.add(ent)
                await session.commit()
        except Exception:
            logger.exception("Failed setting user premium tier")
            await _ephemeral(interaction, "⚠️ DB error setting premium tier.")
            return

        await _ephemeral(interaction, f"✅ Set user `{uid}` premium tier to `{tier.value}`.")


    # ----------------------------
    # /owner analytics ...
    # ----------------------------

    @analytics.command(name="today", description="Today's counters for this guild")
    @_owner_only()
    async def analytics_today(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this in a server.")
            return
        from utils.analytics import read_daily_counters

        gid = int(interaction.guild.id)
        day = utc_day_str()
        counters = await read_daily_counters(day_utc=day, guild_id=gid)
        msg = (
            f"UTC day: `{day}`\n"
            f"Rolls: `{counters.get('daily_rolls', 0)}`\n"
            f"Talk calls: `{counters.get('daily_talk_calls', 0)}`\n"
            f"Scene calls: `{counters.get('daily_scene_calls', 0)}`\n"
            f"AI calls (talk+scene): `{counters.get('daily_ai_calls', 0)}`\n"
            f"Active users: `{counters.get('daily_active_users', 0)}`\n"
            f"Tokens: `{counters.get('daily_ai_token_budget', 0)}`\n"
            f"5-pulls: `{counters.get(METRIC_PULL_5, 0)}` | 10-pulls: `{counters.get(METRIC_PULL_10, 0)}`\n"
            f"Trials started: `{counters.get(METRIC_TRIAL_START, 0)}` | Conversions: `{counters.get(METRIC_CONVERSION, 0)}`"
        )
        await _ephemeral(interaction, msg)

    @analytics.command(name="last7", description="Show last 7 days metrics from Postgres")
    @_owner_only()
    async def analytics_last7(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this in a server.")
            return
        if select is None:
            await _ephemeral(interaction, "SQL support not available.")
            return

        gid = int(interaction.guild.id)
        # last 7 UTC days including today
        days = [utc_day_str(int(__import__('time').time()) - 86400 * i) for i in range(0, 7)]
        Session = get_sessionmaker()
        async with Session() as session:
            rows = await session.execute(
                select(AnalyticsDailyMetric.day_utc, AnalyticsDailyMetric.metric, AnalyticsDailyMetric.value)
                .where(AnalyticsDailyMetric.guild_id == gid)
                .where(AnalyticsDailyMetric.day_utc.in_(days))
            )
            data = rows.all()

        # format grouped by day
        per: dict[str, dict[str, int]] = {d: {} for d in days}
        for d, m, v in data:
            per[str(d)][str(m)] = int(v or 0)

        lines = []
        for d in reversed(days):
            dd = per.get(d, {})
            p5 = dd.get(METRIC_PULL_5, 0)
            p10 = dd.get(METRIC_PULL_10, 0)
            trial = dd.get(METRIC_TRIAL_START, 0)
            conv = dd.get(METRIC_CONVERSION, 0)
            lines.append(
                f"`{d}` rolls={dd.get('daily_rolls', 0)} talk={dd.get('daily_talk_calls', 0)} active={dd.get('daily_active_users', 0)} "
                f"tokens={dd.get('daily_ai_token_budget', 0)} | 5p={p5} 10p={p10} trial={trial} conv={conv}"
            )
        await _ephemeral(interaction, "\n".join(lines[:7]))

    @analytics.command(name="funnel", description="Conversion funnel (last 7 days)")
    @_owner_only()
    async def analytics_funnel(self, interaction: discord.Interaction):
        """Show funnel metrics for last 7 days (Postgres aggregate)."""
        if select is None or func is None:
            await _ephemeral(interaction, "SQL support not available.")
            return

        now = int(__import__("time").time())
        days = [utc_day_str(now - 86400 * i) for i in range(0, 7)]

        try:
            Session = get_sessionmaker()
            async with Session() as session:
                # New users (first_seen) - count UserFirstSeen by first_day_utc
                new_users_res = await session.execute(
                    select(func.count())
                    .select_from(UserFirstSeen)
                    .where(UserFirstSeen.first_day_utc.in_(days))
                )
                new_users = int(new_users_res.scalar() or 0)

                # Metrics from AnalyticsDailyMetric (sum across all guilds for global view)
                metrics_res = await session.execute(
                    select(
                        AnalyticsDailyMetric.metric,
                        func.sum(AnalyticsDailyMetric.value),
                    )
                    .where(AnalyticsDailyMetric.day_utc.in_(days))
                    .group_by(AnalyticsDailyMetric.metric)
                )
                data = {str(m): int(v or 0) for (m, v) in metrics_res.all()}

            rolls = data.get("daily_rolls", 0)
            talk = data.get("daily_talk_calls", 0)
            trial = data.get(METRIC_TRIAL_START, 0)
            conv = data.get(METRIC_CONVERSION, 0)
            p5 = data.get(METRIC_PULL_5, 0)
            p10 = data.get(METRIC_PULL_10, 0)

            msg = (
                f"**Conversion funnel** (UTC last 7 days: `{days[-1]}` → `{days[0]}`)\n\n"
                f"1️⃣ New users (first seen): `{new_users}`\n"
                f"2️⃣ Rolls: `{rolls}`\n"
                f"3️⃣ Talk calls: `{talk}`\n"
                f"4️⃣ Trials started: `{trial}`\n"
                f"5️⃣ Conversions (Pro): `{conv}`\n\n"
                f"**Shop** 5-pulls: `{p5}` | 10-pulls: `{p10}`"
            )
            await _ephemeral(interaction, msg)
        except Exception:
            logger.exception("analytics funnel failed")
            await _ephemeral(interaction, "⚠️ Failed to load funnel. Check logs.")

    @analytics.command(name="retention", description="D1/D7/D30 retention (last 7 cohorts)")
    @_owner_only()
    async def analytics_retention(self, interaction: discord.Interaction):
        from utils.dashboard_queries import get_retention_stats

        gid = int(interaction.guild.id) if interaction.guild else None
        try:
            stats = await get_retention_stats(guild_id=gid, cohort_days_back=7)
            if not stats:
                await _ephemeral(interaction, "No retention data yet. Run the migration and wait for activity.")
                return
            lines = []
            for s in stats:
                lines.append(
                    f"`{s.cohort_day}` cohort={s.cohort_size} | D1: {s.d1_retained} ({s.d1_pct:.0f}%) | "
                    f"D7: {s.d7_retained} ({s.d7_pct:.0f}%) | D30: {s.d30_retained} ({s.d30_pct:.0f}%)"
                )
            msg = "**Retention** (requires UserActivityDay table + migration 0009)\n\n" + "\n".join(lines)
            await _ephemeral(interaction, msg[:1900])
        except Exception:
            logger.exception("analytics retention failed")
            await _ephemeral(interaction, "⚠️ Failed to load retention. Run migration 0009?")

    @analytics.command(name="economy", description="Points spent by reason/item (last 7 days)")
    @_owner_only()
    async def analytics_economy(self, interaction: discord.Interaction):
        from utils.dashboard_queries import get_economy_stats

        try:
            stats = await get_economy_stats(days=7)
            lines = [f"**Total spent:** `{stats.total_points_spent}` points (last 7 days)"]
            if stats.by_reason:
                lines.append("\n**By reason:** " + ", ".join(f"{k}={v}" for k, v in sorted(stats.by_reason.items(), key=lambda x: -x[1])[:8]))
            if stats.by_item:
                lines.append("\n**By item:** " + ", ".join(f"{k}={v}" for k, v in sorted(stats.by_item.items(), key=lambda x: -x[1])[:8]))
            msg = "\n".join(lines) if lines else "No spend data yet."
            await _ephemeral(interaction, msg[:1900])
        except Exception:
            logger.exception("analytics economy failed")
            await _ephemeral(interaction, "⚠️ Failed to load economy.")

    @analytics.command(name="cost", description="AI token usage and estimated cost (last 7 days)")
    @_owner_only()
    async def analytics_cost(self, interaction: discord.Interaction):
        from utils.dashboard_queries import get_ai_cost_stats

        gid = int(interaction.guild.id) if interaction.guild else None
        try:
            stats = await get_ai_cost_stats(days=7, guild_id=gid)
            lines = [
                f"**Tokens (last 7 days):** `{stats.total_tokens}`",
                f"**Est. cost (USD):** `${stats.estimated_usd:.4f}`",
                "*(Set AI_COST_PER_1K_TOKENS env to override pricing)*",
            ]
            if stats.tokens_by_day:
                lines.append("\n**By day:** " + ", ".join(f"{d}={v}" for d, v in list(stats.tokens_by_day.items())[:7]))
            await _ephemeral(interaction, "\n".join(lines)[:1900])
        except Exception:
            logger.exception("analytics cost failed")
            await _ephemeral(interaction, "⚠️ Failed to load AI cost.")

    @analytics.command(name="churn", description="Guilds with declining activity, trials ended")
    @_owner_only()
    async def analytics_churn(self, interaction: discord.Interaction):
        from utils.dashboard_queries import get_churn_stats

        try:
            stats = await get_churn_stats()
            lines = [f"**Trials ended (free tier with trial source):** `{stats.trials_ended_recently}`"]
            lines.append(f"\n**Guilds declining** (AI calls last7 < 50% of prev7):")
            for gid, last7, prev7 in stats.guilds_declining[:5]:
                lines.append(f"• `{gid}`: {last7} → {prev7}")
            if not stats.guilds_declining:
                lines.append("(none)")
            await _ephemeral(interaction, "\n".join(lines)[:1900])
        except Exception:
            logger.exception("analytics churn failed")
            await _ephemeral(interaction, "⚠️ Failed to load churn.")

    # ----------------------------
    # Error handling
    # ----------------------------

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Avoid noisy Railway error logs for non-owner attempts.

        Discord will still *show* the slash command to everyone, but we want a clean,
        user-friendly message instead of stacktraces.
        """
        try:
            if isinstance(error, app_commands.CheckFailure):
                await _ephemeral(interaction, "Only the bot owner can use this command.")
                return
        except Exception:
            pass

        logger.exception("/owner command error: %s", type(error).__name__)
        try:
            await _ephemeral(interaction, "Something went wrong running that owner command.")
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(SlashOwner(bot))