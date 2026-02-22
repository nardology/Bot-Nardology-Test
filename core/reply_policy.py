# core/reply_policy.py
from __future__ import annotations

import discord
from core.entitlements import get_entitlements


async def should_ephemeral_ai_reply(interaction: discord.Interaction) -> bool:
    """Whether AI replies should be ephemeral for this interaction (tier policy)."""
    guild_id = interaction.guild_id or 0
    ent = await get_entitlements(user_id=interaction.user.id, guild_id=guild_id)
    return ent.ai_private_only
