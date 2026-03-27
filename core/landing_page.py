"""Landing page: public HTML page showcasing the bot for visitors, investors, and collaborators."""
from __future__ import annotations

import logging
import pathlib

log = logging.getLogger("landing_page")

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"


async def handle_landing(request):
    from aiohttp import web

    path = _TEMPLATE_DIR / "landing.html"
    html = path.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def handle_y2k_event(request):
    from aiohttp import web

    path = _TEMPLATE_DIR / "event_y2k_frutiger.html"
    html = path.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


def register_routes(app) -> None:
    app.router.add_get("/", handle_landing)
    app.router.add_get("/events/y2k", handle_y2k_event)
    log.info("Landing page route registered at /")
