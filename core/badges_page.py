from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from utils.badges import build_badge_catalog

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


async def handle_badges_page(request: web.Request) -> web.Response:
    path = _TEMPLATE_DIR / "badges.html"
    if not path.exists():
        return web.Response(text="Badges template missing.", status=500)
    return web.Response(text=path.read_text(encoding="utf-8"), content_type="text/html")


async def handle_badges_api(_request: web.Request) -> web.Response:
    rows = await build_badge_catalog()
    return web.json_response({"badges": rows})


def register_routes(app: web.Application, _bot) -> None:
    app.router.add_get("/badges", handle_badges_page)
    app.router.add_get("/api/public/badges", handle_badges_api)
