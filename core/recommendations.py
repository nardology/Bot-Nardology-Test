"""Character recommendation system: token auth, DB helpers, JSON API handlers, DM notifications.

HTML pages are hosted on Netlify (static). This module provides only JSON API
endpoints that the Netlify pages call via fetch(). CORS is handled per-response.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import config

log = logging.getLogger("recommendations")

_bot = None
_TOKEN_MAX_AGE_S = 30 * 24 * 3600  # 30 days

_ALLOWED_ORIGINS: set[str] | None = None


def _get_allowed_origins() -> set[str]:
    global _ALLOWED_ORIGINS
    if _ALLOWED_ORIGINS is not None:
        return _ALLOWED_ORIGINS
    origins: set[str] = set()
    for url in (config.RECOMMEND_FORM_URL, config.RECOMMEND_REVIEW_URL, config.RECOMMEND_DASHBOARD_URL):
        if url:
            origins.add(url.rstrip("/"))
    if config.ENVIRONMENT == "dev":
        origins.add("http://localhost:5500")
        origins.add("http://127.0.0.1:5500")
    _ALLOWED_ORIGINS = origins
    log.info("CORS allowed origins: %s", origins)
    return origins


# ---------------------------------------------------------------------------
# CORS middleware (applied to all /api/recommend responses automatically)
# ---------------------------------------------------------------------------

def _match_origin(request) -> str | None:
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if not origin:
        return None
    allowed = _get_allowed_origins()
    if origin in allowed:
        return origin
    return None


def _set_cors_headers(response, origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "86400"


def make_cors_middleware():
    """Return aiohttp middleware that adds CORS headers to every /api/ response."""
    from aiohttp import web

    @web.middleware
    async def cors_middleware(request, handler):
        origin = _match_origin(request)

        if request.method == "OPTIONS" and origin and request.path.startswith("/api/"):
            resp = web.Response(status=204)
            _set_cors_headers(resp, origin)
            return resp

        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc
        except Exception:
            log.exception("Unhandled error in %s %s", request.method, request.path)
            resp = web.json_response({"error": "Internal server error"}, status=500)

        if origin and request.path.startswith("/api/"):
            _set_cors_headers(resp, origin)
        return resp

    return cors_middleware


# ---------------------------------------------------------------------------
# HMAC token helpers
# ---------------------------------------------------------------------------

def _secret() -> bytes:
    return (config.DISCORD_TOKEN or "fallback-dev-key").encode()


def generate_token(user_id: int, purpose: str) -> str:
    payload = json.dumps({"uid": user_id, "p": purpose, "ts": int(time.time())}, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(_secret(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_token(token: str, expected_purpose: str) -> int | None:
    """Return user_id if valid, else None."""
    try:
        b64, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(_secret(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64))
        if payload.get("p") != expected_purpose:
            return None
        ts = int(payload.get("ts", 0))
        if time.time() - ts > _TOKEN_MAX_AGE_S:
            return None
        return int(payload["uid"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def get_pending_recommendation(user_id: int):
    """Return the user's most recent pending/viewed recommendation, or None."""
    try:
        from sqlalchemy import select
        from utils.db import get_sessionmaker
        from utils.models import CharacterRecommendation
    except Exception:
        return None

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterRecommendation)
            .where(CharacterRecommendation.user_id == user_id)
            .where(CharacterRecommendation.status.in_(["pending", "viewed"]))
            .order_by(CharacterRecommendation.created_at.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()


async def get_recommendation_by_id(rec_id: int):
    try:
        from utils.db import get_sessionmaker
        from utils.models import CharacterRecommendation
    except Exception:
        return None

    Session = get_sessionmaker()
    async with Session() as session:
        return await session.get(CharacterRecommendation, rec_id)


async def save_recommendation(user_id: int, data: dict) -> int:
    """Insert or update a recommendation. Returns the recommendation ID."""
    from sqlalchemy import select
    from utils.db import get_sessionmaker
    from utils.models import CharacterRecommendation

    json_fields = {
        "tips", "personality_traits", "quirks", "fears", "desires",
        "likes", "dislikes", "catchphrases", "secrets", "tags",
        "relationships", "topic_reactions",
    }

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(CharacterRecommendation)
            .where(CharacterRecommendation.user_id == user_id)
            .where(CharacterRecommendation.status.in_(["pending", "viewed"]))
            .order_by(CharacterRecommendation.created_at.desc())
            .limit(1)
        )
        rec = res.scalar_one_or_none()

        if rec is None:
            rec = CharacterRecommendation(user_id=user_id)
            session.add(rec)

        for key, val in data.items():
            if not hasattr(rec, key) or key in ("id", "user_id", "created_at"):
                continue
            if key in json_fields and isinstance(val, (list, dict)):
                setattr(rec, key, json.dumps(val))
            else:
                setattr(rec, key, val)

        rec.status = "pending"
        rec.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(rec)
        return rec.id


async def update_recommendation_status(rec_id: int, status: str, reviewer_notes: str | None = None) -> bool:
    from utils.db import get_sessionmaker
    from utils.models import CharacterRecommendation

    Session = get_sessionmaker()
    async with Session() as session:
        rec = await session.get(CharacterRecommendation, rec_id)
        if rec is None:
            return False
        rec.status = status
        rec.updated_at = datetime.now(timezone.utc)
        if reviewer_notes is not None:
            rec.reviewer_notes = reviewer_notes
        await session.commit()
        return True


async def list_recommendations(status_filter: str | None = None) -> list[dict]:
    from sqlalchemy import select
    from utils.db import get_sessionmaker
    from utils.models import CharacterRecommendation

    Session = get_sessionmaker()
    async with Session() as session:
        q = select(CharacterRecommendation).order_by(CharacterRecommendation.created_at.desc())
        if status_filter and status_filter != "all":
            q = q.where(CharacterRecommendation.status == status_filter)
        res = await session.execute(q.limit(200))
        rows = res.scalars().all()
        return [_rec_to_dict(r) for r in rows]


def _rec_to_dict(rec) -> dict:
    json_fields = {
        "tips", "personality_traits", "quirks", "fears", "desires",
        "likes", "dislikes", "catchphrases", "secrets", "tags",
        "relationships", "topic_reactions",
    }
    d: dict[str, Any] = {}
    for col in rec.__table__.columns:
        val = getattr(rec, col.name, None)
        if col.name in json_fields and isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                pass
        if isinstance(val, datetime):
            val = val.isoformat()
        d[col.name] = val
    return d


# ---------------------------------------------------------------------------
# DM notifications
# ---------------------------------------------------------------------------

async def _dm_user(user_id: int, message: str) -> None:
    try:
        if _bot is None:
            return
        user = _bot.get_user(user_id) or await _bot.fetch_user(user_id)
        if user:
            await user.send(message[:2000])
    except Exception:
        log.debug("Could not DM user %s", user_id, exc_info=True)


async def _dm_owners_recommendation(rec_id: int, display_name: str, rarity: str, user_id: int) -> None:
    review_base = config.RECOMMEND_REVIEW_URL
    dash_base = config.RECOMMEND_DASHBOARD_URL
    api_base = config.BASE_URL
    if not review_base or not api_base:
        log.warning("RECOMMEND_REVIEW_URL or BASE_URL not configured; cannot generate review link")
        return
    for owner_id in sorted(config.BOT_OWNER_IDS or []):
        token = generate_token(owner_id, "review")
        url = f"{review_base}?token={token}&id={rec_id}&api={api_base}"
        dash_url = f"{dash_base}?token={token}&api={api_base}"
        msg = (
            f"**New Character Recommendation!**\n"
            f"**Character:** {display_name}\n"
            f"**Rarity:** {rarity}\n"
            f"**From:** <@{user_id}> (ID: {user_id})\n"
            f"**Review:** {url}\n"
            f"**Dashboard:** {dash_url}"
        )
        try:
            if _bot is None:
                continue
            user = _bot.get_user(owner_id) or await _bot.fetch_user(owner_id)
            if user:
                await user.send(msg[:2000])
                break
        except Exception:
            log.debug("Could not DM owner %s", owner_id, exc_info=True)


# ---------------------------------------------------------------------------
# JSON API route handlers (called by Netlify-hosted HTML pages)
# ---------------------------------------------------------------------------

async def handle_form_data(request):
    """GET /api/recommend/form-data?token=... — return existing recommendation as JSON."""
    from aiohttp import web

    token = request.query.get("token", "")
    user_id = verify_token(token, "submit")
    if user_id is None:
        return web.json_response({"error": "Invalid or expired token"}, status=403)

    existing = await get_pending_recommendation(user_id)
    existing_data = _rec_to_dict(existing) if existing else None

    return web.json_response({"ok": True, "user_id": user_id, "existing": existing_data})


async def handle_submit(request):
    """POST /api/recommend — submit or update a recommendation."""
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token = body.get("token", "")
    user_id = verify_token(token, "submit")
    if user_id is None:
        return web.json_response({"error": "Invalid or expired token"}, status=403)

    display_name = (body.get("display_name") or "").strip()
    rarity = (body.get("rarity") or "").strip().lower()

    if not display_name:
        return web.json_response({"error": "Character name is required"}, status=400)
    if rarity not in ("common", "uncommon", "rare", "legendary", "mythic"):
        return web.json_response({"error": "Invalid rarity"}, status=400)

    fields: dict[str, Any] = {
        "display_name": display_name,
        "rarity": rarity,
    }
    text_keys = [
        "color", "description", "prompt", "backstory", "speech_style",
        "lore", "age", "occupation", "image_url", "world",
        "original_world", "world_knowledge",
    ]
    for k in text_keys:
        v = body.get(k)
        if v and isinstance(v, str) and v.strip():
            fields[k] = v.strip()

    list_keys = [
        "tips", "personality_traits", "quirks", "fears", "desires",
        "likes", "dislikes", "catchphrases", "secrets", "tags",
    ]
    for k in list_keys:
        v = body.get(k)
        if isinstance(v, list):
            fields[k] = [str(i).strip() for i in v if str(i).strip()]

    dict_keys = ["relationships", "topic_reactions"]
    for k in dict_keys:
        v = body.get(k)
        if isinstance(v, dict):
            fields[k] = {str(kk).strip(): str(vv).strip() for kk, vv in v.items() if str(kk).strip()}

    try:
        rec_id = await save_recommendation(user_id, fields)
    except Exception:
        log.exception("Failed to save recommendation for user=%s", user_id)
        return web.json_response({"error": "Database error"}, status=500)

    try:
        await _dm_owners_recommendation(rec_id, display_name, rarity, user_id)
    except Exception:
        log.exception("Failed to DM owners about recommendation %s", rec_id)

    try:
        await _dm_user(user_id, f"Your character recommendation **{display_name}** has been submitted! You'll be notified when it's reviewed.")
    except Exception:
        log.debug("Could not DM user %s about submission", user_id)

    return web.json_response({"ok": True, "id": rec_id})


async def handle_review_data(request):
    """GET /api/recommend/review-data?token=...&id=... — return recommendation for owner review."""
    from aiohttp import web

    token = request.query.get("token", "")
    owner_id = verify_token(token, "review")
    if owner_id is None or owner_id not in (config.BOT_OWNER_IDS or set()):
        return web.json_response({"error": "Invalid or expired token"}, status=403)

    rec_id_str = request.query.get("id", "")
    try:
        rec_id = int(rec_id_str)
    except (ValueError, TypeError):
        return web.json_response({"error": "Missing recommendation ID"}, status=400)

    rec = await get_recommendation_by_id(rec_id)
    if rec is None:
        return web.json_response({"error": "Recommendation not found"}, status=404)

    if rec.status == "pending":
        await update_recommendation_status(rec_id, "viewed")
        try:
            await _dm_user(rec.user_id, f"Your character recommendation **{rec.display_name}** is being reviewed!")
        except Exception:
            pass

    rec_data = _rec_to_dict(rec)
    if rec_data.get("status") == "pending":
        rec_data["status"] = "viewed"

    return web.json_response({"ok": True, "rec": rec_data})


async def handle_decide(request):
    """POST /api/recommend/decide — owner accepts or denies."""
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token = body.get("token", "")
    owner_id = verify_token(token, "review")
    if owner_id is None or owner_id not in (config.BOT_OWNER_IDS or set()):
        return web.json_response({"error": "Invalid or expired token"}, status=403)

    rec_id = int(body.get("id", 0))
    decision = body.get("decision", "").strip().lower()
    notes = (body.get("notes") or "").strip()

    if decision not in ("accepted", "denied"):
        return web.json_response({"error": "Decision must be 'accepted' or 'denied'"}, status=400)
    if not rec_id:
        return web.json_response({"error": "Missing recommendation ID"}, status=400)

    rec = await get_recommendation_by_id(rec_id)
    if rec is None:
        return web.json_response({"error": "Recommendation not found"}, status=404)

    ok = await update_recommendation_status(rec_id, decision, notes or None)
    if not ok:
        return web.json_response({"error": "Failed to update"}, status=500)

    if decision == "accepted":
        msg = f"Great news! Your character recommendation **{rec.display_name}** has been **accepted**!"
        if notes:
            msg += f"\n**Notes:** {notes}"
    else:
        msg = f"Your character recommendation **{rec.display_name}** was **denied**."
        if notes:
            msg += f"\n**Reason:** {notes}"

    try:
        await _dm_user(rec.user_id, msg)
    except Exception:
        pass

    return web.json_response({"ok": True})


async def handle_list(request):
    """GET /api/recommend/list?token=...&status=... — return all recommendations for owner dashboard."""
    from aiohttp import web

    token = request.query.get("token", "")
    owner_id = verify_token(token, "review")
    if owner_id is None or owner_id not in (config.BOT_OWNER_IDS or set()):
        return web.json_response({"error": "Invalid or expired token"}, status=403)

    status_filter = request.query.get("status", "all")
    recs = await list_recommendations(status_filter)

    return web.json_response({"ok": True, "recs": recs})


def register_routes(app, bot) -> None:
    """Register all recommendation API routes on the aiohttp app."""
    global _bot
    _bot = bot

    app.router.add_get("/api/recommend/form-data", handle_form_data)
    app.router.add_post("/api/recommend", handle_submit)
    app.router.add_get("/api/recommend/review-data", handle_review_data)
    app.router.add_post("/api/recommend/decide", handle_decide)
    app.router.add_get("/api/recommend/list", handle_list)
    log.info("Recommendation API routes registered (Netlify mode)")
