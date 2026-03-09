"""utils/ai_kill.py

Redis-backed global AI kill switch.

Why:
  - config.AI_DISABLED is an env/static flag.
  - For safety, we also want a *runtime* kill switch that can be toggled
    instantly (e.g., anomaly detection) without redeploying.

Design:
  - Store a single Redis key with optional TTL.
  - Keep a small reason blob for operator visibility.
  - If Redis is unavailable, degrade to config.AI_DISABLED only.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple

import config

from utils.backpressure import get_redis_or_none


KEY_DISABLED = "ai:disabled"
KEY_DISABLED_META = "ai:disabled:meta"


def _now() -> int:
    return int(time.time())


async def is_disabled() -> bool:
    """True if AI is disabled via env OR Redis flag."""
    if getattr(config, "AI_DISABLED", False):
        return True
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        v = await r.get(KEY_DISABLED)
        return bool(int(v or 0))
    except Exception:
        return False


async def disable(*, reason: str, ttl_s: int = 3600) -> None:
    """Disable AI globally for ttl_s seconds (default 1 hour)."""
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.set(KEY_DISABLED, "1", ex=max(60, int(ttl_s)))
    except Exception:
        pass

    meta: Dict[str, Any] = {
        "t": _now(),
        "reason": (reason or "").strip()[:400],
        "ttl_s": max(60, int(ttl_s)),
    }
    try:
        await r.set(KEY_DISABLED_META, json.dumps(meta), ex=max(300, int(ttl_s)))
    except Exception:
        pass

    # Incident record (for DMs + future dashboard)
    try:
        from utils.incidents import record_incident

        await record_incident(
            kind="ai_disabled",
            reason=str(meta.get("reason") or "")[:400],
            fields={"ttl_s": int(meta.get("ttl_s") or 0)},
        )
    except Exception:
        pass


async def enable() -> None:
    """Re-enable AI (clears Redis flag)."""
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.delete(KEY_DISABLED)
        await r.delete(KEY_DISABLED_META)
    except Exception:
        pass


async def get_disable_meta() -> Tuple[Optional[int], str, Optional[int]]:
    """Returns (disabled_at_unix, reason, ttl_s) if available."""
    r = await get_redis_or_none()
    if r is None:
        return (None, "", None)
    try:
        raw = await r.get(KEY_DISABLED_META)
        if not raw:
            return (None, "", None)
        if isinstance(raw, (bytes, bytearray)):
            raw_s = raw.decode("utf-8", errors="ignore")
        else:
            raw_s = str(raw)
        data = json.loads(raw_s)
        if not isinstance(data, dict):
            return (None, "", None)
        t = data.get("t")
        ttl_s = data.get("ttl_s")
        reason = str(data.get("reason") or "")
        return (
            int(t) if t is not None else None,
            reason,
            int(ttl_s) if ttl_s is not None else None,
        )
    except Exception:
        return (None, "", None)
