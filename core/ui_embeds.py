"""core/ui_embeds.py

Standard embed builders used across commands.

Phase 1 goal:
- Provide a single, consistent style for success/warn/error/info responses.
- Keep UI decisions out of individual commands.

Embeds are intentionally minimal (title + description + footer).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

import config


def _brand_name() -> str:
    return str(getattr(config, "BOT_NAME", "Bot-Nardology") or "Bot-Nardology")


def _base_embed(*, title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=description)
    try:
        e.timestamp = datetime.now(timezone.utc)
    except Exception:
        pass
    try:
        e.set_footer(text=_brand_name())
    except Exception:
        pass
    return e


def success(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return _base_embed(title=title or "✅ Success", description=description)


def error(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return _base_embed(title=title or "❌ Error", description=description)


def warning(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return _base_embed(title=title or "⚠️ Warning", description=description)


def info(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return _base_embed(title=title or "ℹ️ Info", description=description)
