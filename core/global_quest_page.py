"""Public global quest page + owner editor API (same auth as admin panel)."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

from aiohttp import web

import config
from core.admin_panel import _require_admin_token, _get_token
log = logging.getLogger("global_quest_page")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_bot = None
_UPLOAD_MAX_BYTES = 6 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


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
        from utils.media_assets import resolve_embed_image_url

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
                "image_url": resolve_embed_image_url(v.image_url),
                "image_url_secondary": resolve_embed_image_url(v.image_url_secondary),
                "scope": v.scope,
                "guild_id": v.guild_id,
                "target_training_points": v.target_training_points,
                "total_training": v.total_training,
                "guild_training": v.guild_training,
                "progress_pct": v.progress_pct,
                "days_left": v.days_left,
                "ends_at": v.ends_at.isoformat() if v.ends_at else None,
                "activated_at": v.activated_at.isoformat() if v.activated_at else None,
            }
        )
    except Exception as e:
        log.exception("public global quest json: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_global_quest_editor_page(request: web.Request) -> web.Response:
    """GET /global-quest/edit?token=... — owner editor (iframe: ?embed=1). Standalone opens admin panel."""
    embed = str(request.query.get("embed") or "").strip().lower() in {"1", "true", "yes"}
    if not embed:
        tok = _get_token(request)
        if not tok:
            return web.Response(
                text="<h2>Missing token</h2><p>Use <strong>/z_owner admin_link</strong> or <strong>/z_owner global_quest_link</strong> in Discord.</p>",
                content_type="text/html",
                status=403,
            )
        owner_id, err = _require_admin_token(request, json_response=False)
        if err is not None:
            return err
        base = _public_base().rstrip("/")
        raise web.HTTPFound(
            location=f"{base}/admin?token={quote_plus(tok)}&panel=global-quest",
        )
    owner_id, err = _require_admin_token(request, json_response=False)
    if err is not None:
        return err
    path = _TEMPLATE_DIR / "global_quest_editor.html"
    if not path.exists():
        return web.Response(text="Template missing", status=500)
    html = path.read_text(encoding="utf-8")
    token = _get_token(request) or ""
    inject = (
        f"window.__ADMIN_TOKEN__ = {json.dumps(token)}; "
        f"window.__PUBLIC_BASE__ = {json.dumps(_public_base())}; "
        f"window.__GQ_EMBED__ = true;"
    )
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
            raw_desc = str(r.description or "")
            if len(raw_desc) > 4000:
                raw_desc = raw_desc[:4000] + "…"
            out.append(
                {
                    "id": r.id,
                    "slug": r.slug,
                    "title": r.title,
                    "description": raw_desc,
                    "image_url": r.image_url,
                    "image_url_secondary": r.image_url_secondary,
                    "scope": r.scope,
                    "guild_id": r.guild_id,
                    "starts_at": r.starts_at.isoformat() if r.starts_at else None,
                    "ends_at": r.ends_at.isoformat() if r.ends_at else None,
                    "activated_at": (
                        r.activated_at.isoformat()
                        if getattr(r, "activated_at", None) is not None
                        else None
                    ),
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


def _end_of_month_utc(now: datetime) -> datetime:
    """Return last second of current month in UTC."""
    n = now.astimezone(timezone.utc)
    if n.month == 12:
        first_next = n.replace(year=n.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        first_next = n.replace(month=n.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_next - timedelta(seconds=1)


def _clean_name(s: str) -> str:
    cleaned = _SAFE_NAME.sub("-", (s or "").strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or "image"


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

    ends = _parse_dt(body.get("ends_at")) or _end_of_month_utc(now)
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
                starts_at=now,
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
        ev.starts_at = now
        ev.activated_at = now
        ev.updated_at = now
        ends = getattr(ev, "ends_at", None)
        if ends is not None and getattr(ends, "tzinfo", None) is None:
            ends = ends.replace(tzinfo=timezone.utc)
        if ends is None or ends <= now:
            ev.ends_at = _end_of_month_utc(now)
            log.info(
                "global quest %s: extended ends_at to end-of-month (was missing or past)",
                eid,
            )
        await session.commit()
        act_iso = ev.activated_at.isoformat() if ev.activated_at else None
        ends_iso = ev.ends_at.isoformat() if ev.ends_at else None
        log.info(
            "global quest activated id=%s ends_at=%s",
            eid,
            ends_iso,
        )
    return web.json_response({"ok": True, "activated_at": act_iso, "ends_at": ends_iso})


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


async def handle_admin_gq_delete(request: web.Request) -> web.Response:
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

    from utils.models import GlobalQuestEvent
    from utils.db import get_sessionmaker

    Session = get_sessionmaker()
    async with Session() as session:
        ev = await session.get(GlobalQuestEvent, eid)
        if ev is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            await session.delete(ev)
            await session.commit()
            return web.json_response({"ok": True, "mode": "hard_delete"})
        except Exception:
            # Fallback for deployments where FK constraints block deletion:
            # keep history but hide the event from active lists/UI.
            await session.rollback()
            try:
                ev.status = "cancelled"
                ev.resolution_applied = True
                ev.updated_at = datetime.now(timezone.utc)
                await session.commit()
                log.exception("global quest delete fallback to soft-cancel id=%s", eid)
                return web.json_response({"ok": True, "mode": "soft_cancel"})
            except Exception as e:
                await session.rollback()
                log.exception("global quest delete failed id=%s", eid)
                return web.json_response({"error": str(e)}, status=500)


async def handle_admin_gq_active_debug(request: web.Request) -> web.Response:
    """Diagnostic: explain why events are/aren't active for a guild."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    gid_s = request.query.get("guild_id", "0")
    try:
        gid = int(gid_s) if str(gid_s).strip().lstrip("-").isdigit() else 0
    except Exception:
        gid = 0
    try:
        from sqlalchemy import select  # type: ignore
        from utils.models import GlobalQuestEvent
        from utils.db import get_sessionmaker

        now = datetime.now(timezone.utc)
        Session = get_sessionmaker()
        async with Session() as session:
            rows = (await session.execute(select(GlobalQuestEvent).order_by(GlobalQuestEvent.id.desc()).limit(100))).scalars().all()
        out = []
        active_ids: list[int] = []
        for ev in rows:
            status = (getattr(ev, "status", "") or "").strip().lower()
            scope = (getattr(ev, "scope", "") or "").strip().lower()
            eg = getattr(ev, "guild_id", None)
            ends = getattr(ev, "ends_at", None)
            ends_utc = None
            if ends is not None:
                ends_utc = ends.replace(tzinfo=timezone.utc) if getattr(ends, "tzinfo", None) is None else ends
            reasons: list[str] = []
            if status != "active":
                reasons.append("status_not_active")
            if ends_utc is not None and ends_utc < now:
                reasons.append("ended")
            if scope == "guild":
                if eg is None:
                    reasons.append("guild_scope_missing_guild_id")
                elif int(eg) != int(gid):
                    reasons.append(f"guild_mismatch(event={int(eg)} req={int(gid)})")
            elif scope != "global":
                reasons.append("unknown_scope")
            is_active_here = len(reasons) == 0
            if is_active_here:
                active_ids.append(int(getattr(ev, "id", 0) or 0))
            out.append(
                {
                    "id": int(getattr(ev, "id", 0) or 0),
                    "slug": str(getattr(ev, "slug", "") or ""),
                    "title": str(getattr(ev, "title", "") or ""),
                    "status": status,
                    "scope": scope,
                    "guild_id": int(eg) if eg is not None else None,
                    "ends_at": ends_utc.isoformat() if ends_utc else None,
                    "is_active_for_guild": is_active_here,
                    "reasons": reasons,
                }
            )
        return web.json_response(
            {
                "guild_id": gid,
                "now_utc": now.isoformat(),
                "active_ids": [x for x in active_ids if x > 0],
                "events": out,
            }
        )
    except Exception as e:
        log.exception("admin gq active debug failed")
        return web.json_response({"error": str(e)}, status=500)


async def handle_admin_gq_upload_image(request: web.Request) -> web.Response:
    """Upload an image for global quest editor and return a URL/path."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        reader = await request.multipart()
        part = await reader.next()
        if part is None or getattr(part, "name", "") != "file":
            return web.json_response({"error": "file field is required"}, status=400)
        data = await part.read()
        if not data:
            return web.json_response({"error": "empty file"}, status=400)
        if len(data) > _UPLOAD_MAX_BYTES:
            return web.json_response({"error": "file too large (max 6MB)"}, status=400)
        ctype = (getattr(part, "headers", {}) or {}).get("Content-Type", "")
        if not str(ctype).lower().startswith("image/"):
            return web.json_response({"error": "image file required"}, status=400)

        from utils.media_assets import (
            asset_abspath,
            asset_storage_mode,
            ensure_assets_dirs,
        )
        from utils.object_store import upload_bytes

        scope = _clean_name(str(request.query.get("scope") or "global"))
        slot = _clean_name(str(request.query.get("slot") or "primary"))
        slug = _clean_name(str(request.query.get("slug") or "event"))
        filename = _clean_name(str(getattr(part, "filename", "") or "image"))
        ext = os.path.splitext(filename)[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            ct_map = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }
            ext = ct_map.get(str(ctype).lower(), ".png")
        key_name = f"{slug}-{slot}-{int(datetime.now(timezone.utc).timestamp())}{ext}"
        rel = f"global-quest/{scope}/{key_name}"

        if asset_storage_mode() == "s3":
            ref = await upload_bytes(
                key=f"assets/{rel}",
                data=data,
                content_type=str(ctype or "image/png"),
                bucket_override=(os.getenv("ASSET_S3_BUCKET") or os.getenv("S3_BUCKET") or "").strip() or None,
                public_base_url_override=(os.getenv("ASSET_PUBLIC_BASE_URL") or "").strip() or None,
                presign_expires_s_override=(
                    int(os.getenv("ASSET_PRESIGN_EXPIRES_S", "0"))
                    if (os.getenv("ASSET_PRESIGN_EXPIRES_S", "0") or "0").isdigit()
                    else None
                ),
                public_base_url_env="ASSET_PUBLIC_BASE_URL",
                presign_expires_env="ASSET_PRESIGN_EXPIRES_S",
            )
            return web.json_response({"ok": True, "url": ref.url})

        ensure_assets_dirs()
        public_base = (os.getenv("ASSET_PUBLIC_BASE_URL") or "").strip().rstrip("/")
        if not public_base:
            return web.json_response(
                {"error": "ASSET_PUBLIC_BASE_URL is not set; configure s3 asset mode for web-visible uploads"},
                status=400,
            )
        out_abs = asset_abspath(rel)
        os.makedirs(os.path.dirname(out_abs), exist_ok=True)
        with open(out_abs, "wb") as f:
            f.write(data)
        return web.json_response({"ok": True, "url": f"{public_base}/{rel.lstrip('/')}"})
    except Exception as e:
        log.exception("global quest upload failed")
        return web.json_response({"error": str(e)}, status=500)


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
    app.router.add_post("/api/admin/global-quest/delete", handle_admin_gq_delete)
    app.router.add_post("/api/admin/global-quest/upload-image", handle_admin_gq_upload_image)
    app.router.add_get("/api/admin/global-quest/active-debug", handle_admin_gq_active_debug)
    log.info("Global quest routes registered at /global-quest")
