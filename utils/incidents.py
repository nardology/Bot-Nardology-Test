"""utils/incidents.py

Lightweight incident recording + notification triggers.

Goals:
  - Persist "out of the ordinary" events in a place you can build a dashboard from later.
  - Trigger owner notifications (DM) without coupling low-level code to Discord.

Storage:
  - Always writes to logs/audit.log via utils.audit.audit_log (JSONL).
  - Best-effort also appends to a Redis list (for quick /owner viewing).
  - Increments a Redis sequence number so the bot can DM owners from a background task.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict

from utils.audit import audit_log
from utils.backpressure import get_redis_or_none


REDIS_INCIDENTS_LIST = "incidents:global"          # LPUSH JSON; capped
REDIS_INCIDENTS_SEQ = "incidents:global:seq"       # INCR to signal new incident


def _now() -> int:
    return int(time.time())


async def record_incident(
    *,
    kind: str,
    reason: str,
    fields: Dict[str, Any] | None = None,
    guild_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Record an incident (never raises)."""
    try:
        payload: Dict[str, Any] = {
            "t": _now(),
            "kind": (kind or "incident")[:64],
            "reason": (reason or "")[:400],
        }
        if guild_id is not None:
            payload["guild_id"] = int(guild_id)
        if user_id is not None:
            payload["user_id"] = int(user_id)
        if fields:
            # keep it compact
            payload["fields"] = {str(k)[:40]: str(v)[:200] for k, v in fields.items()}

        # 1) Durable-ish audit log on disk
        audit_log(
            "incident",
            guild_id=guild_id,
            user_id=user_id,
            result=str(kind)[:64],
            reason=str(reason)[:400],
            fields=(fields or None),
        )

        # 2) Best-effort Redis list + seq for DMs
        r = await get_redis_or_none()
        if r is None:
            return

        try:
            raw = json.dumps(payload, ensure_ascii=False)
        except Exception:
            raw = json.dumps({"t": _now(), "kind": "incident", "reason": "(serialization failed)"})

        try:
            await r.lpush(REDIS_INCIDENTS_LIST, raw)
            await r.ltrim(REDIS_INCIDENTS_LIST, 0, 199)  # keep last ~200
            await r.expire(REDIS_INCIDENTS_LIST, 86400 * 60)
        except Exception:
            pass

        try:
            await r.incr(REDIS_INCIDENTS_SEQ)
            await r.expire(REDIS_INCIDENTS_SEQ, 86400 * 90)
        except Exception:
            pass
    except Exception:
        return


async def list_recent_incidents(limit: int = 25) -> list[dict[str, Any]]:
    """Fetch recent incidents from Redis (best-effort)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        raw = await r.lrange(REDIS_INCIDENTS_LIST, 0, max(0, int(limit) - 1))
        out: list[dict[str, Any]] = []
        for item in raw or []:
            try:
                out.append(json.loads(item))
            except Exception:
                continue
        return out
    except Exception:
        return []
