from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import discord


async def safe_ephemeral_send(interaction: discord.Interaction, content: str) -> None:
    """Safely send an ephemeral message.

    Uses followups if the initial interaction response has already been used.
    Never raises.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        pass


async def safe_send(interaction: discord.Interaction, content: str, *, ephemeral: bool = False) -> None:
    """Safely send a message (ephemeral optional)."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception:
        pass


async def safe_send_embed(interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = False) -> None:
    """Safely send an embed (ephemeral optional)."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except Exception:
        pass


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> None:
    """Safely defer an interaction response."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
    except Exception:
        pass


def format_retry_after(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f" Try again in {seconds}s."
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f" Try again in {minutes}m {sec}s."
    hours, minutes = divmod(minutes, 60)
    return f" Try again in {hours}h {minutes}m."


@dataclass(frozen=True)
class UiError:
    """A structured, user-facing error."""

    message: str
    retry_after_s: Optional[int] = None

    def render(self) -> str:
        return f"⚠️ {self.message}{format_retry_after(self.retry_after_s)}"


async def send_ui_error(interaction: discord.Interaction, err: UiError) -> None:
    await safe_ephemeral_send(interaction, err.render())
