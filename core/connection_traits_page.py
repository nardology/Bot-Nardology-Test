"""Connection traits dashboard: Discord OAuth + aiohttp JSON API (same origin as HTML)."""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

import config
from utils.connection_traits_catalog import list_enabled_traits
from core.recommendations import generate_token, verify_token
from utils.character_store import load_state
from utils.connection_traits_store import (
    get_shard_balance,
    load_profile,
    purchase_trait,
    update_payload_fields,
    price_for_trait_purchase,
)

log = logging.getLogger("connection_traits_page")

_bot = None
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

COOKIE_SESSION = "cn_session"
COOKIE_OAUTH_STATE = "cn_oauth_state"


def _redirect_uri() -> str:
    return (config.CONNECTION_OAUTH_REDIRECT_URI or "").strip()


def _public_base() -> str:
    b = (config.BASE_URL or "").strip().rstrip("/")
    return b or "http://localhost:8080"


def _session_user(request: web.Request) -> int | None:
    raw = request.cookies.get(COOKIE_SESSION, "").strip()
    if not raw:
        return None
    uid = verify_token(raw, "connection_web")
    return uid if uid is not None else None


def _html_shell(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
body{{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f0f12;color:#e8e8ef;max-width:900px;margin:0 auto;padding:24px;}}
a{{color:#7c89ff;}} .card{{background:#18181f;border:1px solid #2a2a35;border-radius:12px;padding:16px;margin:12px 0;}}
button{{background:#5865f2;color:#fff;border:none;padding:10px 16px;border-radius:8px;cursor:pointer;}}
button:disabled{{opacity:0.5;cursor:not-allowed;}}
label{{display:block;margin:8px 0 4px;color:#b8b8c8;}}
input,textarea,select{{width:100%;max-width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333;background:#101018;color:#eee;}}
small{{color:#888;}}
</style></head><body>{body}</body></html>"""


async def handle_connection_landing(request: web.Request) -> web.Response:
    uid = _session_user(request)
    if uid is None:
        login_url = f"{_public_base()}/connection/login"
        body = f"<h1>Connection traits</h1><p>Log in with Discord to manage traits per character.</p><p><a href=\"{login_url}\"><button type=\"button\">Login with Discord</button></a></p>"
        return web.Response(text=_html_shell("Connection traits", body), content_type="text/html")
    raise web.HTTPFound(location=f"{_public_base()}/connection/app")


async def handle_connection_login(request: web.Request) -> web.Response:
    cid = config.CONNECTION_OAUTH_CLIENT_ID
    redir = _redirect_uri()
    if not cid or not redir:
        return web.Response(
            text=_html_shell(
                "Connection traits",
                "<h1>OAuth not configured</h1><p>Set CONNECTION_OAUTH_CLIENT_ID and CONNECTION_OAUTH_REDIRECT_URI (or DISCORD_CLIENT_ID + BASE_URL).</p>",
            ),
            content_type="text/html",
            status=501,
        )
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": cid,
        "redirect_uri": redir,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",
    }
    url = "https://discord.com/api/oauth2/authorize?" + urlencode(params)
    resp = web.HTTPFound(location=url)
    resp.set_cookie(
        COOKIE_OAUTH_STATE,
        state,
        httponly=True,
        secure=config.ENVIRONMENT != "dev",
        samesite="Lax",
        max_age=600,
        path="/",
    )
    return resp


async def handle_oauth_callback(request: web.Request) -> web.Response:
    err = request.query.get("error")
    if err:
        return web.Response(text=_html_shell("Login error", f"<h1>OAuth error</h1><p>{err}</p>"), content_type="text/html", status=400)
    code = request.query.get("code", "").strip()
    state = request.query.get("state", "").strip()
    cookie_state = request.cookies.get(COOKIE_OAUTH_STATE, "").strip()
    if not code or not state or state != cookie_state:
        return web.Response(
            text=_html_shell("Login error", "<h1>Invalid OAuth state</h1><p>Try again from /connection.</p>"),
            content_type="text/html",
            status=400,
        )
    cid = config.CONNECTION_OAUTH_CLIENT_ID
    secret = config.CONNECTION_OAUTH_CLIENT_SECRET
    redir = _redirect_uri()
    if not cid or not secret or not redir:
        return web.Response(text="OAuth not configured", status=501)

    import aiohttp

    token_data = {
        "client_id": cid,
        "client_secret": secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redir,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://discord.com/api/oauth2/token",
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as tr:
                td = await tr.json()
                if tr.status >= 400:
                    log.warning("Discord token error: %s", td)
                    return web.Response(text="Token exchange failed", status=502)
                access = (td or {}).get("access_token")
                if not access:
                    return web.Response(text="No access_token", status=502)
            async with session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access}"},
            ) as ur:
                me = await ur.json()
                if ur.status >= 400:
                    return web.Response(text="User fetch failed", status=502)
                discord_id = int(me.get("id", 0))
    except Exception:
        log.exception("OAuth callback failed")
        return web.Response(text="OAuth failed", status=502)

    if discord_id <= 0:
        return web.Response(text="Bad user", status=400)

    sess_tok = generate_token(discord_id, "connection_web")
    resp = web.HTTPFound(location=f"{_public_base()}/connection/app")
    resp.del_cookie(COOKIE_OAUTH_STATE, path="/")
    resp.set_cookie(
        COOKIE_SESSION,
        sess_tok,
        httponly=True,
        secure=config.ENVIRONMENT != "dev",
        samesite="Lax",
        max_age=30 * 24 * 3600,
        path="/",
    )
    return resp


async def handle_connection_app(request: web.Request) -> web.Response:
    uid = _session_user(request)
    if uid is None:
        raise web.HTTPFound(location=f"{_public_base()}/connection")
    path = _TEMPLATE_DIR / "connection_traits.html"
    if not path.exists():
        return web.Response(text=_html_shell("Connection traits", "<p>Template missing.</p>"), content_type="text/html", status=500)
    html = path.read_text(encoding="utf-8")
    inject = f"window.__PUBLIC_BASE__ = {json.dumps(_public_base())}; window.__UID__ = {int(uid)};"
    if "/*__INJECT__*/" in html:
        html = html.replace("/*__INJECT__*/", inject)
    else:
        html = html.replace("</head>", f"<script>{inject}</script></head>", 1)
    return web.Response(text=html, content_type="text/html")


async def handle_api_state(request: web.Request) -> web.Response:
    uid = _session_user(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    style_id = (request.query.get("style_id") or "").strip().lower()
    st = await load_state(uid)
    owned = [x for x in (getattr(st, "owned_custom", None) or []) if x and x != "fun"]
    traits = [
        {
            "trait_id": t.trait_id,
            "title": t.title,
            "description": t.description,
            "base_shard_cost": t.base_shard_cost,
        }
        for t in list_enabled_traits()
    ]
    if not style_id:
        return web.json_response(
            {
                "shard_balance": await get_shard_balance(uid),
                "owned_characters": sorted(owned),
                "traits": traits,
                "purchased": {},
                "payload": {},
            }
        )
    data = await load_profile(user_id=uid, style_id=style_id)
    purchased = dict(data.get("purchased") or {})
    prices: dict[str, Any] = {}
    for t in list_enabled_traits():
        tid = t.trait_id
        c0, _ = price_for_trait_purchase(tid, purchased, kind=None)
        prices[tid] = {"base": c0}
    h_exp = int((purchased.get("hobbies") or {}).get("slot_expansions") or 0)
    s_exp = int((purchased.get("speech_style") or {}).get("word_expansions") or 0)
    hs, _ = price_for_trait_purchase("hobbies", purchased, kind="hobby_slot")
    ss, _ = price_for_trait_purchase("speech_style", purchased, kind="speech_expand")
    ne, _ = price_for_trait_purchase("remember_name", purchased, kind="remember_name_edit")

    return web.json_response(
        {
            "shard_balance": await get_shard_balance(uid),
            "owned_characters": sorted(owned),
            "style_id": style_id,
            "traits": traits,
            "purchased": purchased,
            "payload": data.get("payload") or {},
            "prices": prices,
            "upgrade_prices": {
                "hobby_slot": hs,
                "speech_expand": ss,
                "name_edit": ne,
                "hobby_slots_total": 3 + h_exp,
                "speech_words_cap": 150 + 100 * s_exp,
            },
        }
    )


async def handle_api_purchase(request: web.Request) -> web.Response:
    uid = _session_user(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    style_id = (body.get("style_id") or "").strip().lower()
    trait_id = (body.get("trait_id") or "").strip().lower()
    kind = body.get("kind")
    kind = (kind or None) if kind in (None, "hobby_slot", "speech_expand", "remember_name_edit") else None
    ok, msg = await purchase_trait(user_id=uid, style_id=style_id, trait_id=trait_id, kind=kind)
    if not ok:
        return web.json_response({"error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg})


async def handle_api_update(request: web.Request) -> web.Response:
    uid = _session_user(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    style_id = (body.get("style_id") or "").strip().lower()
    fields = body.get("fields") or {}
    if not isinstance(fields, dict):
        return web.json_response({"error": "fields must be object"}, status=400)
    ok, msg = await update_payload_fields(user_id=uid, style_id=style_id, fields=fields)
    if not ok:
        return web.json_response({"error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg})


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound(location=f"{_public_base()}/connection")
    resp.del_cookie(COOKIE_SESSION, path="/")
    return resp


def register_routes(app: web.Application, bot) -> None:
    global _bot
    _bot = bot
    app.router.add_get("/connection", handle_connection_landing)
    app.router.add_get("/connection/login", handle_connection_login)
    app.router.add_get("/connection/oauth/callback", handle_oauth_callback)
    app.router.add_get("/connection/app", handle_connection_app)
    app.router.add_get("/connection/api/state", handle_api_state)
    app.router.add_post("/connection/api/purchase", handle_api_purchase)
    app.router.add_post("/connection/api/update", handle_api_update)
    app.router.add_get("/connection/logout", handle_logout)
