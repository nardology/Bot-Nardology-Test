# core/admin_panel.py
"""Owner-only admin HTML panel: servers, limits, analytics, reports.
Auth via magic link (token from /z_owner admin_link). Reuses recommendations token helpers.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

import config
from core.recommendations import verify_token

log = logging.getLogger("admin_panel")

_bot = None
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _get_token(request: web.Request) -> str | None:
    """Extract admin token from query string or Authorization header."""
    token = request.query.get("token", "").strip()
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _require_admin_token(request: web.Request, json_response: bool = False) -> tuple[int | None, web.Response | None]:
    """Verify admin token. Returns (owner_id, None) on success or (None, error_response) on failure.
    If json_response is True, return 403 as JSON for API handlers."""
    token = _get_token(request)
    if not token:
        if json_response:
            return None, web.json_response({"error": "Missing token"}, status=403)
        return None, web.Response(
            text="<h2>Missing token.</h2><p>Use /z_owner admin_link in Discord to get a link.</p>",
            content_type="text/html",
            status=403,
        )
    owner_id = verify_token(token, "admin")
    if owner_id is None or owner_id not in (config.BOT_OWNER_IDS or set()):
        if json_response:
            return None, web.json_response({"error": "Invalid or expired token"}, status=403)
        return None, web.Response(
            text="<h2>Invalid or expired link.</h2><p>Run /z_owner admin_link in Discord for a new link.</p>",
            content_type="text/html",
            status=403,
        )
    return owner_id, None


async def handle_admin_page(request: web.Request) -> web.Response:
    """GET /admin?token=... — serve the admin dashboard HTML with token injected."""
    owner_id, err = _require_admin_token(request)
    if err is not None:
        return err
    token = _get_token(request)
    path = _TEMPLATE_DIR / "admin.html"
    html = path.read_text(encoding="utf-8")
    inject_js = f"window.__ADMIN_TOKEN__ = {json.dumps(token)};"
    if "/*__INJECT__*/" in html:
        html = html.replace("/*__INJECT__*/", inject_js)
    else:
        html = html.replace("</head>", f"<script>{inject_js}</script></head>", 1)
    return web.Response(text=html, content_type="text/html")


async def handle_api_servers(request: web.Request) -> web.Response:
    """GET /api/admin/servers?token=... — list guilds the bot is in."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    guilds = list(_bot.guilds) if _bot else []
    out = []
    for g in guilds:
        out.append({
            "id": str(g.id),
            "name": g.name,
            "member_count": getattr(g, "member_count", None),
        })
    out.sort(key=lambda x: (x["name"] or "").lower())
    return web.json_response(out)


async def handle_api_limits(request: web.Request) -> web.Response:
    """GET /api/admin/limits?token=... — read-only limit configuration."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        from utils.character_store import roll_window_seconds, ROLLS_PER_DAY_FREE, ROLLS_PER_DAY_PRO
        roll_window = roll_window_seconds()
        rolls_free, rolls_pro = ROLLS_PER_DAY_FREE, ROLLS_PER_DAY_PRO
    except Exception:
        roll_window = 18000
        rolls_free, rolls_pro = 1, 3
    limits = {
        "roll_window_seconds": roll_window,
        "roll_window_hours": round(roll_window / 3600, 1) if roll_window else 0,
        "rolls_per_day_free": rolls_free,
        "rolls_per_day_pro": rolls_pro,
        "ai_cost_cap_guild_daily_cents": getattr(config, "AI_COST_CAP_GUILD_DAILY_CENTS", 10),
        "ai_cost_cap_free_daily_cents": getattr(config, "AI_COST_CAP_FREE_DAILY_CENTS", 5),
        "ai_cost_cap_pro_daily_cents": getattr(config, "AI_COST_CAP_PRO_DAILY_CENTS", 10),
        "max_custom_packs_per_guild": getattr(config, "MAX_CUSTOM_PACKS_PER_GUILD", 3),
        "max_custom_chars_total_per_guild": getattr(config, "MAX_CUSTOM_CHARS_TOTAL_PER_GUILD", 100),
        "max_custom_chars_total_per_guild_pro": getattr(config, "MAX_CUSTOM_CHARS_TOTAL_PER_GUILD_PRO", 250),
    }
    return web.json_response(limits)


async def handle_api_analytics_overview(request: web.Request) -> web.Response:
    """GET /api/admin/analytics/overview?token=... — stickiness, AI cost, economy, churn, etc."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    guild_id = request.query.get("guild_id")
    gid = int(guild_id) if guild_id and str(guild_id).strip().isdigit() else None
    try:
        from utils.dashboard_queries import (
            get_stickiness_stats,
            get_ai_cost_stats,
            get_economy_stats,
            get_churn_stats,
            get_streak_distribution,
            get_inactive_users,
        )
        stickiness = await get_stickiness_stats(guild_id=gid)
        ai_cost = await get_ai_cost_stats(days=7, guild_id=gid)
        economy = await get_economy_stats(days=7)
        churn = await get_churn_stats()
        streaks = await get_streak_distribution()
        inactive = await get_inactive_users(limit=20)
        return web.json_response({
            "stickiness": asdict(stickiness),
            "ai_cost": asdict(ai_cost),
            "economy": asdict(economy),
            "churn": {
                "guilds_declining": churn.guilds_declining,
                "trials_ended_recently": churn.trials_ended_recently,
            },
            "streak_distribution": [asdict(b) for b in streaks],
            "inactive_users": {
                "total_at_risk": inactive.total_at_risk,
                "sample_user_ids": inactive.sample_user_ids,
            },
        })
    except Exception as e:
        log.exception("analytics overview failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_analytics_spending(request: web.Request) -> web.Response:
    """GET /api/admin/analytics/spending?token=... — AI spend by period + projections."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    series = request.query.get("series_days", "90")
    try:
        series_days = max(7, min(int(series or "90"), 365))
    except (ValueError, TypeError):
        series_days = 90
    try:
        from utils.dashboard_queries import get_spending_dashboard

        data = await get_spending_dashboard(series_days=series_days)
        return web.json_response(data)
    except Exception as e:
        log.exception("analytics spending failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_analytics_retention(request: web.Request) -> web.Response:
    """GET /api/admin/analytics/retention?token=... — retention stats."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    guild_id = request.query.get("guild_id")
    gid = int(guild_id) if guild_id and str(guild_id).strip().isdigit() else None
    cohort_days = request.query.get("cohort_days_back", "14")
    try:
        days_back = max(1, min(int(cohort_days or "14"), 30))
    except (ValueError, TypeError):
        days_back = 14
    try:
        from utils.dashboard_queries import get_retention_stats
        results = await get_retention_stats(guild_id=gid, cohort_days_back=days_back)
        return web.json_response([asdict(r) for r in results])
    except Exception as e:
        log.exception("retention failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_reports(request: web.Request) -> web.Response:
    """GET /api/admin/reports?token=... — open reports."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        from utils.reporting import get_open_reports
        reports = await get_open_reports(guild_id=None, limit=50)
        return web.json_response(reports)
    except Exception as e:
        log.exception("reports failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_abuse_flagged(request: web.Request) -> web.Response:
    """GET /api/admin/abuse/flagged?token=... — flagged/restricted user IDs and flag log."""
    _, err = _require_admin_token(request, json_response=True)
    if err is not None:
        return err
    try:
        from utils.ai_abuse import get_flagged_user_ids, get_restricted_user_ids, get_flag_log, get_prompts_for_flagged_users
        flagged = await get_flagged_user_ids()
        restricted = await get_restricted_user_ids()
        log_entries = await get_flag_log(limit=100)
        flagged_prompts = await get_prompts_for_flagged_users(flagged)
        return web.json_response({
            "flagged": [str(uid) for uid in flagged],
            "restricted": [str(uid) for uid in restricted],
            "log": log_entries,
            "flagged_prompts": flagged_prompts,
        })
    except Exception as e:
        log.exception("abuse flagged failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


def register_routes(app: web.Application, bot) -> None:
    """Register admin panel routes. Requires bot for guild list."""
    global _bot
    _bot = bot
    if not config.BOT_OWNER_IDS:
        log.warning("BOT_OWNER_IDS empty; admin panel registered but no one can access it")
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_get("/api/admin/servers", handle_api_servers)
    app.router.add_get("/api/admin/limits", handle_api_limits)
    app.router.add_get("/api/admin/analytics/overview", handle_api_analytics_overview)
    app.router.add_get("/api/admin/analytics/spending", handle_api_analytics_spending)
    app.router.add_get("/api/admin/analytics/retention", handle_api_analytics_retention)
    app.router.add_get("/api/admin/reports", handle_api_reports)
    app.router.add_get("/api/admin/abuse/flagged", handle_api_abuse_flagged)
    try:
        from utils.ai_abuse import set_bot_for_flagged_notifications
        set_bot_for_flagged_notifications(bot)
    except Exception:
        pass
    log.info("Admin panel routes registered at /admin and /api/admin/*")
