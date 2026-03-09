"""Analytics utilities.

Phase 2 (Product readiness) introduces:
  - fast-path counters in Redis (safe at scale)
  - periodic flush to Postgres for durable reporting

We ALSO keep a small "event log" stored in guild settings for backwards
compatibility and debugging.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict

from utils.backpressure import get_redis_or_none
from utils.db import get_engine, get_sessionmaker
from utils.models import AnalyticsDailyMetric, UserFirstSeen
from utils.storage import get_guild_setting, set_guild_setting

logger = logging.getLogger(__name__)

# NOTE: Upserts are dialect-specific in SQLAlchemy.
# - Postgres: sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update
# - SQLite:   sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_update
try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore

try:
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # type: ignore
except Exception:  # pragma: no cover
    pg_insert = None  # type: ignore

try:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # type: ignore
except Exception:  # pragma: no cover
    sqlite_insert = None  # type: ignore

# Choose a dialect-specific insert() helper.
# Several call-sites expect ON CONFLICT helpers (do nothing / update),
# so we prefer the Postgres dialect when available.
insert = pg_insert or sqlite_insert  # type: ignore


async def _record_user_activity_day(*, guild_id: int, user_id: int, day_utc: str) -> None:
    """Insert (user, guild, day) for retention. Phase 3. ON CONFLICT DO NOTHING."""
    if insert is None:
        return
    try:
        from utils.models import UserActivityDay  # type: ignore

        Session = get_sessionmaker()
        async with Session() as session:
            stmt = insert(UserActivityDay).values(
                user_id=int(user_id),
                guild_id=int(guild_id),
                day_utc=str(day_utc),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "guild_id", "day_utc"])  # type: ignore
            await session.execute(stmt)
            await session.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backwards compatible event log (stored in Redis-backed guild settings)
# ---------------------------------------------------------------------------

ANALYTICS_KEY = "analytics_events"
MAX_EVENTS_PER_GUILD = 5000


def _now() -> int:
    return int(time.time())


async def record_event(
    guild_id: int,
    event: str,
    *,
    user_id: int | None = None,
    command: str | None = None,
    result: str | None = None,
    reason: str | None = None,
    channel_id: int | None = None,
    fields: dict[str, Any] | None = None,
) -> None:
    """Append a small analytics event for debugging."""
    events = await get_guild_setting(guild_id, ANALYTICS_KEY, default=[])
    if not isinstance(events, list):
        events = []

    payload = {
        "t": _now(),
        "event": (event or "").strip()[:80],
    }
    if user_id is not None:
        payload["user_id"] = int(user_id)
    if channel_id is not None:
        payload["channel_id"] = int(channel_id)
    if command:
        payload["command"] = str(command)[:64]
    if result:
        payload["result"] = str(result)[:64]
    if reason:
        payload["reason"] = str(reason)[:160]
    if fields and isinstance(fields, dict):
        payload["fields"] = {str(k)[:40]: str(v)[:200] for k, v in fields.items()}

    events.append(payload)
    if len(events) > MAX_EVENTS_PER_GUILD:
        events = events[-MAX_EVENTS_PER_GUILD:]
    await set_guild_setting(guild_id, ANALYTICS_KEY, events)


async def get_summary(guild_id: int, *, days: int = 7) -> Dict[str, Any]:
    """Aggregate analytics events for the last N days for /analytics view.

    Returns dict with by_command, by_result, by_event (counts) and events_total.
    """
    events = await get_guild_setting(guild_id, ANALYTICS_KEY, default=[])
    if not isinstance(events, list):
        events = []
    cutoff_ts = _now() - (max(1, min(int(days), 30)) * 86400)
    by_command: Dict[str, int] = {}
    by_result: Dict[str, int] = {}
    by_event: Dict[str, int] = {}
    total = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = int(e.get("t", 0) or 0)
        if ts < cutoff_ts:
            continue
        total += 1
        cmd = str((e.get("command") or "")).strip().lower()
        if cmd:
            by_command[cmd] = by_command.get(cmd, 0) + 1
        res = str((e.get("result") or "")).strip().lower()
        if res:
            by_result[res] = by_result.get(res, 0) + 1
        ev = str((e.get("event") or "")).strip()
        if ev:
            by_event[ev] = by_event.get(ev, 0) + 1
    return {
        "by_command": by_command,
        "by_result": by_result,
        "by_event": by_event,
        "events_total": total,
    }


async def reset_guild(guild_id: int) -> None:
    """Clear stored analytics events for a guild (for /analytics reset)."""
    await set_guild_setting(guild_id, ANALYTICS_KEY, [])


async def get_event_counts(guild_id: int, *, since_s: int = 86400) -> Dict[str, int]:
    """Counts of event types for the last N seconds (best effort)."""
    events = await get_guild_setting(guild_id, ANALYTICS_KEY, default=[])
    if not isinstance(events, list):
        return {}
    cutoff = _now() - int(since_s)
    c = Counter()
    for e in events:
        if not isinstance(e, dict):
            continue
        if int(e.get("t", 0) or 0) < cutoff:
            continue
        name = str(e.get("event", "") or "").strip()
        if name:
            c[name] += 1
    return dict(c)


# ---------------------------------------------------------------------------
# Product metrics (Redis counters + Postgres flush)
# ---------------------------------------------------------------------------

METRIC_DAILY_ROLLS = "daily_rolls"
METRIC_DAILY_TALK_CALLS = "daily_talk_calls"
METRIC_DAILY_SCENE_CALLS = "daily_scene_calls"
METRIC_DAILY_AI_CALLS = "daily_ai_calls"  # talk + scene
METRIC_DAILY_AI_TOKEN_BUDGET = "daily_ai_token_budget"  # estimated max tokens requested
METRIC_DAILY_ACTIVE_USERS = "daily_active_users"

# Phase 2 funnel metrics
METRIC_PULL_5 = "pull_5"
METRIC_PULL_10 = "pull_10"
METRIC_TRIAL_START = "trial_start"
METRIC_CONVERSION = "conversion"  # Pro granted (trial or paid)


def utc_day_str(ts: int | None = None) -> str:
    """YYYYMMDD in UTC."""
    return time.strftime("%Y%m%d", time.gmtime(ts or _now()))


def _k_count(day_utc: str, guild_id: int, metric: str) -> str:
    return f"analytics:count:{day_utc}:{int(guild_id)}:{metric}"


def _k_active(day_utc: str, guild_id: int) -> str:
    return f"analytics:active:{day_utc}:{int(guild_id)}"


def _k_dirty(day_utc: str) -> str:
    return f"analytics:dirty:{day_utc}"


def _k_seen_guild(guild_id: int) -> str:
    return f"analytics:seen:{int(guild_id)}"


async def _mark_dirty(day_utc: str, guild_id: int) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.sadd(_k_dirty(day_utc), str(int(guild_id)))
        await r.expire(_k_dirty(day_utc), 86400 * 15)
    except Exception:
        pass


async def _touch_active(day_utc: str, guild_id: int, user_id: int) -> None:
    """Track daily active user sets (Redis) + first-seen in DB (rare) + activity day for retention."""
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        added_today = await r.sadd(_k_active(day_utc, guild_id), str(int(user_id)))
        await r.expire(_k_active(day_utc, guild_id), 86400 * 10)
    except Exception:
        return

    # Phase 3: record user-day activity for retention (only on first activity of day)
    if int(added_today or 0) == 1:
        try:
            await _record_user_activity_day(guild_id=guild_id, user_id=user_id, day_utc=day_utc)
        except Exception:
            pass
        # Update "Days Active" leaderboard (server + global) by 1 for this new active day
        try:
            from utils.leaderboard import update_all_periods, CATEGORY_ACTIVITY, GLOBAL_GUILD_ID as GLB_GID
            await update_all_periods(
                category=CATEGORY_ACTIVITY,
                guild_id=guild_id,
                user_id=user_id,
                value=1.0,
            )
            await update_all_periods(
                category=CATEGORY_ACTIVITY,
                guild_id=GLB_GID,
                user_id=user_id,
                value=1.0,
            )
        except Exception:
            pass

    # Only insert first_seen once per (guild,user): Redis set is a cheap guard.
    try:
        added = await r.sadd(_k_seen_guild(guild_id), str(int(user_id)))
        await r.expire(_k_seen_guild(guild_id), 86400 * 365)
    except Exception:
        return

    if int(added or 0) != 1:
        return

    if insert is None:
        return

    try:
        Session = get_sessionmaker()
        async with Session() as session:
            stmt = insert(UserFirstSeen).values(
                guild_id=int(guild_id),
                user_id=int(user_id),
                first_day_utc=str(day_utc),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["guild_id", "user_id"])  # type: ignore[attr-defined]
            await session.execute(stmt)
            await session.commit()
    except Exception:
        return


# Global guild ID for global rolls
GLOBAL_GUILD_ID = 0

async def track_roll(*, guild_id: int, user_id: int, count: int = 1) -> None:
    """Call whenever one or more rolls are successfully consumed.

    Updates both global and server leaderboards so /leaderboard view works for either scope.
    """
    day = utc_day_str()
    await _touch_active(day, guild_id, user_id)
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        k = _k_count(day, GLOBAL_GUILD_ID, METRIC_DAILY_ROLLS)
        await r.incrby(k, count)
        await r.expire(k, 86400 * 10)

        from utils.leaderboard import update_all_periods, CATEGORY_ROLLS

        # Global leaderboard (all rolls)
        await update_all_periods(
            category=CATEGORY_ROLLS,
            guild_id=GLOBAL_GUILD_ID,
            user_id=user_id,
            value=float(count),
        )
        # Server leaderboard (rolls in this server) so default "Server" scope shows data
        if guild_id != GLOBAL_GUILD_ID:
            await update_all_periods(
                category=CATEGORY_ROLLS,
                guild_id=guild_id,
                user_id=user_id,
                value=float(count),
            )
    except Exception:
        pass
    await _mark_dirty(day, GLOBAL_GUILD_ID)


async def track_ai_call(
    *,
    guild_id: int,
    user_id: int,
    mode: str = "talk",
    tokens_used: int = 0,
    est_token_budget: int = 0,
) -> None:
    """Call whenever an AI text call is made.

    Args:
      mode: "talk" or "scene" (others will be treated as "talk")

    Notes:
      - tokens_used should be the *real* total tokens from the API response when available.
      - est_token_budget remains for backwards compatibility and will be used only
        if tokens_used is not provided.
    """
    day = utc_day_str()
    await _touch_active(day, guild_id, user_id)
    r = await get_redis_or_none()
    if r is None:
        return

    mode_l = (mode or "talk").strip().lower()
    if mode_l not in {"talk", "scene"}:
        mode_l = "talk"

    # Total AI calls
    try:
        k_all = _k_count(day, guild_id, METRIC_DAILY_AI_CALLS)
        await r.incrby(k_all, 1)
        await r.expire(k_all, 86400 * 10)
    except Exception:
        pass

    # Mode-specific calls
    try:
        if mode_l == "scene":
            k = _k_count(day, guild_id, METRIC_DAILY_SCENE_CALLS)
        else:
            k = _k_count(day, guild_id, METRIC_DAILY_TALK_CALLS)
        await r.incrby(k, 1)
        await r.expire(k, 86400 * 10)
    except Exception:
        pass

    inc_tokens = 0
    if tokens_used and int(tokens_used) > 0:
        inc_tokens = int(tokens_used)
    elif est_token_budget and int(est_token_budget) > 0:
        inc_tokens = int(est_token_budget)

    if inc_tokens > 0:
        try:
            k2 = _k_count(day, guild_id, METRIC_DAILY_AI_TOKEN_BUDGET)
            await r.incrby(k2, int(inc_tokens))
            await r.expire(k2, 86400 * 10)
        except Exception:
            pass
    await _mark_dirty(day, guild_id)


# Backwards compatibility: older code calls track_talk().
async def track_talk(
    *,
    guild_id: int,
    user_id: int,
    tokens_used: int = 0,
    est_token_budget: int = 0,
) -> None:
    await track_ai_call(
        guild_id=guild_id,
        user_id=user_id,
        mode="talk",
        tokens_used=tokens_used,
        est_token_budget=est_token_budget,
    )


# ---------------------------------------------------------------------------
# Phase 2 funnel metrics
# ---------------------------------------------------------------------------


async def track_funnel_event(
    *,
    guild_id: int,
    event: str,
    user_id: int | None = None,
) -> None:
    """Increment a funnel metric (pull_5, pull_10, trial_start, conversion).

    Events are stored per-guild per-day in Redis and flushed to Postgres.
    """
    valid = (METRIC_PULL_5, METRIC_PULL_10, METRIC_TRIAL_START, METRIC_CONVERSION)
    if event not in valid:
        return

    day = utc_day_str()
    if user_id is not None:
        await _touch_active(day, guild_id, user_id)

    r = await get_redis_or_none()
    if r is None:
        return
    try:
        k = _k_count(day, guild_id, event)
        await r.incrby(k, 1)
        await r.expire(k, 86400 * 10)
    except Exception:
        pass
    await _mark_dirty(day, guild_id)


async def read_daily_counters(*, day_utc: str, guild_id: int) -> Dict[str, int]:
    """Read current Redis counters for a guild/day (best effort)."""
    r = await get_redis_or_none()
    out: Dict[str, int] = {}

    if r is None:
        return {
            METRIC_DAILY_ROLLS: 0,
            METRIC_DAILY_TALK_CALLS: 0,
            METRIC_DAILY_SCENE_CALLS: 0,
            METRIC_DAILY_AI_CALLS: 0,
            METRIC_DAILY_AI_TOKEN_BUDGET: 0,
            METRIC_DAILY_ACTIVE_USERS: 0,
            METRIC_PULL_5: 0,
            METRIC_PULL_10: 0,
            METRIC_TRIAL_START: 0,
            METRIC_CONVERSION: 0,
        }

    for metric in (
        METRIC_DAILY_ROLLS,
        METRIC_DAILY_TALK_CALLS,
        METRIC_DAILY_SCENE_CALLS,
        METRIC_DAILY_AI_CALLS,
        METRIC_DAILY_AI_TOKEN_BUDGET,
        METRIC_PULL_5,
        METRIC_PULL_10,
        METRIC_TRIAL_START,
        METRIC_CONVERSION,
    ):
        try:
            v = await r.get(_k_count(day_utc, guild_id, metric))
            out[metric] = int(v or 0)
        except Exception:
            out[metric] = 0

    try:
        out[METRIC_DAILY_ACTIVE_USERS] = int(await r.scard(_k_active(day_utc, guild_id)) or 0)
    except Exception:
        out[METRIC_DAILY_ACTIVE_USERS] = 0

    return out


async def flush_day_to_db(*, day_utc: str, guild_id: int) -> None:
    """Upsert Redis counters into Postgres for a given day+guild."""
    # No DB support in this deployment (or SQLAlchemy not installed).
    if pg_insert is None and sqlite_insert is None:
        return

    metrics = await read_daily_counters(day_utc=day_utc, guild_id=guild_id)
    non_zero = {k: int(v or 0) for k, v in metrics.items() if int(v or 0) > 0}
    if not non_zero:
        logger.debug("flush_day_to_db day_utc=%s guild_id=%s: all zeros, skipping write", day_utc, guild_id)
        return

    # Use engine dialect so we pick the right INSERT (session.get_bind() can be unreliable with async).
    try:
        dialect_name = str(getattr(get_engine().dialect, "name", "") or "").lower()
    except Exception:
        dialect_name = ""

    ins = None
    if "postgres" in dialect_name and pg_insert is not None:
        ins = pg_insert
    elif "sqlite" in dialect_name and sqlite_insert is not None:
        ins = sqlite_insert
    else:
        ins = pg_insert or sqlite_insert

    if ins is None:
        logger.warning("flush_day_to_db: no dialect insert available (dialect=%s)", dialect_name)
        return

    now_utc = datetime.now(timezone.utc)
    Session = get_sessionmaker()
    async with Session() as session:
        written = 0
        for metric, value in metrics.items():
            val = int(value or 0)
            if val <= 0:
                continue
            stmt = ins(AnalyticsDailyMetric).values(
                day_utc=str(day_utc),
                guild_id=int(guild_id),
                metric=str(metric),
                value=val,
                updated_at=now_utc,
            )
            try:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["day_utc", "guild_id", "metric"],
                    set_={"value": val, "updated_at": now_utc},
                )
                await session.execute(stmt)
                written += 1
            except Exception as e:
                logger.warning(
                    "flush_day_to_db upsert failed for %s/%s/%s: %s; rolling back and skipping rest",
                    day_utc, guild_id, metric, e,
                )
                await session.rollback()
                raise
        await session.commit()
        logger.debug("flush_day_to_db day_utc=%s guild_id=%s: wrote %s metrics (e.g. rolls=%s talk=%s)", day_utc, guild_id, written, non_zero.get(METRIC_DAILY_ROLLS), non_zero.get(METRIC_DAILY_TALK_CALLS))


async def pop_dirty_guilds(*, day_utc: str, max_items: int = 200) -> list[int]:
    """Pop up to N dirty guild IDs for a day (Redis)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    key = _k_dirty(day_utc)
    out: list[int] = []
    try:
        # Use SPOP for simple work-queue semantics.
        for _ in range(max_items):
            v = await r.spop(key)
            if not v:
                break
            out.append(int(v))
    except Exception:
        return []
    return out
