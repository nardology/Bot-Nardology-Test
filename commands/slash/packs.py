# commands/slash/packs.py
from __future__ import annotations

import logging
from typing import Any, Dict

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.owner import is_bot_owner

from utils.premium import get_premium_tier
from utils.packs_store import (
    normalize_pack_id,
    normalize_style_id,
    list_custom_packs,
    get_custom_pack,
    upsert_custom_pack,
    delete_custom_pack,
    add_character_to_pack,
    remove_character_from_pack,
    get_enabled_pack_ids,
    enable_pack_for_guild,
    disable_pack_for_guild,
    list_packs_for_marketplace,
    get_pack_upvote_count,
    has_user_upvoted,
    upvote_pack,
    get_creator_leaderboard,
)
from utils.packs_builtin import list_builtin_packs, get_builtin_pack
from utils.pack_badges import badges_for_pack_payload
from utils.character_registry import merge_pack_payload
from utils.media_assets import save_attachment_image
from utils.media_assets import get_discord_file_for_asset, resolve_embed_image_url
from utils.pack_security import hash_pack_password, verify_pack_password
from utils.server_chars_store import (
    make_internal_id,
    list_server_chars,
    upsert_server_char,
    remove_server_char,
    to_pack_payload,
)
from utils.verification import (
    create_verification_ticket,
    get_ticket,
    get_trust_score,
    increment_trust_approval,
    increment_trust_denial,
)

logger = logging.getLogger("bot.packs")


RARITIES = ["common", "uncommon", "rare", "legendary", "mythic"]

RARITY_EMOJI = {
    "common": "⚪",
    "uncommon": "🟢",
    "rare": "🔵",
    "legendary": "🟣",
    "mythic": "🟡",
}


# Bond-cap options (by level). These map to the last level within each title band.
_BOND_CAP_OPTIONS: list[tuple[str, int | None]] = [
    ("Max (no cap)", None),
    ("Acquaintance", 2),
    ("Friend", 4),
    ("Trusted", 9),
    ("Close Companion", 14),
    ("Devoted", 19),
    ("Soulbound", 999),
]


async def _ac_bond_cap(interaction: discord.Interaction, current: str):
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    for label, lvl in _BOND_CAP_OPTIONS:
        value = "max" if lvl is None else str(int(lvl))
        if not cur or cur in label.lower() or cur in value:
            out.append(app_commands.Choice(name=label, value=value))
        if len(out) >= 25:
            break
    return out


def _parse_bond_cap(value: str | None) -> int | None:
    v = (value or "").strip().lower()
    if not v or v in {"max", "none"}:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None


def _guild_limits_msg() -> str:
    return (
        "This server has reached its custom pack limits. "
        "You can offer paid upgrades for more pack/character slots."
    )


def _is_unlimited(interaction: discord.Interaction) -> bool:
    return is_bot_owner(getattr(interaction, "user", None))


async def _notify_owner_verification_request(
    *,
    bot: commands.Bot,
    ticket_id: str,
    ticket_type: str,
    guild_id: int,
    user_id: int,
    payload: Dict[str, Any],
) -> None:
    """DM all bot owners with verification request details."""
    import config
    from commands.slash.z_server import VerificationDecisionView
    from utils.verification import get_ticket

    gid = int(guild_id)
    uid = int(user_id)
    view = VerificationDecisionView(ticket_id=ticket_id)

    # Fetch ticket to get original_payload for edits
    ticket = await get_ticket(ticket_id)
    original_payload = ticket.get("original_payload") if ticket else None

    # Format payload details for owner review
    lines: list[str] = []
    is_edit = ticket_type in {"pack_edit", "character_edit"}

    if ticket_type == "pack_create":
        lines.append(f"**Pack Create Request** `{ticket_id}`")
        lines.append(f"Pack ID: `{payload.get('pack_id', '')}`")
        lines.append(f"Name: {payload.get('name', '')}")
        lines.append(f"Description: {payload.get('description', '')[:300]}")
        lines.append(f"Private: {bool(payload.get('private', False))}")
    elif ticket_type == "pack_edit":
        lines.append(f"**Pack Edit Request** `{ticket_id}`")
        lines.append(f"Pack ID: `{payload.get('pack_id', '')}`")
        if original_payload:
            lines.append("**Changes:**")
            if payload.get("name") != original_payload.get("name"):
                lines.append(f"  Name: `{original_payload.get('name', '')}` → `{payload.get('name', '')}`")
            if payload.get("description") != original_payload.get("description"):
                old_desc = str(original_payload.get("description", ""))[:150]
                new_desc = str(payload.get("description", ""))[:150]
                lines.append(f"  Description: `{old_desc}` → `{new_desc}`")
            if payload.get("private") != original_payload.get("private"):
                lines.append(f"  Private: `{original_payload.get('private', False)}` → `{payload.get('private', False)}`")
        else:
            lines.append(f"Name: {payload.get('name', '')}")
            lines.append(f"Description: {payload.get('description', '')[:300]}")
            lines.append(f"Private: {bool(payload.get('private', False))}")
    elif ticket_type == "character_add":
        lines.append(f"**Character Add Request** `{ticket_id}`")
        lines.append(f"Pack ID: `{payload.get('pack_id', '')}`")
        lines.append(f"Character ID: `{payload.get('id') or payload.get('style_id', '')}`")
        lines.append(f"Display Name: {payload.get('display_name', '')}")
        lines.append(f"Rarity: {payload.get('rarity', 'common')}")
        lines.append(f"Description: {payload.get('description', '')[:300]}")
        lines.append(f"Prompt: {payload.get('prompt', '')[:500]}")
        if payload.get("image_url"):
            lines.append(f"Image: {str(payload.get('image_url', ''))[:200]}")
        if payload.get("tags"):
            lines.append(f"Tags: {', '.join(payload.get('tags', [])[:10])}")
    elif ticket_type == "character_edit":
        lines.append(f"**Character Edit Request** `{ticket_id}`")
        lines.append(f"Pack ID: `{payload.get('pack_id', '')}`")
        lines.append(f"Character ID: `{payload.get('id') or payload.get('style_id', '')}`")
        if original_payload:
            lines.append("**Changes:**")
            if payload.get("display_name") != original_payload.get("display_name"):
                lines.append(f"  Display Name: `{original_payload.get('display_name', '')}` → `{payload.get('display_name', '')}`")
            if payload.get("rarity") != original_payload.get("rarity"):
                lines.append(f"  Rarity: `{original_payload.get('rarity', '')}` → `{payload.get('rarity', '')}`")
            if payload.get("description") != original_payload.get("description"):
                old_desc = str(original_payload.get("description", ""))[:150]
                new_desc = str(payload.get("description", ""))[:150]
                lines.append(f"  Description: `{old_desc}` → `{new_desc}`")
            if payload.get("prompt") != original_payload.get("prompt"):
                old_prompt = str(original_payload.get("prompt", ""))[:200]
                new_prompt = str(payload.get("prompt", ""))[:200]
                lines.append(f"  Prompt: `{old_prompt}` → `{new_prompt}`")
            if payload.get("image_url") != original_payload.get("image_url"):
                lines.append(f"  Image: `{str(original_payload.get('image_url', ''))[:100]}` → `{str(payload.get('image_url', ''))[:100]}`")
            if payload.get("tags") != original_payload.get("tags"):
                old_tags = ", ".join(original_payload.get("tags", [])[:5])
                new_tags = ", ".join(payload.get("tags", [])[:5])
                lines.append(f"  Tags: `{old_tags}` → `{new_tags}`")
        else:
            lines.append(f"Display Name: {payload.get('display_name', '')}")
            lines.append(f"Rarity: {payload.get('rarity', 'common')}")
            lines.append(f"Description: {payload.get('description', '')[:300]}")
            lines.append(f"Prompt: {payload.get('prompt', '')[:500]}")

    lines.append(f"\nGuild: `{gid}`")
    lines.append(f"User: `{uid}`")
    content = "\n".join(lines)[:1900]

    sent = 0
    for oid in sorted(getattr(config, "BOT_OWNER_IDS", set()) or set()):
        try:
            owner = await bot.fetch_user(int(oid))
            await owner.send(content, view=view)
            sent += 1
        except Exception:
            continue
    if sent == 0:
        log.warning("Failed to DM any owners for verification ticket %s", ticket_id)


async def _ac_pack_any(interaction: discord.Interaction, current: str):
    cur = (current or "").lower().strip()
    # Allow enabling the per-guild server-only pseudo pack.
    # (This pack exists even if it isn't a built-in/custom pack record.)
    out: list[app_commands.Choice[str]] = []
    if interaction.guild:
        gid = int(interaction.guild.id)
        server_pid = f"server_{gid}"
        server_name = f"This Server ({gid})"
        hay = f"{server_pid} {server_name}".lower()
        if not cur or cur in hay:
            out.append(app_commands.Choice(name=f"{server_name} ({server_pid})"[:100], value=server_pid))

    builtin = list_builtin_packs()
    custom = await list_custom_packs()
    packs = builtin + custom
    for p in packs:
        # Hide private packs from autocomplete (they use /packs private_enable).
        try:
            if isinstance(p, dict) and bool(p.get("private", False)):
                continue
        except Exception:
            pass
        pid = str(p.get("pack_id") or "")
        name = str(p.get("name") or pid)
        if not pid:
            continue
        hay = f"{pid} {name}".lower()
        if cur and cur not in hay:
            continue
        out.append(app_commands.Choice(name=f"{name} ({pid})"[:100], value=pid))
        if len(out) >= 25:
            break
    return out


async def _ac_pack_enabled(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    enabled = await get_enabled_pack_ids(int(interaction.guild.id))
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    gid = int(interaction.guild.id)
    for pid in sorted(enabled):
        if cur and cur not in pid:
            continue
        # Friendly display for the server-only pseudo pack
        if pid == f"server_{gid}":
            name = f"This Server ({gid})"
        else:
            p = get_builtin_pack(pid) or (await get_custom_pack(pid)) or {"pack_id": pid, "name": pid}
            name = str(p.get("name") or pid)
        out.append(app_commands.Choice(name=f"{name} ({pid})"[:100], value=pid))
        if len(out) >= 25:
            break
    return out


async def _ac_pack_custom_owned(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    gid = int(interaction.guild.id)
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    # "This Server" option removed - packless characters are now shop-only
    for p in await list_custom_packs():
        if not _can_manage_custom_pack(interaction, p):
            continue
        pid = str(p.get("pack_id") or "")
        name = str(p.get("name") or pid)
        if not pid:
            continue
        hay = f"{pid} {name}".lower()
        if cur and cur not in hay:
            continue
        out.append(app_commands.Choice(name=f"{name} ({pid})"[:100], value=pid))
        if len(out) >= 25:
            break
    return out


async def _ac_rarity(interaction: discord.Interaction, current: str):
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    for r in RARITIES:
        if cur and cur not in r:
            continue
        out.append(app_commands.Choice(name=r, value=r))
    return out


async def _ac_character_in_selected_pack(interaction: discord.Interaction, current: str):
    pid = str(getattr(getattr(interaction, "namespace", None), "pack_id", "") or "").strip()
    pid = normalize_pack_id(pid)
    if not pid:
        return []
    pack = await get_custom_pack(pid)
    if not pack:
        return []
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    chars = pack.get("characters") or []
    if not isinstance(chars, list):
        return []
    for c in chars:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or c.get("style_id") or "")
        dn = str(c.get("display_name") or cid)
        if not cid:
            continue
        hay = f"{cid} {dn}".lower()
        if cur and cur not in hay:
            continue
        out.append(app_commands.Choice(name=f"{dn} ({cid})"[:100], value=cid))
        if len(out) >= 25:
            break
    return out


async def _ac_server_character(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    try:
        chars = await list_server_chars(int(interaction.guild.id))
    except Exception:
        chars = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        pid = str(c.get("public_id") or "").strip()
        name = str(c.get("display_name") or pid).strip()
        if not pid:
            continue
        hay = f"{pid} {name}".lower()
        if cur and cur not in hay:
            continue
        out.append(app_commands.Choice(name=f"{name} ({pid})"[:100], value=pid))
        if len(out) >= 25:
            break
    return out


def _is_guild_owner_or_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.user:
        return False
    # Guild owner always ok
    if int(getattr(interaction.guild, "owner_id", 0) or 0) == int(getattr(interaction.user, "id", 0) or 0):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False))


def _is_unlimited_creator(interaction: discord.Interaction) -> bool:
    """Bot owners get unlimited packs/characters."""
    try:
        return is_bot_owner(interaction.user)
    except Exception:
        return False


def _pack_owned_by_this_server(interaction: discord.Interaction, pack: dict[str, Any]) -> bool:
    """Ownership check for custom packs.

    New packs store created_by_guild. Older packs (or manually imported ones) might only
    have created_by_user; we allow those to be managed by the creating user within the
    same server.
    """
    if not interaction.guild or not interaction.user or not isinstance(pack, dict):
        return False
    if _is_unlimited_creator(interaction):
        return True
    gid = int(interaction.guild.id)
    uid = int(interaction.user.id)
    created_gid = int(pack.get("created_by_guild") or 0)
    if created_gid and created_gid == gid:
        return True
    # Legacy support: if created_by_guild is missing/0, fall back to created_by_user.
    if not created_gid:
        created_uid = int(pack.get("created_by_user") or 0)
        return created_uid == uid
    return False


def _can_manage_custom_pack(interaction: discord.Interaction, pack: dict[str, Any]) -> bool:
    """True if this user should be able to *edit* the given custom pack.

    - For normal packs: server ownership rules apply (created_by_guild == this guild).
    - For legacy packs without created_by_guild: allow the creating user to manage.
    - For *private* packs: only the creating user (created_by_user) may manage,
      even if they are a server admin. (Bot owners remain unlimited.)

    This prevents private packs from leaking via autocomplete or being edited by other admins.
    """
    if not _pack_owned_by_this_server(interaction, pack):
        return False
    try:
        if bool(pack.get("private", False)) and not _is_unlimited_creator(interaction):
            if not interaction.user:
                return False
            return int(pack.get("created_by_user") or 0) == int(interaction.user.id)
    except Exception:
        # If we can't evaluate privacy, fail closed.
        return False
    return True


async def _enforce_pack_limits_or_message(interaction: discord.Interaction) -> bool:
    """Return True if the guild is allowed to create a new custom pack.

    Phase 5: Pro guilds get unlimited custom packs (Pro creator perk).
    """
    if _is_unlimited_creator(interaction):
        return True
    if not interaction.guild:
        return False
    gid = int(interaction.guild.id)
    tier = await get_premium_tier(int(interaction.user.id))
    if tier == "pro":
        return True
    packs = await list_custom_packs()
    owned = [p for p in packs if _pack_owned_by_this_server(interaction, p)]
    if len(owned) >= int(getattr(config, "MAX_CUSTOM_PACKS_PER_GUILD", 3)):
        await _ephemeral(
            interaction,
            f"⚠️ This server is at its custom pack limit (**{len(owned)}**).\n"
            "(Upgrade to Pro for unlimited packs; bot owners are unlimited.)",
        )
        return False
    return True


async def _enforce_character_limits_or_message(interaction: discord.Interaction, pack: dict[str, Any]) -> bool:
    """Return True if the guild is allowed to add another character to this pack.

    Phase 5: Pro guilds get higher limits (Pro creator perk).
    """
    if _is_unlimited_creator(interaction):
        return True
    if not interaction.guild:
        return False
    gid = int(interaction.guild.id)
    tier = await get_premium_tier(int(interaction.user.id))
    is_pro = tier == "pro"

    # Per-pack limit (Pro: 2x or use env; for now Pro = same limit, only pack count is unlimited)
    chars = pack.get("characters") or []
    if not isinstance(chars, list):
        chars = []
    per_pack_limit = int(getattr(config, "MAX_CUSTOM_CHARS_PER_PACK", 25))
    if not is_pro and len(chars) >= per_pack_limit:
        await _ephemeral(
            interaction,
            f"⚠️ This pack is at its character limit (**{len(chars)}** / {per_pack_limit}).\n"
            "(Upgrade to Pro for more; bot owners are unlimited.)",
        )
        return False

    # Total-per-guild limit (Phase 5: Pro gets higher cap)
    total_limit = int(getattr(config, "MAX_CUSTOM_CHARS_TOTAL_PER_GUILD", 100))
    if is_pro:
        total_limit = int(getattr(config, "MAX_CUSTOM_CHARS_TOTAL_PER_GUILD_PRO", 250) or 250)
    total = 0
    for p in await list_custom_packs():
        if not _pack_owned_by_this_server(interaction, p):
            continue
        c = p.get("characters") or []
        if isinstance(c, list):
            total += len(c)
    if total >= total_limit:
        await _ephemeral(
            interaction,
            f"⚠️ This server is at its total custom character limit (**{total}** / {total_limit}).\n"
            "(Upgrade to Pro for more; bot owners are unlimited.)",
        )
        return False

    return True


def _asset_attachment_for_char(c: dict[str, Any]) -> tuple[discord.File | None, str | None]:
    url = c.get("image_url")
    if not isinstance(url, str) or not url:
        return None, None
    if url.startswith("asset:"):
        rel = url[len("asset:") :].strip()
        f = get_discord_file_for_asset(rel)
        if not f:
            return None, None
        return f, f"attachment://{f.filename}"
    return None, url


def _character_preview_embed(pack: dict[str, Any], c: dict[str, Any]) -> tuple[discord.Embed, discord.File | None]:
    pid = str(pack.get("pack_id") or "")
    pname = str(pack.get("name") or pid or "Pack")
    cid = str(c.get("id") or c.get("style_id") or "")
    dn = str(c.get("display_name") or cid or "Character")
    desc = str(c.get("description") or "").strip()
    rarity = str(c.get("rarity") or "").lower().strip()
    emoji = RARITY_EMOJI.get(rarity, "❔")

    e = discord.Embed(
        title=f"{emoji} {dn}",
        description=(desc or "(no description)") + f"\n\n**Pack:** {pname} (`{pid}`)",
        color=0x5865F2,
    )
    if cid:
        e.add_field(name="Character ID", value=f"`{cid}`", inline=True)
    e.add_field(name="Rarity", value=f"{emoji} **{rarity.title() if rarity else 'Unknown'}**", inline=True)

    shop_item = c.get("shop_item") if isinstance(c.get("shop_item"), dict) else None
    if shop_item and shop_item.get("exclusive"):
        active = shop_item.get("active", False)
        cost = int(shop_item.get("cost") or 0)
        if active:
            status = f"🏷️ **LIMITED** — Available in the shop for **{cost}** points!" if cost else "🏷️ **LIMITED** — Available in the shop!"
        else:
            status = "🔒 **LIMITED** — No longer available in the shop."
        e.add_field(name="Availability", value=status, inline=False)

    f, attach_url = _asset_attachment_for_char(c)
    if attach_url:
        e.set_image(url=attach_url)
    return e, f


async def _ephemeral(
    interaction: discord.Interaction,
    content: str,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Send an ephemeral message safely.

    Important: discord.py expects `view` to be omitted when there is no view.
    Passing `view=None` can trigger AttributeError inside discord.py.
    """
    try:
        kwargs: dict[str, Any] = {"ephemeral": True}
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view

        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
    except Exception:
        logger.exception("Failed to send packs response")


def _pack_creator_display(pack: dict[str, Any], pack_id: str) -> str:
    """Display creator for moderation/rewards: name/mention and user ID."""
    pid = (pack_id or "").strip().lower()
    if pid.startswith("server_"):
        return "Server-only (this server; no creator rewards)"
    if pid == "nardologybot" or pack.get("created_by") == "builtin":
        return "Built-in (NardologyBot)"
    uid = pack.get("created_by_user") or pack.get("created_by_user_id")
    if uid is not None and int(uid) != 0:
        return f"<@{int(uid)}> (ID: `{int(uid)}`)"
    gid = pack.get("created_by_guild")
    if gid is not None and int(gid) != 0:
        return f"Guild ID: `{int(gid)}` (user ID unknown)"
    return "Unknown"


def _pack_to_embed(
    pack: dict[str, Any], *, enabled: bool | None = None, upvotes: int | None = None,
) -> tuple[discord.Embed, discord.File | None]:
    """Build an embed for a pack.  Returns ``(embed, file)`` where *file* is a
    :class:`discord.File` that must be attached if non-``None`` (used for local
    asset thumbnails)."""
    name = str(pack.get("name") or pack.get("pack_id") or "Pack")
    pid = str(pack.get("pack_id") or "").strip()
    desc = str(pack.get("description") or "").strip()
    chars = pack.get("characters") or []
    if not isinstance(chars, list):
        chars = []

    badge = badges_for_pack_payload(pack)
    if pack.get("_featured"):
        badge = ("⭐ FEATURED " + badge).strip() if badge else "⭐ FEATURED"
    if upvotes is not None and upvotes > 0:
        badge = (badge + f" | ⬆️ {upvotes}").strip() if badge else f"⬆️ {upvotes}"

    e = discord.Embed(
        title=f"📦 {name} — {badge}",
        description=(desc or "(no description)") + f"\n\n**Pack ID:** `{pid}`",
        color=0x5865F2,
    )
    e.add_field(name="Creator", value=_pack_creator_display(pack, pid), inline=True)
    if enabled is not None:
        e.add_field(name="Enabled in this server", value=("✅ Yes" if enabled else "❌ No"), inline=True)

    # List characters (compact)
    lines: list[str] = []
    for c in chars[:25]:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or c.get("style_id") or "").strip()
        dn = str(c.get("display_name") or c.get("name") or cid).strip()
        rarity = str(c.get("rarity") or "").lower().strip()
        if not cid:
            continue
        shop_item = c.get("shop_item") if isinstance(c.get("shop_item"), dict) else None
        limited_tag = ""
        if shop_item and shop_item.get("exclusive"):
            if shop_item.get("active"):
                limited_tag = " — 🏷️ *LIMITED (in shop!)*"
            else:
                limited_tag = " — 🔒 *LIMITED (no longer available)*"
        lines.append(f"• **{dn}** (`{cid}`) — *{rarity or 'unknown'}*{limited_tag}")
    if lines:
        e.add_field(name=f"Characters ({min(len(chars), 25)}/{len(chars)})", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="Characters", value="(none yet)", inline=False)

    # Pack cover image (thumbnail)
    thumb_file: discord.File | None = None
    raw_img = pack.get("image_url") or ""
    if raw_img:
        public = resolve_embed_image_url(raw_img)
        if public:
            e.set_thumbnail(url=public)
        elif str(raw_img).startswith("asset:"):
            rel = str(raw_img)[len("asset:"):].strip().lstrip("/")
            f = get_discord_file_for_asset(rel)
            if f is not None:
                e.set_thumbnail(url=f"attachment://{f.filename}")
                thumb_file = f
        elif str(raw_img).startswith("http"):
            e.set_thumbnail(url=raw_img)

    return e, thumb_file


class PackSelectView(discord.ui.View):
    def __init__(self, *, bot: commands.Bot, guild_id: int, user_id: int, packs: list[dict[str, Any]]):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.packs = packs

        options: list[discord.SelectOption] = []
        for p in packs[:25]:
            pid = str(p.get("pack_id") or "")
            badge = badges_for_pack_payload(p)
            prefix = "⭐" if "OFFICIAL" in badge or p.get("_featured") else "🎨"
            if "EXCLUSIVE" in badge:
                prefix = prefix + "🕒"
            upv = p.get("_upvotes", 0)
            suffix = f" ⬆{upv}" if upv and isinstance(upv, int) else ""
            label = f"{prefix} {str(p.get('name') or pid)}{suffix}"[:100]
            d = str(p.get("description") or "")
            options.append(
                discord.SelectOption(
                    label=label,
                    value=pid,
                    description=(d[:100] if d else None),
                )
            )

        self.select = discord.ui.Select(placeholder="Select a pack…", options=options)
        self.select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(getattr(interaction.user, "id", 0) or 0) == self.user_id

    async def _on_select(self, interaction: discord.Interaction):
        pid = str(self.select.values[0] if self.select.values else "")
        enabled = pid in (await get_enabled_pack_ids(self.guild_id))
        pack = get_builtin_pack(pid) or (await get_custom_pack(pid))
        if not pack:
            await _ephemeral(interaction, "⚠️ Pack not found.")
            return
        upvotes = next((p.get("_upvotes") for p in self.packs if str(p.get("pack_id") or "") == pid), None)
        if upvotes is None and pid and str(pack.get("created_by") or "") != "builtin":
            upvotes = await get_pack_upvote_count(pid)
        e, thumb_file = _pack_to_embed(pack, enabled=enabled, upvotes=upvotes)
        view = PackDetailView(
            bot=self.bot,
            guild_id=self.guild_id,
            user_id=self.user_id,
            pack_id=pid,
            pack_payload=pack,
            all_packs=self.packs,
        )
        try:
            kwargs: dict[str, Any] = {"content": "", "embed": e, "view": view}
            if thumb_file is not None:
                kwargs["attachments"] = [thumb_file]
            await interaction.response.edit_message(**kwargs)
        except Exception:
            await _ephemeral(interaction, "⚠️ Failed to update pack view.")


class PackToggleView(discord.ui.View):
    def __init__(self, *, guild_id: int, user_id: int, pack_id: str, pack_name: str):
        super().__init__(timeout=180)
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.pack_id = normalize_pack_id(pack_id)
        self.pack_name = pack_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(getattr(interaction.user, "id", 0) or 0) == self.user_id

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success)
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can change packs.")
            return
        pack = get_builtin_pack(self.pack_id) or (await get_custom_pack(self.pack_id))
        if isinstance(pack, dict) and bool(pack.get("private", False)):
            await _ephemeral(interaction, "🔒 This is a private pack. Use **/packs private_enable** with the password.")
            return
        if isinstance(pack, dict) and bool(pack.get("exclusive", False)):
            tier = await get_premium_tier(self.user_id)
            if tier != "pro":
                await _ephemeral(interaction, "🕒 This pack is **exclusive**. Upgrade this server to **Pro** to enable it.")
                return
        ok = await enable_pack_for_guild(self.guild_id, self.pack_id)
        await _ephemeral(interaction, "✅ Enabled." if ok else "⚠️ Enable failed.")

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can change packs.")
            return
        ok = await disable_pack_for_guild(self.guild_id, self.pack_id)
        await _ephemeral(interaction, "✅ Disabled." if ok else "⚠️ Disable failed.")


class PackDetailView(discord.ui.View):
    """Pack details with enable/disable + character preview dropdown + upvote."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        pack_id: str,
        pack_payload: dict[str, Any],
        all_packs: list[dict[str, Any]],
    ):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.pack_id = normalize_pack_id(pack_id)
        self.pack_payload = pack_payload
        self.all_packs = all_packs
        self._upvotable = (
            str(pack_payload.get("created_by") or "") != "builtin"
            and self.pack_id != "nardologybot"
            and not bool(pack_payload.get("private", False))
        )

        # Character preview dropdown (polish step)
        chars = pack_payload.get("characters") or []
        if isinstance(chars, list) and chars:
            options: list[discord.SelectOption] = []
            for c in chars[:25]:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or c.get("style_id") or "").strip()
                dn = str(c.get("display_name") or c.get("name") or cid).strip()
                rarity = str(c.get("rarity") or "").lower().strip()
                emoji = RARITY_EMOJI.get(rarity, "❔")
                if not cid:
                    continue
                shop_item = c.get("shop_item") if isinstance(c.get("shop_item"), dict) else None
                desc = None
                if shop_item and shop_item.get("exclusive"):
                    desc = "🏷️ LIMITED — in shop!" if shop_item.get("active") else "🔒 LIMITED — no longer available"
                options.append(discord.SelectOption(label=(f"{emoji} {dn}"[:100]), value=cid, description=desc))
                if len(options) >= 25:
                    break
            if options:
                sel = discord.ui.Select(placeholder="Preview a character…", options=options)
                sel.callback = self._on_character_select  # type: ignore[assignment]
                self.add_item(sel)

        if self._upvotable:
            upv_btn = discord.ui.Button(label="⬆️ Upvote", style=discord.ButtonStyle.secondary, row=2)
            upv_btn.callback = self._on_upvote  # type: ignore[assignment]
            self.add_item(upv_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(getattr(interaction.user, "id", 0) or 0) == self.user_id

    async def _on_character_select(self, interaction: discord.Interaction):
        cid = ""
        try:
            # first select in the view is our preview select
            for child in self.children:
                if isinstance(child, discord.ui.Select) and child.values:
                    cid = str(child.values[0])
                    break
        except Exception:
            cid = ""
        if not cid:
            await _ephemeral(interaction, "⚠️ Character not found.")
            return
        # Find character payload
        chars = self.pack_payload.get("characters") or []
        target = None
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, dict) and normalize_style_id(str(c.get("id") or c.get("style_id") or "")) == normalize_style_id(cid):
                    target = c
                    break
        if not target:
            await _ephemeral(interaction, "⚠️ Character not found.")
            return
        e, f = _character_preview_embed(self.pack_payload, target)
        # Send as ephemeral followup so we can attach local files safely.
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=e, file=f, ephemeral=True) if f else await interaction.followup.send(embed=e, ephemeral=True)
            else:
                await interaction.response.send_message(embed=e, file=f, ephemeral=True) if f else await interaction.response.send_message(embed=e, ephemeral=True)
        except Exception:
            await _ephemeral(interaction, "⚠️ Failed to show preview.")

    async def _on_upvote(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self.user_id:
            await _ephemeral(interaction, "This panel isn't yours.")
            return
        ok, count, msg = await upvote_pack(int(interaction.user.id), self.pack_id)
        await _ephemeral(interaction, msg)
        if ok:
            try:
                for child in self.children:
                    if isinstance(child, discord.ui.Button) and getattr(child, "label", "") == "⬆️ Upvote":
                        child.disabled = True
                        break
                await interaction.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success, row=2)
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can change packs.")
            return
        pack = get_builtin_pack(self.pack_id) or (await get_custom_pack(self.pack_id))
        if isinstance(pack, dict) and bool(pack.get("private", False)):
            await _ephemeral(interaction, "🔒 This is a private pack. Use **/packs private_enable** with the password.")
            return
        if isinstance(pack, dict) and bool(pack.get("exclusive", False)):
            tier = await get_premium_tier(self.user_id)
            if tier != "pro":
                await _ephemeral(interaction, "🕒 This pack is **exclusive**. Upgrade this server to **Pro** to enable it.")
                return
        ok = await enable_pack_for_guild(self.guild_id, self.pack_id)
        await _ephemeral(interaction, "✅ Enabled." if ok else "⚠️ Enable failed.")

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger, row=2)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can change packs.")
            return
        ok = await disable_pack_for_guild(self.guild_id, self.pack_id)
        await _ephemeral(interaction, "✅ Disabled." if ok else "⚠️ Disable failed.")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        # Go back to the pack list
        view = PackSelectView(bot=self.bot, guild_id=self.guild_id, user_id=self.user_id, packs=self.all_packs)
        await _ephemeral(interaction, "Select a pack:", view=view)


class SlashPacks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    packs = app_commands.Group(name="packs", description="Manage rollable character packs")

    @packs.command(name="marketplace", description="Discover packs (featured, popular). Open to everyone.")
    @app_commands.describe(sort="Sort order: featured, popular, or newest")
    @app_commands.choices(sort=[
        app_commands.Choice(name="Featured first", value="featured"),
        app_commands.Choice(name="Most upvotes", value="popular"),
        app_commands.Choice(name="Newest", value="newest"),
    ])
    async def packs_marketplace(
        self,
        interaction: discord.Interaction,
        sort: app_commands.Choice[str] | None = None,
    ):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        sort_val = (sort.value if sort else "featured").lower()
        packs = await list_packs_for_marketplace(sort_by=sort_val, limit=25)
        if not packs:
            await _ephemeral(interaction, "No community packs yet. Create one with `/packs create` (Pro required).")
            return
        view = PackSelectView(
            bot=self.bot,
            guild_id=int(interaction.guild.id),
            user_id=int(interaction.user.id),
            packs=packs,
        )
        header = "📦 **Pack Marketplace** — Select a pack to view details and upvote."
        if sort_val == "featured":
            header += "\n*Featured packs shown first.*"
        elif sort_val == "popular":
            header += "\n*Sorted by upvotes.*"
        await _ephemeral(interaction, header, view=view)

    @packs.command(name="upvote", description="Upvote a pack (one vote per user)")
    @app_commands.describe(pack_id="Pack to upvote")
    @app_commands.autocomplete(pack_id=_ac_pack_any)
    async def packs_upvote(self, interaction: discord.Interaction, pack_id: str):
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        pack = get_builtin_pack(pid) or (await get_custom_pack(pid))
        if not pack:
            await _ephemeral(interaction, "Pack not found.")
            return
        if str(pack.get("created_by") or "") == "builtin" or pid == "nardologybot":
            await _ephemeral(interaction, "You can't upvote the built-in pack.")
            return
        if bool(pack.get("private", False)):
            await _ephemeral(interaction, "Private packs can't be upvoted.")
            return
        ok, count, msg = await upvote_pack(int(interaction.user.id), pid)
        await _ephemeral(interaction, msg)

    @packs.command(name="leaderboard", description="Top pack creators by upvotes")
    async def packs_leaderboard(self, interaction: discord.Interaction):
        leaders = await get_creator_leaderboard(limit=10)
        if not leaders:
            await _ephemeral(interaction, "No creators yet. Create a pack with `/packs create` (Pro required).")
            return
        lines = []
        for i, c in enumerate(leaders, 1):
            uid = c.get("user_id") or 0
            gid = c.get("guild_id") or 0
            if uid:
                label = f"<@{uid}>"
            else:
                label = f"Guild `{gid}`"
            lines.append(f"**{i}.** {label} — {c['packs']} pack(s), {c['chars']} chars, ⬆️ {c['upvotes']}")
        await _ephemeral(interaction, "**🏆 Top Pack Creators** (by upvotes)\n\n" + "\n".join(lines)[:1900])

    @packs.command(name="browse", description="Browse available packs and enable/disable them")
    async def packs_browse(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can manage packs.")
            return

        builtin = list_builtin_packs()
        custom = await list_packs_for_marketplace(sort_by="featured", limit=50)
        packs = builtin + custom
        if not packs:
            await _ephemeral(interaction, "No packs found.")
            return
        view = PackSelectView(bot=self.bot, guild_id=int(interaction.guild.id), user_id=int(interaction.user.id), packs=packs)
        await _ephemeral(interaction, "Select a pack:", view=view)

    @packs.command(name="enabled", description="Show packs enabled in this server")
    async def packs_enabled(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        enabled = await get_enabled_pack_ids(int(interaction.guild.id))
        lines = [f"• `{pid}`" for pid in sorted(enabled)]
        await _ephemeral(interaction, "**Enabled packs:**\n" + ("\n".join(lines) if lines else "(none)"))

    @packs.command(name="enable", description="Enable a pack by id")
    @app_commands.describe(pack_id="Pack id (use /packs browse)")
    @app_commands.autocomplete(pack_id=_ac_pack_any)
    async def packs_enable(self, interaction: discord.Interaction, pack_id: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can manage packs.")
            return
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        # The per-guild server-only pseudo pack does not exist as a stored pack record,
        # but we still allow it to be enabled/disabled.
        if pid == f"server_{int(interaction.guild.id)}":
            ok = await enable_pack_for_guild(int(interaction.guild.id), pid)
            await _ephemeral(interaction, "✅ Enabled." if ok else "⚠️ Enable failed.")
            return

        pack = get_builtin_pack(pid) or await get_custom_pack(pid)
        # confirm pack exists
        if not pack:
            await _ephemeral(interaction, "Pack not found. Use /packs browse.")
            return
        if isinstance(pack, dict) and bool(pack.get("private", False)):
            await _ephemeral(interaction, "🔒 This is a private pack. Use **/packs private_enable** with the password.")
            return
        # Phase 5: exclusive packs require Pro
        if isinstance(pack, dict) and bool(pack.get("exclusive", False)):
            tier = await get_premium_tier(int(interaction.user.id))
            if tier != "pro":
                await _ephemeral(
                    interaction,
                    "🕒 This pack is **exclusive**. Upgrade this server to **Pro** to enable it.",
                )
                return
        ok = await enable_pack_for_guild(int(interaction.guild.id), pid)
        await _ephemeral(interaction, "✅ Enabled." if ok else "⚠️ Enable failed.")

    @packs.command(name="private_enable", description="Enable a private pack (requires password)")
    @app_commands.describe(pack_id="Private pack id (no autocomplete)", password="Pack password")
    async def packs_private_enable(self, interaction: discord.Interaction, pack_id: str, password: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can manage packs.")
            return
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        pack = await get_custom_pack(pid)
        if not pack or not isinstance(pack, dict):
            await _ephemeral(interaction, "Private pack not found.")
            return
        if not bool(pack.get("private", False)):
            await _ephemeral(interaction, "That pack is not private. Use **/packs enable**.")
            return
        salt = str(pack.get("password_salt") or "")
        hsh = str(pack.get("password_hash") or "")
        if not salt or not hsh:
            await _ephemeral(interaction, "⚠️ This private pack is misconfigured (no password set).")
            return
        if not verify_pack_password(password or "", salt_hex=salt, hash_hex=hsh):
            await _ephemeral(interaction, "❌ Wrong password.")
            return
        # Phase 5: exclusive packs require Pro
        if bool(pack.get("exclusive", False)):
            tier = await get_premium_tier(int(interaction.user.id))
            if tier != "pro":
                await _ephemeral(
                    interaction,
                    "🕒 This pack is **exclusive**. Upgrade this server to **Pro** to enable it.",
                )
                return
        ok = await enable_pack_for_guild(int(interaction.guild.id), pid)
        await _ephemeral(interaction, "✅ Enabled." if ok else "⚠️ Enable failed.")

    @packs.command(name="disable", description="Disable a pack by id")
    @app_commands.describe(pack_id="Pack id")
    @app_commands.autocomplete(pack_id=_ac_pack_enabled)
    async def packs_disable(self, interaction: discord.Interaction, pack_id: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can manage packs.")
            return
        pid = normalize_pack_id(pack_id)
        ok = await disable_pack_for_guild(int(interaction.guild.id), pid)
        await _ephemeral(interaction, "✅ Disabled." if ok else "⚠️ Disable failed.")

    # -----------------------------
    # Premium-only global packs
    # -----------------------------

    @packs.command(name="create", description="(Premium) Create a new global pack")
    @app_commands.describe(
        pack_id="Short id, e.g. winter_pack",
        name="Display name",
        description="Pack description",
        private="Hide from browse/enable (requires password)",
        password="Required if private=True",
        image="(Optional) Upload a cover image for this pack",
        image_url="(Optional) Direct https image/gif URL for pack cover",
    )
    async def packs_create(
        self,
        interaction: discord.Interaction,
        pack_id: str,
        name: str,
        description: str,
        private: bool = False,
        password: str | None = None,
        image: discord.Attachment | None = None,
        image_url: str | None = None,
    ):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can create packs.")
            return
        tier = await get_premium_tier(int(interaction.user.id))
        if tier != "pro" and not _is_unlimited(interaction):
            await _ephemeral(interaction, "This requires **Pro** for this server.")
            return

        # Monetization guard: non-bot-owners have slot limits.
        if not _is_unlimited(interaction):
            try:
                all_packs = await list_custom_packs()
                owned_count = sum(1 for p in all_packs if _pack_owned_by_this_server(interaction, p))
                if owned_count >= int(config.MAX_CUSTOM_PACKS_PER_GUILD):
                    await _ephemeral(interaction, "⚠️ " + _guild_limits_msg())
                    return
            except Exception:
                pass
        pid = normalize_pack_id(pack_id)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return
        if get_builtin_pack(pid):
            await _ephemeral(interaction, "That pack id is reserved.")
            return
        existing = await get_custom_pack(pid)
        if existing:
            await _ephemeral(interaction, "A pack with that id already exists.")
            return

        pw_salt: str | None = None
        pw_hash: str | None = None
        if bool(private):
            if not (isinstance(password, str) and password.strip()):
                await _ephemeral(interaction, "⚠️ Private packs require a password.")
                return
            pw_salt, pw_hash = hash_pack_password(password.strip())

        payload = {
            "type": "pack",
            "pack_id": pid,
            "name": str(name or pid)[:64],
            "description": str(description or "").strip()[:800],
            "created_by_guild": int(interaction.guild.id),
            "created_by_user": int(interaction.user.id),
            # New: packs created by premium users are COMMUNITY by default.
            "official": False,
            "exclusive": False,
            "characters": [],
            "private": bool(private),
            "nsfw": False,
            "password_salt": pw_salt,
            "password_hash": pw_hash,
        }

        # Pack cover image (optional)
        if image is not None:
            max_bytes = 20 * 1024 * 1024 if _is_unlimited(interaction) else None
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=image,
                rel_dir=f"packs/{pid}",
                basename="pack",
                max_bytes=max_bytes,
                upscale_min_px=1024,
            )
            if not ok_img:
                await _ephemeral(interaction, f"⚠️ {msg_img}")
                return
            if rel:
                payload["image_url"] = rel if rel.startswith("http") else f"asset:{rel}"
        elif isinstance(image_url, str) and image_url.strip():
            u = image_url.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                await _ephemeral(interaction, "⚠️ image_url must start with http:// or https://")
                return
            payload["image_url"] = u

        # Verification system: queue for review (unless bot owner or auto-approved by trust)
        gid = int(interaction.guild.id)
        uid = int(interaction.user.id)
        ok, msg, ticket_id = await create_verification_ticket(
            ticket_type="pack_create",
            guild_id=gid,
            user_id=uid,
            payload=payload,
        )
        if not ok:
            await _ephemeral(interaction, f"⚠️ {msg}")
            return

        ticket = await get_ticket(ticket_id) if ticket_id else {}
        status = str(ticket.get("status") or "pending")
        auto_approved = bool(ticket.get("auto_approved", False))

        if status == "auto_approved":
            # Trusted creator: save immediately
            ok_save = await upsert_custom_pack(payload)
            if ok_save:
                await increment_trust_approval(guild_id=gid, user_id=uid)
                await _ephemeral(interaction, f"✅ Pack created (auto-approved via trust score).")
            else:
                await _ephemeral(interaction, "⚠️ Failed to create pack.")
        else:
            # Pending verification: notify owner via DM
            await _notify_owner_verification_request(
                bot=self.bot,
                ticket_id=ticket_id or "",
                ticket_type="pack_create",
                guild_id=gid,
                user_id=uid,
                payload=payload,
            )
            await _ephemeral(
                interaction,
                f"⏳ Pack creation submitted for verification (ticket `{ticket_id or 'unknown'}`). "
                f"You'll be notified when it's reviewed.",
            )

    @packs.command(name="delete", description="(Premium) Delete a global pack you created")
    @app_commands.describe(pack_id="Pack id")
    @app_commands.autocomplete(pack_id=_ac_pack_custom_owned)
    async def packs_delete(self, interaction: discord.Interaction, pack_id: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can delete packs.")
            return
        tier = await get_premium_tier(int(interaction.user.id))
        if tier != "pro":
            await _ephemeral(interaction, "This requires **Pro** for this server.")
            return
        pid = normalize_pack_id(pack_id)
        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found (or it's built-in).")
            return
        if not _can_manage_custom_pack(interaction, p):
            if isinstance(p, dict) and bool(p.get("private", False)):
                await _ephemeral(interaction, "🔒 This is a private pack owned by another user.")
            else:
                await _ephemeral(interaction, "You can only delete packs created by this server (or legacy packs you created).")
            return
        ok = await delete_custom_pack(pid)
        await _ephemeral(interaction, "✅ Pack deleted." if ok else "⚠️ Delete failed.")

    @packs.command(name="edit", description="(Premium) Edit a global pack you created")
    @app_commands.describe(
        pack_id="Pack id",
        name="New display name (optional)",
        description="New pack description (optional)",
        private="(Optional) Make pack private/public",
        password="(Optional) Set/rotate password (only used if private=True)",
        image="(Optional) Upload a new cover image for the pack",
        image_url="(Optional) Direct https image/gif URL for pack cover",
    )
    @app_commands.autocomplete(pack_id=_ac_pack_custom_owned)
    async def packs_edit(
        self,
        interaction: discord.Interaction,
        pack_id: str,
        name: str | None = None,
        description: str | None = None,
        private: bool | None = None,
        password: str | None = None,
        image: discord.Attachment | None = None,
        image_url: str | None = None,
    ):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can edit packs.")
            return
        tier = await get_premium_tier(int(interaction.user.id))
        if tier != "pro":
            await _ephemeral(interaction, "This requires **Pro** for this server.")
            return

        pid = normalize_pack_id(pack_id)
        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found (or it's built-in).")
            return
        if not _can_manage_custom_pack(interaction, p):
            if isinstance(p, dict) and bool(p.get("private", False)):
                await _ephemeral(interaction, "🔒 This is a private pack owned by another user.")
            else:
                await _ephemeral(interaction, "You can only edit packs created by this server (or legacy packs you created).")
            return

        changed = False
        if isinstance(name, str) and name.strip():
            p["name"] = name.strip()[:64]
            changed = True
        if isinstance(description, str):
            d = description.strip()
            if d:
                p["description"] = d[:800]
                changed = True

        if private is not None:
            p["private"] = bool(private)
            if bool(private):
                # If making private and no prior password, require a password.
                if not (p.get("password_hash") and p.get("password_salt")) and not (password or "").strip():
                    await _ephemeral(interaction, "⚠️ Private packs require a password (set password=...).")
                    return
            else:
                # If making public, clear password.
                p.pop("password_hash", None)
                p.pop("password_salt", None)
            changed = True

        if isinstance(password, str) and password.strip():
            if not bool(p.get("private", False)):
                await _ephemeral(interaction, "⚠️ Set private=True to use a password.")
                return
            salt, hsh = hash_pack_password(password.strip())
            p["password_salt"] = salt
            p["password_hash"] = hsh
            changed = True

        # Pack cover image
        if image is not None:
            max_bytes = 20 * 1024 * 1024 if _is_unlimited(interaction) else None
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=image,
                rel_dir=f"packs/{pid}",
                basename="pack",
                max_bytes=max_bytes,
                upscale_min_px=1024,
            )
            if not ok_img:
                await _ephemeral(interaction, f"⚠️ {msg_img}")
                return
            if rel:
                p["image_url"] = rel if rel.startswith("http") else f"asset:{rel}"
                changed = True
        elif isinstance(image_url, str) and image_url.strip():
            u = image_url.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                await _ephemeral(interaction, "⚠️ image_url must start with http:// or https://")
                return
            p["image_url"] = u
            changed = True

        if not changed:
            await _ephemeral(interaction, "Nothing to update.")
            return

        # Store original pack before modifications (deep copy)
        import copy
        original_payload = copy.deepcopy(p)

        # Apply modifications to create new payload
        new_payload = copy.deepcopy(p)

        # Verification system: queue for review (unless bot owner or auto-approved by trust)
        gid = int(interaction.guild.id)
        uid = int(interaction.user.id)
        ok, msg, ticket_id = await create_verification_ticket(
            ticket_type="pack_edit",
            guild_id=gid,
            user_id=uid,
            payload=new_payload,
            original_payload=original_payload,
        )
        if not ok:
            await _ephemeral(interaction, f"⚠️ {msg}")
            return

        ticket = await get_ticket(ticket_id) if ticket_id else {}
        status = str(ticket.get("status") or "pending")
        auto_approved = bool(ticket.get("auto_approved", False))

        if status == "auto_approved":
            # Trusted creator: save immediately
            ok_save = await upsert_custom_pack(new_payload)
            if ok_save:
                await increment_trust_approval(guild_id=gid, user_id=uid)
                # Update runtime registry
                try:
                    merge_pack_payload(new_payload)
                except Exception:
                    pass
                await _ephemeral(interaction, "✅ Pack updated (auto-approved via trust score).")
            else:
                await _ephemeral(interaction, "⚠️ Update failed.")
        else:
            # Pending verification: notify owner via DM
            await _notify_owner_verification_request(
                bot=self.bot,
                ticket_id=ticket_id or "",
                ticket_type="pack_edit",
                guild_id=gid,
                user_id=uid,
                payload=new_payload,
            )
            await _ephemeral(
                interaction,
                f"⏳ Pack edit submitted for verification (ticket `{ticket_id or 'unknown'}`). "
                f"You'll be notified when it's reviewed.",
            )

    @packs.command(name="character_add", description="(Premium) Add or replace a character in a pack")
    @app_commands.describe(
        pack_id="Pack id",
        character_id="Unique character id, e.g. snow_wizard",
        display_name="Display name",
        rarity="common/uncommon/rare/legendary/mythic",
        description="Short description (must be original, no copyrighted/real-person content)",
        prompt="Personality/system prompt (must be original, no copyrighted/real-person content)",
        max_bond_cap="Optional max bond cap (default: no cap)",
        image="(Optional) Upload an image/GIF for this character",
        # Optional emotion/bond images (uploaded and stored like `image`).
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
        image_url="(Optional) Direct https image/gif URL instead of uploading",
        traits="Optional comma-separated tags/traits",
    )
    @app_commands.autocomplete(pack_id=_ac_pack_custom_owned, rarity=_ac_rarity, max_bond_cap=_ac_bond_cap)
    async def packs_character_add(
        self,
        interaction: discord.Interaction,
        pack_id: str,
        character_id: str,
        display_name: str,
        rarity: str,
        description: str,
        prompt: str,
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
        image_url: str | None = None,
        traits: str | None = None,
    ):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can edit packs.")
            return
        tier = await get_premium_tier(int(interaction.user.id))
        if tier != "pro":
            await _ephemeral(interaction, "This requires **Pro** for this server.")
            return

        raw_pid = str(pack_id or "").strip()
        # "This Server" / packless option removed - packless characters are now shop-only
        if raw_pid.lower().strip() in {"server", "none"}:
            await _ephemeral(interaction, "Packless characters are no longer available. Use `/owner shop character_create` for shop-only characters, or add characters to a pack.")
            return

        cid = normalize_style_id(character_id)
        if not cid:
            await _ephemeral(interaction, "Invalid character id.")
            return

        p: dict[str, Any] | None = None
        pid = normalize_pack_id(raw_pid)
        if not pid:
            await _ephemeral(interaction, "Invalid pack id.")
            return

        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found (must be a custom pack).")
            return
        if not _can_manage_custom_pack(interaction, p):
            if isinstance(p, dict) and bool(p.get("private", False)):
                await _ephemeral(interaction, "🔒 This is a private pack owned by another user.")
            else:
                await _ephemeral(interaction, "You can only edit packs created by this server (or legacy packs you created).")
            return

        # Slot limits (non-bot-owners)
        if not _is_unlimited(interaction):
            try:
                chars = p.get("characters") or []  # type: ignore[union-attr]
                if isinstance(chars, list) and len(chars) >= int(config.MAX_CUSTOM_CHARS_PER_PACK):
                    # If we're replacing an existing id, allow it.
                    existing_ids = {
                        normalize_style_id(str(c.get("id") or c.get("style_id") or ""))
                        for c in chars
                        if isinstance(c, dict)
                    }
                    if cid not in existing_ids:
                        await _ephemeral(interaction, "⚠️ " + _guild_limits_msg())
                        return
                # Total chars across all packs created by this guild
                all_packs = await list_custom_packs()
                total = 0
                for pk in all_packs:
                    if not _pack_owned_by_this_server(interaction, pk):
                        continue
                    c2 = pk.get("characters") or []
                    if isinstance(c2, list):
                        total += len(c2)
                if total >= int(config.MAX_CUSTOM_CHARS_TOTAL_PER_GUILD):
                    await _ephemeral(interaction, "⚠️ " + _guild_limits_msg())
                    return
            except Exception:
                pass

        tags = [t.strip() for t in (traits or "").split(",") if t.strip()]

        # ---- Copyright / identity protection screening ----
        from utils.copyright_filter import check_copyright_blocklist, ai_copyright_screen

        cr_reason = check_copyright_blocklist(
            display_name=str(display_name or ""),
            character_id=cid,
            description=str(description or ""),
            prompt=str(prompt or ""),
        )
        if cr_reason:
            await _ephemeral(
                interaction,
                f"🚫 **Character rejected** — {cr_reason}\n\n"
                "Custom characters must be entirely original and not based on any real person, "
                "public figure, copyrighted character, or trademarked property.",
            )
            return

        ai_flagged, ai_reason = await ai_copyright_screen(
            display_name=str(display_name or ""),
            description=str(description or ""),
            prompt=str(prompt or ""),
        )
        if ai_flagged:
            await _ephemeral(
                interaction,
                f"🚫 **Character rejected** — {ai_reason}\n\n"
                "Custom characters must be entirely original and not based on any real person, "
                "public figure, copyrighted character, or trademarked property.",
            )
            return

        image_ref: str | None = None

        # Optional per-character emotion + bond images.
        # These are stored on the character definition so /talk can embed them.
        emotion_images: dict[str, str] = {}
        bond_images: list[str] = []

        # Owners can upload larger assets; everyone else uses the env cap (default 2MB).
        max_bytes = 20 * 1024 * 1024 if _is_unlimited(interaction) else None

        async def _save_img(att: discord.Attachment, rel_dir: str, basename: str) -> str | None:
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=att,
                rel_dir=rel_dir,
                basename=basename,
                max_bytes=max_bytes,
                upscale_min_px=1024,
            )
            if not ok_img:
                await _ephemeral(interaction, f"⚠️ {msg_img}")
                return None
            if not rel:
                return None
            # save_attachment_image may return a local relative path OR a public URL (if ASSET_STORAGE_MODE=s3)
            return rel if rel.startswith("http") else f"asset:{rel}"
        # Prefer an uploaded file; otherwise allow a direct URL.
        if image is not None:
            image_ref = await _save_img(image, f"packs/{pid}", cid)
            if image_ref is None:
                return
        elif image_url is not None and str(image_url).strip():
            u = str(image_url).strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                await _ephemeral(interaction, "⚠️ image_url must start with http:// or https://")
                return
            image_ref = u

        # Emotion images (optional)
        emotion_inputs: dict[str, discord.Attachment | None] = {
            "neutral": emotion_neutral,
            "happy": emotion_happy,
            "sad": emotion_sad,
            "mad": emotion_mad,
            "confused": emotion_confused,
            "excited": emotion_excited,
            "affectionate": emotion_affectionate,
        }
        for key, att in emotion_inputs.items():
            if att is None:
                continue
            ref = await _save_img(att, f"packs/{pid}/{cid}/emotions", key)
            if ref is None:
                return
            emotion_images[key] = ref

        # Bond images (optional) - ordered tier 1..5
        bond_atts = [bond1, bond2, bond3, bond4, bond5]
        for idx, att in enumerate(bond_atts, start=1):
            if att is None:
                continue
            ref = await _save_img(att, f"packs/{pid}/{cid}/bonds", f"bond{idx}")
            if ref is None:
                return
            # Ensure list length up to idx
            while len(bond_images) < idx:
                bond_images.append("")
            bond_images[idx - 1] = ref
        # Drop any empty placeholders
        bond_images = [x for x in bond_images if x]

        cap_level: int | None = _parse_bond_cap(max_bond_cap)

        # Check if character already exists (edit vs create)
        original_char: Dict[str, Any] | None = None
        is_edit = False
        chars_list = p.get("characters") or []
        if isinstance(chars_list, list):
            for existing in chars_list:
                if isinstance(existing, dict):
                    existing_id = normalize_style_id(str(existing.get("id") or existing.get("style_id") or ""))
                    if existing_id == cid:
                        import copy
                        original_char = copy.deepcopy(existing)
                        is_edit = True
                        break

        char = {
            "type": "character",
            "id": cid,
            "style_id": cid,
            "pack_id": pid,
            "display_name": str(display_name or cid)[:64],
            "rarity": str(rarity or "common").lower().strip(),
            "color": "#5865F2",
            "description": str(description or "").strip()[:400],
            "prompt": str(prompt or "").strip()[:6000],
            "image_url": image_ref,
            # Optional emotion/bond images (used by /talk for per-character rendering)
            "emotion_images": emotion_images or None,
            "bond_images": bond_images or None,
            "tags": tags,
            "rollable": True,
            "max_bond_level": cap_level,
        }

        # Verification system: queue for review (unless bot owner or auto-approved by trust)
        gid = int(interaction.guild.id)
        uid = int(interaction.user.id)
        ticket_type = "character_edit" if is_edit else "character_add"
        ok, msg, ticket_id = await create_verification_ticket(
            ticket_type=ticket_type,
            guild_id=gid,
            user_id=uid,
            payload=char,
            original_payload=original_char if is_edit else None,
        )
        if not ok:
            await _ephemeral(interaction, f"⚠️ {msg}")
            return

        ticket = await get_ticket(ticket_id) if ticket_id else {}
        status = str(ticket.get("status") or "pending")
        auto_approved = bool(ticket.get("auto_approved", False))

        if status == "auto_approved":
            # Trusted creator: save immediately
            ok_save, msg_save = await add_character_to_pack(pid, char)
            if ok_save:
                await increment_trust_approval(guild_id=gid, user_id=uid)
                # Merge into runtime registry so it becomes rollable immediately.
                p2 = await get_custom_pack(pid)
                if p2:
                    merge_pack_payload(p2)
                await _ephemeral(
                    interaction,
                    f"✅ Saved character **{char['display_name']}** (`{cid}`) in `{pid}` (auto-approved via trust score).\n\n"
                    "By submitting this character you agree that it is entirely original and not based on "
                    "any real person, public figure, copyrighted character, trademarked property, or protected "
                    "intellectual property. Content violating this policy will be removed without notice.",
                )
            else:
                await _ephemeral(interaction, f"⚠️ {msg_save}")
        else:
            # Pending verification: notify owner via DM
            await _notify_owner_verification_request(
                bot=self.bot,
                ticket_id=ticket_id or "",
                ticket_type=ticket_type,
                guild_id=gid,
                user_id=uid,
                payload=char,
            )
            action_text = "edit" if is_edit else "creation"
            await _ephemeral(
                interaction,
                f"⏳ Character {action_text} submitted for verification (ticket `{ticket_id or 'unknown'}`). "
                f"You'll be notified when it's reviewed.\n\n"
                "By submitting this character you agree that it is entirely original and not based on "
                "any real person, public figure, copyrighted character, trademarked property, or protected "
                "intellectual property. Content violating this policy will be removed without notice. "
                "Repeat violations may result in account restrictions.",
            )

    @packs.command(name="server_characters", description="List server-only (no pack) characters")
    async def packs_server_characters(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        chars = await list_server_chars(int(interaction.guild.id))
        if not chars:
            await _ephemeral(interaction, "No server-only characters yet.")
            return
        lines = []
        for c in chars[:25]:
            if not isinstance(c, dict):
                continue
            pid = str(c.get("public_id") or "")
            dn = str(c.get("display_name") or pid)
            r = str(c.get("rarity") or "common").lower()
            emoji = RARITY_EMOJI.get(r, "❔")
            lines.append(f"• {emoji} **{dn}** (`{pid}`)")
        extra = "\n…(more hidden)" if len(chars) > 25 else ""
        await _ephemeral(interaction, "**Server-only characters:**\n" + "\n".join(lines) + extra)

    @packs.command(name="server_character_edit", description="Edit a server-only (no pack) character")
    @app_commands.describe(
        character_id="Server character id",
        display_name="(Optional) New display name",
        rarity="(Optional) New rarity",
        description="(Optional) New description",
        prompt="(Optional) New prompt/personality",
        max_bond_cap="(Optional) New max bond cap",
        image="(Optional) Upload a new image/GIF",
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
        traits="(Optional) New comma-separated tags/traits",
    )
    @app_commands.autocomplete(character_id=_ac_server_character, rarity=_ac_rarity, max_bond_cap=_ac_bond_cap)
    async def packs_server_character_edit(
        self,
        interaction: discord.Interaction,
        character_id: str,
        display_name: str | None = None,
        rarity: str | None = None,
        description: str | None = None,
        prompt: str | None = None,
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
        image_url: str | None = None,
        traits: str | None = None,
    ):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can edit server-only characters.")
            return

        gid = int(interaction.guild.id)
        cid = normalize_style_id(character_id)
        if not cid:
            await _ephemeral(interaction, "Invalid character id.")
            return

        chars = await list_server_chars(gid)
        target = None
        for c in chars:
            if isinstance(c, dict) and normalize_style_id(str(c.get("public_id") or "")) == cid:
                target = c
                break
        if not target:
            await _ephemeral(interaction, "Character not found.")
            return

        if isinstance(display_name, str) and display_name.strip():
            target["display_name"] = display_name.strip()[:64]
        if isinstance(rarity, str) and rarity.strip():
            target["rarity"] = rarity.strip().lower()
        if isinstance(description, str) and description.strip():
            target["description"] = description.strip()[:400]
        if isinstance(prompt, str) and prompt.strip():
            target["prompt"] = prompt.strip()[:6000]
        if traits is not None:
            target["tags"] = [t.strip() for t in (traits or "").split(",") if t.strip()]
        if max_bond_cap is not None:
            target["max_bond_level"] = _parse_bond_cap(max_bond_cap)

        # Owners can upload larger assets; everyone else uses the env cap (default 2MB).
        pid = f"server_{gid}"
        max_bytes = 20 * 1024 * 1024 if _is_unlimited(interaction) else None

        async def _save_img(att: discord.Attachment, rel_dir: str, basename: str) -> str | None:
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=att,
                rel_dir=rel_dir,
                basename=basename,
                max_bytes=max_bytes,
                upscale_min_px=1024,
            )
            if not ok_img:
                await _ephemeral(interaction, f"⚠️ {msg_img}")
                return None
            if not rel:
                return None
            return rel if rel.startswith("http") else f"asset:{rel}"

        # Main image
        if image is not None:
            ref = await _save_img(image, f"packs/{pid}", cid)
            if ref is None:
                return
            target["image_url"] = ref
        elif image_url is not None and str(image_url).strip():
            u = str(image_url).strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                await _ephemeral(interaction, "⚠️ image_url must start with http:// or https://")
                return
            target["image_url"] = u

        # Emotion images (merge into existing)
        emotion_inputs: dict[str, discord.Attachment | None] = {
            "neutral": emotion_neutral,
            "happy": emotion_happy,
            "sad": emotion_sad,
            "mad": emotion_mad,
            "confused": emotion_confused,
            "excited": emotion_excited,
            "affectionate": emotion_affectionate,
        }
        existing_emotions: dict[str, str] = target.get("emotion_images") or {}
        if not isinstance(existing_emotions, dict):
            existing_emotions = {}
        emotions_changed = False
        for key, att in emotion_inputs.items():
            if att is None:
                continue
            ref = await _save_img(att, f"packs/{pid}/{cid}/emotions", key)
            if ref is None:
                return
            existing_emotions[key] = ref
            emotions_changed = True
        if emotions_changed:
            target["emotion_images"] = existing_emotions

        # Bond images (merge into existing)
        bond_atts = [bond1, bond2, bond3, bond4, bond5]
        existing_bonds: list[str] = target.get("bond_images") or []
        if not isinstance(existing_bonds, list):
            existing_bonds = []
        bonds_changed = False
        for idx, att in enumerate(bond_atts, start=1):
            if att is None:
                continue
            ref = await _save_img(att, f"packs/{pid}/{cid}/bonds", f"bond{idx}")
            if ref is None:
                return
            while len(existing_bonds) < idx:
                existing_bonds.append("")
            existing_bonds[idx - 1] = ref
            bonds_changed = True
        if bonds_changed:
            target["bond_images"] = existing_bonds

        # Ensure internal id is correct
        target["public_id"] = cid
        target["id"] = make_internal_id(gid, cid)
        target["style_id"] = target["id"]

        ok = await upsert_server_char(gid, target)
        if ok:
            try:
                chars_now = await list_server_chars(gid)
                merge_pack_payload(to_pack_payload(gid, chars_now))
            except Exception:
                pass
        await _ephemeral(interaction, "✅ Updated." if ok else "⚠️ Update failed.")

    @packs.command(name="server_character_remove", description="Remove a server-only (no pack) character")
    @app_commands.describe(character_id="Server character id")
    @app_commands.autocomplete(character_id=_ac_server_character)
    async def packs_server_character_remove(self, interaction: discord.Interaction, character_id: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can remove server-only characters.")
            return
        gid = int(interaction.guild.id)
        cid = normalize_style_id(character_id)
        internal_id = make_internal_id(gid, cid)
        ok = await remove_server_char(gid, internal_id)
        if ok:
            try:
                chars_now = await list_server_chars(gid)
                merge_pack_payload(to_pack_payload(gid, chars_now))
            except Exception:
                pass
        await _ephemeral(interaction, "✅ Removed." if ok else "⚠️ Not found.")

    @packs.command(name="character_edit", description="(Premium) Edit an existing character in a pack")
    @app_commands.describe(
        pack_id="Pack id",
        character_id="Character id",
        display_name="(Optional) New display name",
        rarity="(Optional) New rarity",
        max_bond_cap="(Optional) New max bond cap (title cap)",
        description="(Optional) New description",
        prompt="(Optional) New prompt/personality",
        image="(Optional) Upload a new image/GIF",
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
        image_url="(Optional) Direct https image/gif URL instead of uploading",
        traits="(Optional) New comma-separated tags/traits (replaces existing)",
    )
    @app_commands.autocomplete(
        pack_id=_ac_pack_custom_owned,
        character_id=_ac_character_in_selected_pack,
        rarity=_ac_rarity,
        max_bond_cap=_ac_bond_cap,
    )
    async def packs_character_edit(
        self,
        interaction: discord.Interaction,
        pack_id: str,
        character_id: str,
        display_name: str | None = None,
        rarity: str | None = None,
        max_bond_cap: str | None = None,
        description: str | None = None,
        prompt: str | None = None,
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
    ):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can edit packs.")
            return
        tier = await get_premium_tier(int(interaction.user.id))
        if tier != "pro":
            await _ephemeral(interaction, "This requires **Pro** for this server.")
            return

        pid = normalize_pack_id(pack_id)
        cid = normalize_style_id(character_id)
        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found.")
            return
        if not _can_manage_custom_pack(interaction, p):
            if isinstance(p, dict) and bool(p.get("private", False)):
                await _ephemeral(interaction, "🔒 This is a private pack owned by another user.")
            else:
                await _ephemeral(interaction, "You can only edit packs created by this server (or legacy packs you created).")
            return

        chars = p.get("characters") or []
        if not isinstance(chars, list):
            await _ephemeral(interaction, "Pack is corrupted (characters is not a list).")
            return

        target: dict[str, Any] | None = None
        for c in chars:
            if not isinstance(c, dict):
                continue
            sid = normalize_style_id(str(c.get("id") or c.get("style_id") or ""))
            if sid == cid:
                target = c
                break
        if not target:
            await _ephemeral(interaction, "Character not found in that pack.")
            return

        # Apply updates
        if display_name is not None and str(display_name).strip():
            target["display_name"] = str(display_name).strip()[:64]
        if rarity is not None and str(rarity).strip():
            target["rarity"] = str(rarity).lower().strip()
        if max_bond_cap is not None:
            target["max_bond_level"] = _parse_bond_cap(max_bond_cap)
        if description is not None:
            target["description"] = str(description).strip()[:400]
        if prompt is not None:
            target["prompt"] = str(prompt).strip()[:6000]
        if traits is not None:
            target["tags"] = [t.strip() for t in str(traits).split(",") if t.strip()]

        # Owners can upload larger assets; everyone else uses the env cap (default 2MB).
        max_bytes = 20 * 1024 * 1024 if _is_unlimited(interaction) else None

        async def _save_img(att: discord.Attachment, rel_dir: str, basename: str) -> str | None:
            ok_img, msg_img, rel = await save_attachment_image(
                attachment=att,
                rel_dir=rel_dir,
                basename=basename,
                max_bytes=max_bytes,
                upscale_min_px=1024,
            )
            if not ok_img:
                await _ephemeral(interaction, f"⚠️ {msg_img}")
                return None
            if not rel:
                return None
            return rel if rel.startswith("http") else f"asset:{rel}"

        # Main image: prefer uploaded file, otherwise allow a direct URL.
        if image is not None:
            ref = await _save_img(image, f"packs/{pid}", cid)
            if ref is None:
                return
            target["image_url"] = ref
        elif image_url is not None and str(image_url).strip():
            u = str(image_url).strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                await _ephemeral(interaction, "⚠️ image_url must start with http:// or https://")
                return
            target["image_url"] = u

        # Emotion images (merge into existing)
        emotion_inputs: dict[str, discord.Attachment | None] = {
            "neutral": emotion_neutral,
            "happy": emotion_happy,
            "sad": emotion_sad,
            "mad": emotion_mad,
            "confused": emotion_confused,
            "excited": emotion_excited,
            "affectionate": emotion_affectionate,
        }
        existing_emotions: dict[str, str] = target.get("emotion_images") or {}
        if not isinstance(existing_emotions, dict):
            existing_emotions = {}
        emotions_changed = False
        for key, att in emotion_inputs.items():
            if att is None:
                continue
            ref = await _save_img(att, f"packs/{pid}/{cid}/emotions", key)
            if ref is None:
                return
            existing_emotions[key] = ref
            emotions_changed = True
        if emotions_changed:
            target["emotion_images"] = existing_emotions

        # Bond images (merge into existing)
        bond_atts = [bond1, bond2, bond3, bond4, bond5]
        existing_bonds: list[str] = target.get("bond_images") or []
        if not isinstance(existing_bonds, list):
            existing_bonds = []
        bonds_changed = False
        for idx, att in enumerate(bond_atts, start=1):
            if att is None:
                continue
            ref = await _save_img(att, f"packs/{pid}/{cid}/bonds", f"bond{idx}")
            if ref is None:
                return
            # Ensure list length up to idx
            while len(existing_bonds) < idx:
                existing_bonds.append("")
            existing_bonds[idx - 1] = ref
            bonds_changed = True
        if bonds_changed:
            target["bond_images"] = existing_bonds

        # Save back
        p["characters"] = chars
        ok = await upsert_custom_pack(p)
        if not ok:
            await _ephemeral(interaction, "⚠️ Failed saving pack.")
            return

        merge_pack_payload(p)
        await _ephemeral(interaction, f"✅ Updated **{target.get('display_name') or cid}** (`{cid}`) in `{pid}`.")

    @packs.command(name="character_remove", description="(Premium) Remove a character from a pack")
    @app_commands.describe(pack_id="Pack id", character_id="Character id")
    @app_commands.autocomplete(pack_id=_ac_pack_custom_owned, character_id=_ac_character_in_selected_pack)
    async def packs_character_remove(self, interaction: discord.Interaction, pack_id: str, character_id: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return
        if not _is_guild_owner_or_admin(interaction):
            await _ephemeral(interaction, "Only the server owner/admin can edit packs.")
            return
        tier = await get_premium_tier(int(interaction.user.id))
        if tier != "pro":
            await _ephemeral(interaction, "This requires **Pro** for this server.")
            return
        pid = normalize_pack_id(pack_id)
        cid = normalize_style_id(character_id)
        p = await get_custom_pack(pid)
        if not p:
            await _ephemeral(interaction, "Pack not found.")
            return
        if not _can_manage_custom_pack(interaction, p):
            if isinstance(p, dict) and bool(p.get("private", False)):
                await _ephemeral(interaction, "🔒 This is a private pack owned by another user.")
            else:
                await _ephemeral(interaction, "You can only edit packs created by this server (or legacy packs you created).")
            return
        ok = await remove_character_from_pack(pid, cid)
        await _ephemeral(interaction, "✅ Removed." if ok else "⚠️ Remove failed.")


async def setup(bot: commands.Bot):
    # On startup, merge all custom packs into the runtime registry.
    # include_shop_only + include_internal ensure packadmin-created limited
    # packs and direct-buy "shop singles" characters are loaded too.
    try:
        for p in await list_custom_packs(include_shop_only=True, include_internal=True):
            merge_pack_payload(p)
    except Exception:
        pass

    await bot.add_cog(SlashPacks(bot))
