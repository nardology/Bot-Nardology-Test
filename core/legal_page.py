"""Serve Terms of Service and Privacy Policy at /terms and /privacy (Railway/same origin)."""
from __future__ import annotations

import logging
import pathlib

from aiohttp import web

log = logging.getLogger("legal_page")

_ROOT = pathlib.Path(__file__).resolve().parent.parent


async def _serve_html(request: web.Request, filename: str) -> web.Response:
    path = _ROOT / filename
    if not path.is_file():
        log.warning("Legal page not found: %s", path)
        raise web.HTTPNotFound(text="Page not found.")
    html = path.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def handle_terms(request: web.Request) -> web.Response:
    return await _serve_html(request, "TOS.html")


async def handle_privacy(request: web.Request) -> web.Response:
    return await _serve_html(request, "privacy_policy.html")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/terms", handle_terms)
    app.router.add_get("/privacy", handle_privacy)
    log.info("Legal page routes registered at /terms and /privacy")
