"""core/incident_monitor.py

Background task that DMs owners when incidents occur.

Incidents are signaled via Redis sequence key written by utils.incidents.record_incident.
This keeps low-level code Discord-agnostic while still giving you immediate alerts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import config

from utils.backpressure import get_redis_or_none
from utils.incidents import REDIS_INCIDENTS_LIST, REDIS_INCIDENTS_SEQ


logger = logging.getLogger("bot.incident_monitor")


async def _get_seq(r) -> int:
    try:
        v = await r.get(REDIS_INCIDENTS_SEQ)
        return int(v or 0)
    except Exception:
        return 0


def _fmt_incident(d: Dict[str, Any]) -> str:
    kind = str(d.get("kind") or "incident")
    reason = str(d.get("reason") or "")
    gid = d.get("guild_id")
    uid = d.get("user_id")
    extra = ""
    if gid is not None:
        extra += f" guild={gid}"
    if uid is not None:
        extra += f" user={uid}"
    return f"🚨 **Bot-Nardology incident**\nType: `{kind}`\nReason: {reason}{extra}"[:1800]


async def start_incident_monitor(bot) -> None:
    """Start the background loop (safe to call once)."""
    if getattr(bot, "_incident_monitor_started", False):
        return
    bot._incident_monitor_started = True  # type: ignore[attr-defined]

    async def loop() -> None:
        last_seen = 0
        while not bot.is_closed():
            try:
                r = await get_redis_or_none()
                if r is None:
                    await asyncio.sleep(10)
                    continue

                seq = await _get_seq(r)
                if seq <= last_seen:
                    await asyncio.sleep(5)
                    continue

                # Fetch most recent incident payload for message content
                incident_raw = None
                try:
                    items = await r.lrange(REDIS_INCIDENTS_LIST, 0, 0)
                    if items:
                        incident_raw = items[0]
                except Exception:
                    incident_raw = None

                if incident_raw:
                    try:
                        d = json.loads(incident_raw)
                    except Exception:
                        d = {"kind": "incident", "reason": "(unparseable incident payload)"}
                else:
                    d = {"kind": "incident", "reason": "(incident triggered)"}

                msg = _fmt_incident(d)

                # DM all owners (best-effort)
                owner_ids = list(getattr(config, "BOT_OWNER_IDS", set()) or [])
                for oid in owner_ids:
                    try:
                        user = bot.get_user(int(oid))
                        if user is None:
                            user = await bot.fetch_user(int(oid))
                        if user is not None:
                            await user.send(msg)
                    except Exception:
                        continue

                last_seen = seq
                await asyncio.sleep(2)
            except Exception:
                logger.exception("incident monitor loop error")
                await asyncio.sleep(10)

    bot.loop.create_task(loop())
