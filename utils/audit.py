# utils/audit.py
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.analytics import record_event

logger = logging.getLogger("bot.audit")

_AUDIT_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_log_dir() -> Path:
    # project_root/logs
    return Path(__file__).resolve().parent.parent / "logs"


def _rotate_if_needed(path: Path, *, max_bytes: int = 2_000_000, backups: int = 5) -> None:
    """
    Very small manual log rotation for audit.log (JSONL).
    Keeps audit logs from growing forever.
    """
    try:
        if not path.exists():
            return

        size = path.stat().st_size
        if size < max_bytes:
            return

        # Shift: audit.log.4 -> .5, ... audit.log -> .1
        for i in range(backups, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            dst = path.with_suffix(path.suffix + f".{i+1}")
            if src.exists():
                if i == backups:
                    src.unlink(missing_ok=True)
                else:
                    src.replace(dst)

        # Move current audit.log to .1
        first_backup = path.with_suffix(path.suffix + ".1")
        path.replace(first_backup)

    except Exception:
        # Rotation failure should not break the bot
        logger.exception("Failed rotating audit log")


def audit_log(
    event: str,
    *,
    guild_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    command: Optional[str] = None,
    result: Optional[str] = None,
    reason: Optional[str] = None,
    fields: Optional[dict[str, Any]] = None,
    log_dir: Optional[Path] = None,
) -> None:
    """
    Write one JSON event per line to logs/audit.log AND record analytics.

    Design goals:
    - Never raises (logging must never crash the bot)
    - Structured JSONL so you can grep/aggregate later
    - Rotates to prevent infinite growth
    """
    try:
        payload: dict[str, Any] = {"ts": _utc_now_iso(), "event": str(event)}

        if guild_id is not None:
            payload["guild_id"] = int(guild_id)
        if channel_id is not None:
            payload["channel_id"] = int(channel_id)
        if user_id is not None:
            payload["user_id"] = int(user_id)
        if username is not None:
            payload["username"] = str(username)
        if command is not None:
            payload["command"] = str(command)
        if result is not None:
            payload["result"] = str(result)
        if reason is not None:
            payload["reason"] = str(reason)

        if fields:
            for k, v in fields.items():
                payload[str(k)] = v

        # --- file logging ---
        target_dir = log_dir or _default_log_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "audit.log"

        with _AUDIT_LOCK:
            _rotate_if_needed(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        # --- analytics recording ---
        # Keep analytics minimal (function only accepts guild_id, event, user_id).
        if guild_id is not None:
            try:
                import asyncio

                # fire-and-forget so audit logging never blocks commands
                loop = asyncio.get_running_loop()
                loop.create_task(
                    record_event(
                        guild_id=int(guild_id),
                        event=str(event),
                        user_id=(int(user_id) if user_id is not None else None),
                        command=(str(command) if command is not None else None),
                        result=(str(result) if result is not None else None),
                        reason=(str(reason) if reason is not None else None),
                        channel_id=(int(channel_id) if channel_id is not None else None),
                        fields=(dict(fields) if fields else None),
                    )
                )

            except RuntimeError:
                # No running loop (very early startup). Skip analytics silently.
                pass
            except Exception:
                logger.exception("Failed to record analytics event=%s", event)

    except Exception:
        logger.exception("Failed to write audit log event=%s", event)
