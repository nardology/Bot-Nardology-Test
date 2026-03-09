# utils/permissions.py
from __future__ import annotations

import discord

from utils.storage import get_guild_settings, list_members


def _has_manage_guild(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return bool(perms.manage_guild or perms.administrator)


def _as_int_set(value) -> set[int]:
    """Convert list/set/tuple of ids (ints/strings) into a set[int]."""
    if value is None:
        return set()

    if isinstance(value, (set, tuple)):
        value = list(value)

    if not isinstance(value, list):
        return set()

    out: set[int] = set()
    for x in value:
        try:
            out.add(int(x))
        except Exception:
            continue
    return out


async def _list_ids(guild_id: int, key: str) -> set[int]:
    """Read an allow/block list stored as a Redis SET (strings) and return set[int]."""
    try:
        members = await list_members(guild_id, key)  # set[str]
    except Exception:
        members = set()

    out: set[int] = set()
    for x in members:
        try:
            out.add(int(x))
        except Exception:
            continue
    return out


async def check_ai_access(interaction: discord.Interaction) -> tuple[bool, str]:
    """Returns (allowed, reason_if_denied)."""
    if interaction.guild is None:
        return (False, "Use this command in a server, not DMs.")

    guild_id = int(interaction.guild.id)

    # Scalar settings live in the guild settings hash.
    s = await get_guild_settings(guild_id)

    if s.get("ai_enabled", True) is False:
        return (False, "AI is disabled in this server.")

    member = interaction.user
    if not isinstance(member, discord.Member):
        return (False, "Could not verify your server permissions.")

    # Block lists live in the hash in your current implementation.
    blocked_users = _as_int_set(s.get("ai_blocked_user_ids", []))
    if member.id in blocked_users:
        return (False, "You are not allowed to use AI commands in this server.")

    blocked_roles = _as_int_set(s.get("ai_blocked_role_ids", []))
    if blocked_roles:
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids.intersection(blocked_roles):
            return (False, "Your role is blocked from using AI commands in this server.")

    # Allow lists are stored as Redis SETs via utils.storage.list_add/list_members.
    allowed_channels = await _list_ids(guild_id, "ai_allowed_channel_ids")
    if allowed_channels and interaction.channel_id not in allowed_channels:
        return (False, "AI commands aren’t allowed in this channel.")

    allowed_roles = await _list_ids(guild_id, "ai_allowed_role_ids")
    if allowed_roles:
        member_role_ids = {r.id for r in member.roles}
        if not (member_role_ids.intersection(allowed_roles) or _has_manage_guild(member)):
            return (False, "You don’t have a role permitted to use AI here.")

    # Default behavior (no allow-role list configured): allow everyone.
    return (True, "")


async def check_command_channel_access(
    interaction: discord.Interaction, *, command_key: str
) -> tuple[bool, str]:
    """Per-command channel allowlist, with fallback to global AI allowlist."""
    if interaction.guild is None:
        return (False, "Use this command in a server, not DMs.")

    guild_id = int(interaction.guild.id)

    # Prefer per-command allowlist if it exists.
    per_key = f"{command_key}_allowed_channel_ids"
    per_allowed = await _list_ids(guild_id, per_key)
    if per_allowed:
        if interaction.channel_id not in per_allowed:
            return (False, f"`/{command_key}` isn’t allowed in this channel.")
        return (True, "")

    # Otherwise: global AI allowlist.
    global_allowed = await _list_ids(guild_id, "ai_allowed_channel_ids")
    if global_allowed and interaction.channel_id not in global_allowed:
        return (False, "AI commands aren’t allowed in this channel.")

    return (True, "")


def contains_mass_mention(text: str) -> bool:
    t = (text or "").lower()
    return "@everyone" in t or "@here" in t

