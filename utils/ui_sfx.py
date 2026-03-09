from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord

from utils.character_emotion_manifest import ASSETS_UI_BASE

logger = logging.getLogger("bot.ui_sfx")

_CACHE_DIR = Path("/tmp/ui_sfx_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def ui_asset_url(filename: str) -> str:
    fn = (filename or "").strip().lstrip("/")
    return f"{ASSETS_UI_BASE}/{fn}" if fn else ""

async def _ensure_cached(filename: str) -> Path | None:
    """Download a UI wav to local disk so FFmpeg can play it reliably."""
    fn = (filename or "").strip()
    if not fn:
        return None
    out = _CACHE_DIR / fn
    if out.exists() and out.stat().st_size > 0:
        return out

    try:
        import httpx  # local import (optional dependency)
    except Exception:
        logger.warning("httpx not available; cannot download UI sound %s", fn)
        return None

    url = ui_asset_url(fn)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r.content)
            return out
    except Exception:
        logger.exception("Failed to download UI sound: %s", url)
        return None

async def play_ui_sound(
    interaction: discord.Interaction,
    filename: str,
    *,
    disconnect_after: bool = True,
) -> None:
    """Play a UI .wav in the invoking user's current voice channel (if any).

    - If the user is not in VC, does nothing.
    - If the bot is already connected to the same VC and is playing something,
      we do not interrupt.
    """
    try:
        if not interaction.guild:
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member or not member.voice or not member.voice.channel:
            return

        path = await _ensure_cached(filename)
        if path is None:
            return

        channel = member.voice.channel
        vc = interaction.guild.voice_client

        created = False
        if vc and vc.is_connected():
            if vc.channel and vc.channel.id != channel.id:
                # don't jump channels automatically
                return
        else:
            vc = await channel.connect()
            created = True

        if vc.is_playing():
            # avoid audio collisions
            if created and disconnect_after:
                try:
                    await vc.disconnect()
                except Exception:
                    pass
            return

        src = discord.FFmpegPCMAudio(str(path))
        vc.play(src)

        while vc.is_playing():
            await asyncio.sleep(0.1)

        if created and disconnect_after:
            try:
                await vc.disconnect()
            except Exception:
                pass
    except Exception:
        logger.exception("play_ui_sound failed")
