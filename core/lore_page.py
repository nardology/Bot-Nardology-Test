"""Lore page: public HTML page showing world and character lore, plus suggestion API.

Serves ``GET /lore`` (public, no auth) and ``POST /api/lore/suggest`` (rate-limited).
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any

import config

log = logging.getLogger("lore_page")

_bot = None
_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"

# Simple in-memory rate limiter for suggestions (IP -> list of timestamps)
_suggest_timestamps: dict[str, list[float]] = {}
_SUGGEST_MAX_PER_DAY = 3
_SUGGEST_WINDOW_S = 86400


def _render_template(filename: str, inject_data: dict) -> str:
    path = _TEMPLATE_DIR / filename
    html = path.read_text(encoding="utf-8")
    inject_js = f"window.__DATA__ = {json.dumps(inject_data, default=str)};"
    return html.replace("/*__INJECT__*/", inject_js)


def _check_suggest_rate(ip: str) -> bool:
    """Return True if the IP is allowed to submit (under daily limit)."""
    now = time.time()
    cutoff = now - _SUGGEST_WINDOW_S
    timestamps = _suggest_timestamps.get(ip, [])
    timestamps = [t for t in timestamps if t > cutoff]
    _suggest_timestamps[ip] = timestamps
    return len(timestamps) < _SUGGEST_MAX_PER_DAY


def _record_suggest(ip: str) -> None:
    timestamps = _suggest_timestamps.setdefault(ip, [])
    timestamps.append(time.time())


def _gather_worlds() -> list[dict[str, Any]]:
    from utils.world_lore import get_all_worlds

    worlds_raw = get_all_worlds()
    result = []
    for wid, w in worlds_raw.items():
        result.append({
            "id": wid,
            "name": w.get("name", wid),
            "type": w.get("type", ""),
            "description": w.get("description", ""),
            "key_facts": w.get("key_facts", []),
            "regions": w.get("regions", []),
            "parent_world": w.get("parent_world"),
        })
    return result


def _gather_characters() -> list[dict[str, Any]]:
    from utils.character_registry import STYLE_DEFS

    lore_fields = [
        "display_name", "rarity", "description", "backstory",
        "personality_traits", "quirks", "speech_style",
        "fears", "desires", "likes", "dislikes",
        "catchphrases", "secrets", "lore", "age", "occupation",
        "relationships", "world", "original_world",
        "image_url", "tags",
    ]

    result = []
    for sid, style in STYLE_DEFS.items():
        pack = getattr(style, "pack_id", "core") or "core"
        if pack not in ("nardologybot", "core"):
            continue
        if not getattr(style, "rollable", True):
            continue

        entry: dict[str, Any] = {"style_id": sid}
        for field in lore_fields:
            val = getattr(style, field, None)
            if val is not None:
                if hasattr(val, "value"):
                    val = val.value
                entry[field] = val
        result.append(entry)

    result.sort(key=lambda c: c.get("display_name", ""))
    return result


async def handle_lore_page(request):
    from aiohttp import web

    worlds = _gather_worlds()
    characters = _gather_characters()

    html = _render_template("lore.html", {
        "worlds": worlds,
        "characters": characters,
    })
    return web.Response(text=html, content_type="text/html")


async def handle_lore_suggest(request):
    from aiohttp import web

    ip = request.remote or "unknown"
    if not _check_suggest_rate(ip):
        return web.json_response(
            {"ok": False, "error": "Rate limit reached (3 suggestions per day). Try again tomorrow."},
            status=429,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON."}, status=400)

    username = str(body.get("username", "") or "").strip()[:100]
    subject_type = str(body.get("subject_type", "") or "").strip()[:50]
    subject_name = str(body.get("subject_name", "") or "").strip()[:100]
    suggestion = str(body.get("suggestion", "") or "").strip()[:2000]

    if not username or not subject_type or not suggestion:
        return web.json_response(
            {"ok": False, "error": "Please fill in all required fields."},
            status=400,
        )

    _record_suggest(ip)

    # DM bot owners
    if _bot and config.BOT_OWNER_IDS:
        import discord

        embed = discord.Embed(
            title="Lore Suggestion",
            color=0x9B59B6,
        )
        embed.add_field(name="From", value=username, inline=True)
        embed.add_field(name="Type", value=subject_type, inline=True)
        if subject_name:
            embed.add_field(name="Subject", value=subject_name, inline=True)
        embed.add_field(name="Suggestion", value=suggestion[:1024], inline=False)
        if len(suggestion) > 1024:
            embed.add_field(name="(continued)", value=suggestion[1024:2048], inline=False)

        for owner_id in config.BOT_OWNER_IDS:
            try:
                user = _bot.get_user(int(owner_id)) or await _bot.fetch_user(int(owner_id))
                if user:
                    await user.send(embed=embed)
            except Exception:
                log.debug("Failed to DM owner %s for lore suggestion", owner_id)

    return web.json_response({"ok": True})


def register_routes(app, bot) -> None:
    global _bot
    _bot = bot

    app.router.add_get("/lore", handle_lore_page)
    app.router.add_post("/api/lore/suggest", handle_lore_suggest)
    log.info("Lore page routes registered")
