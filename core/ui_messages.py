"""core/ui_messages.py

Small helpers for sending consistent messages/embeds.

Use these when you want your commands to look and feel the same.
"""

from __future__ import annotations

from typing import Optional

import discord

from core.ui import safe_send, safe_send_embed
from core import ui_embeds


async def send_success(
    interaction: discord.Interaction,
    description: str,
    *,
    ephemeral: bool = True,
    title: Optional[str] = None,
) -> None:
    await safe_send_embed(interaction, ui_embeds.success(description, title=title), ephemeral=ephemeral)


async def send_error(
    interaction: discord.Interaction,
    description: str,
    *,
    ephemeral: bool = True,
    title: Optional[str] = None,
) -> None:
    await safe_send_embed(interaction, ui_embeds.error(description, title=title), ephemeral=ephemeral)


async def send_warning(
    interaction: discord.Interaction,
    description: str,
    *,
    ephemeral: bool = True,
    title: Optional[str] = None,
) -> None:
    await safe_send_embed(interaction, ui_embeds.warning(description, title=title), ephemeral=ephemeral)


async def send_info(
    interaction: discord.Interaction,
    description: str,
    *,
    ephemeral: bool = True,
    title: Optional[str] = None,
) -> None:
    await safe_send_embed(interaction, ui_embeds.info(description, title=title), ephemeral=ephemeral)


async def send_text(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
) -> None:
    """Convenience wrapper when you just want consistent safe sending."""
    await safe_send(interaction, content, ephemeral=ephemeral)
