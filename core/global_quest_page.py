"""Public global quest page + owner editor API (same auth as admin panel)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

import config
from core.admin_panel import _require_admin_token, _get_token
log = logging.getLogger("global_quest_page")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_bot = None


def _public_base() -> str:
    return (getattr(config, "BASE_URL", "") or "").strip().rstrip("/") or "http://localhost:8080"


async def handle_global_quest_public_page(request: web.Request) -> web.Response:
    """GET /global-quest — public status page."""
    path = _TEMPLATE_DIR / "global_quest_public.html"
    if not path.exists():
        return web.Response(text="Template missing", status=500)
    html = path.read_text(encoding="utf-8")
    gid = request.query.get("guild_id", "0")
    inject = f"window.__GUILD_ID__ = {json.dumps(gid)};"
    if "/*__INJECT__*/" in html:
        html = html.replace("/*__INJECT__*/", inject)
    else:
        html = html.replace("</head>", f"<script>{inject}</script></head>", 1)
    return web.Response(text=html, content_type="text/html")


async def handle_public_global_quest_json(request: web.Request) -> web.Response:
    """GET /api/public/global-quest?guild_id=... — JSON for embeds / public page."""
    gid_s = request.query.get("guild_id", "0")
    try:
        gid = int(gid_s) if str(gid_s).strip().isdigit() else 0
    except Exception:
        gid = 0
    try:
        from utils.global_quest import build_quest_view_for_user

        v = await build_quest_view_for_user(
            guild_id=gid,
            user_id=0,
            selected_style_id=None,
        )
        if v is None:
            return web.json_response({"active": False})
        return web.json_response(
            {
                "active": True,
                "slug": v.slug,
                "title": v.title,
                "description": v.description,
                "image_url": v.image_url,
                "image_url_secondary": v.image_url_secondary,
                "scope": v.scope,
                "guild_id": v.guild_id,
                "target_training_points": v.target_training_points,
                "total_training": v.total_training,
                "guild_training": v.guild_training,
                "progress_pct": v.progress_pct,
                "days_left": v.days_left,
                "ends_at": v.ends_at.isoformat() if v.ends_at else None,
            }
        )
    except Exception as e:
        log.exception("public global quest json: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_global_quest_editor_page(request: web.Request) -> web.Response:
    """GET /global-quest/edit?token=... — owner editor."""
    owner_id, err = _require_admin_token(request, json_response=False)
    if err is not None:
        return err
    path = _TEMPLATE_DIR / "global_quest_editor.html"
    if not path.exists():
        return web.Response(text="Template missing", status=500)
    html = path.read_text(encoding="utf-8")
    token = _get_token(request) or ""
    inject = f"window.__ADMIN_TOKEN__ = {json.dumps(token)}; window.__PUBLIC_BASE__ = {json.dumps(_public_base())};"
    if "/*__INJECT__*/" in html:
        html = html.replace("/*__INJECT__*/", inject)
    else:
        html = html.replace("</head>", f"<script>{inject}</script></head>", 1)
    return web.Response(text=html, content_type="text/html")


async def handle_admin_gq_list(request: web.Request) -> web.Response:
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        from sqlalchemy import select  # type: ignore
        from utils.models import GlobalQuestEvent
        from utils.db import get_sessionmaker

        Session = get_sessionmaker()
        async with Session() as session:
            res = await session.execute(
                select(GlobalQuestEvent).order_by(GlobalQuestEvent.id.desc()).limit(50)
            )
            rows = res.scalars().all()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r.id,
                    "slug": r.slug,
                    "title": r.title,
                    "description": r.description,
                    "image_url": r.image_url,
                    "image_url_secondary": r.image_url_secondary,
                    "scope": r.scope,
                    "guild_id": r.guild_id,
                    "starts_at": r.starts_at.isoformat() if r.starts_at else None,
                    "ends_at": r.ends_at.isoformat() if r.ends_at else None,
                    "target_training_points": int(r.target_training_points or 0),
                    "character_multipliers": json.loads(r.character_multipliers_json or "{}"),
                    "status": r.status,
                    "reward_points": int(r.reward_points or 0),
                    "failure_points": int(r.failure_points or 0),
                    "success_badge_emoji": r.success_badge_emoji,
                    "success_badge_label": r.success_badge_label,
                    "grant_success_badge": bool(r.grant_success_badge),
                    "resolution_applied": bool(r.resolution_applied),
                }
            )
        return web.json_response(out)
    except Exception as e:
        log.exception("admin gq list")
        return web.json_response({"error": str(e)}, status=500)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        s = str(s).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


async def handle_admin_gq_save(request: web.Request) -> web.Response:
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    from utils.models import GlobalQuestEvent
    from utils.db import get_sessionmaker

    now = datetime.now(timezone.utc)
    slug = str(body.get("slug") or "").strip()[:64]
    title = str(body.get("title") or "Untitled")[:200]
    if not slug:
        return web.json_response({"error": "slug required"}, status=400)

    desc = str(body.get("description") or "")
    image_url = body.get("image_url") or None
    image_url_secondary = body.get("image_url_secondary") or None
    scope = str(body.get("scope") or "global").strip().lower()
    if scope not in ("global", "guild"):
        scope = "global"
    guild_id = body.get("guild_id")
    gid = int(guild_id) if guild_id is not None and str(guild_id).strip().lstrip("-").isdigit() else None
    if scope == "guild" and gid is None:
        return web.json_response({"error": "guild_id required for guild scope"}, status=400)

    starts = _parse_dt(body.get("starts_at")) or now
    ends = _parse_dt(body.get("ends_at")) or now
    target = int(body.get("target_training_points") or 100_000)
    mult = body.get("character_multipliers") or {}
    if not isinstance(mult, dict):
        mult = {}
    reward_points = int(body.get("reward_points") or 0)
    failure_points = int(body.get("failure_points") or 0)
    badge_emoji = (body.get("success_badge_emoji") or "🏆")[:16]
    badge_label = str(body.get("success_badge_label") or title)[:120]
    grant_badge = bool(body.get("grant_success_badge", True))

    event_id = body.get("id")
    Session = get_sessionmaker()
    async with Session() as session:
        if event_id:
            ev = await session.get(GlobalQuestEvent, int(event_id))
            if ev is None:
                return web.json_response({"error": "not found"}, status=404)
        else:
            ev = GlobalQuestEvent(
                slug=slug,
                title=title,
                description=desc,
                image_url=image_url,
                image_url_secondary=image_url_secondary,
                scope=scope,
                guild_id=gid,
                starts_at=starts,
                ends_at=ends,
                target_training_points=target,
                character_multipliers_json=json.dumps(mult, separators=(",", ":")),
                status="draft",
                reward_points=reward_points,
                failure_points=failure_points,
                success_badge_emoji=badge_emoji,
                success_badge_label=badge_label,
                grant_success_badge=grant_badge,
                created_at=now,
                updated_at=now,
            )
            session.add(ev)
            await session.flush()

        ev.slug = slug
        ev.title = title
        ev.description = desc
        ev.image_url = image_url
        ev.image_url_secondary = image_url_secondary
        ev.scope = scope
        ev.guild_id = gid
        ev.starts_at = starts
        ev.ends_at = ends
        ev.target_training_points = target
        ev.character_multipliers_json = json.dumps(mult, separators=(",", ":"))
        ev.reward_points = reward_points
        ev.failure_points = failure_points
        ev.success_badge_emoji = badge_emoji
        ev.success_badge_label = badge_label
        ev.grant_success_badge = grant_badge
        ev.updated_at = now
        await session.commit()
        eid = int(ev.id)
    return web.json_response({"ok": True, "id": eid})


async def handle_admin_gq_activate(request: web.Request) -> web.Response:
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        body = await request.json()
        eid = int(body.get("id") or 0)
    except Exception:
        return web.json_response({"error": "id required"}, status=400)
    if eid <= 0:
        return web.json_response({"error": "invalid id"}, status=400)

    from sqlalchemy import update  # type: ignore
    from utils.models import GlobalQuestEvent
    from utils.db import get_sessionmaker

    now = datetime.now(timezone.utc)
    Session = get_sessionmaker()
    async with Session() as session:
        await session.execute(
            update(GlobalQuestEvent)
            .where(GlobalQuestEvent.status == "active")
            .values(status="draft", updated_at=now)
        )
        ev = await session.get(GlobalQuestEvent, eid)
        if ev is None:
            await session.rollback()
            return web.json_response({"error": "not found"}, status=404)
        ev.status = "active"
        ev.updated_at = now
        await session.commit()
    return web.json_response({"ok": True})


async def handle_admin_gq_cancel(request: web.Request) -> web.Response:
    """End without rewards (draft/cancel)."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        body = await request.json()
        eid = int(body.get("id") or 0)
    except Exception:
        return web.json_response({"error": "id required"}, status=400)
    from utils.models import GlobalQuestEvent
    from utils.db import get_sessionmaker

    now = datetime.now(timezone.utc)
    Session = get_sessionmaker()
    async with Session() as session:
        ev = await session.get(GlobalQuestEvent, eid)
        if ev is None:
            return web.json_response({"error": "not found"}, status=404)
        ev.status = "cancelled"
        ev.resolution_applied = True
        ev.updated_at = now
        await session.commit()
    return web.json_response({"ok": True})


def register_routes(app: web.Application, bot) -> None:
    global _bot
    _bot = bot
    app.router.add_get("/global-quest", handle_global_quest_public_page)
    app.router.add_get("/global-quest/edit", handle_global_quest_editor_page)
    app.router.add_get("/api/public/global-quest", handle_public_global_quest_json)
    app.router.add_get("/api/admin/global-quest/events", handle_admin_gq_list)
    app.router.add_post("/api/admin/global-quest/save", handle_admin_gq_save)
    app.router.add_post("/api/admin/global-quest/activate", handle_admin_gq_activate)
    app.router.add_post("/api/admin/global-quest/cancel", handle_admin_gq_cancel)
    log.info("Global quest routes registered at /global-quest")
