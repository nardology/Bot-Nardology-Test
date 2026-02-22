"""
core/access.py

Centralized access checks for commands.

Phase 1 goal:
  - Remove duplicated permission/channel/mass-mention gating from each command.
  - Return a consistent Decision object that commands can act on.

Phase 2 addition:
  - Determine reply visibility policy (ephemeral/private-only) via Entitlements.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

from core.entitlements import get_entitlements
from utils.permissions import (
    check_ai_access,
    check_command_channel_access,
    contains_mass_mention,
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    message: str = ""
    reason: str = ""
    retry_after_s: int = 0

    # NEW: Commands can use this to enforce "Free AI is private-only"
    ephemeral: bool = False


async def decide_ai_access(
    interaction: discord.Interaction,
    *,
    command_key: str,
    user_text: str,
) -> Decision:
    """Return whether the user may use an AI command in this context."""

    # You currently require server use. Keep that rule.
    if interaction.guild is None:
        return Decision(False, "Use this command in a server, not DMs.", reason="dm")

    guild_id = interaction.guild_id or 0
    ent = await get_entitlements(user_id=interaction.user.id, guild_id=guild_id)

    # Default policy: Free is ephemeral/private-only, Pro may be public
    # (Commands can still force ephemeral if they want.)
    ephemeral_policy = bool(ent.ai_private_only)

    # 1) Global AI enable + allow/block rules
    allowed, reason = await check_ai_access(interaction)
    if not allowed:
        return Decision(False, reason, reason="ai_access", ephemeral=ephemeral_policy)

    # 2) Per-command channel allowlist (or global fallback)
    ok_ch, ch_reason = await check_command_channel_access(interaction, command_key=command_key)
    if not ok_ch:
        return Decision(False, ch_reason, reason="channel", ephemeral=ephemeral_policy)

    # 3) Mass mention guard (non-admins)
    if contains_mass_mention(user_text or ""):
        member = interaction.user
        if isinstance(member, discord.Member):
            if not (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
                return Decision(
                    False,
                    "⚠️ Mass mentions (@everyone/@here) are blocked. Remove them and try again.",
                    reason="mass_mention",
                    ephemeral=ephemeral_policy,
                )

    return Decision(True, ephemeral=ephemeral_policy)
