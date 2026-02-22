# utils/world_events.py
from __future__ import annotations

"""World Events system — inject live, time-bound narrative context into characters.

Events are stored in ``data/world_events.json`` and loaded at import time.
Each event can target specific characters via an ``affects`` dict whose keys
are character IDs and values are the narrative context injected into that
character's system prompt.

Runtime helpers let owner commands add/toggle/remove events without a restart.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("bot.world_events")

EVENTS_FILE = Path("data") / "world_events.json"

_events: list[dict[str, Any]] = []


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _load_events_from_disk() -> list[dict[str, Any]]:
    try:
        if not EVENTS_FILE.exists():
            return []
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("world_events.json root must be a list")
            return []
        return [e for e in data if isinstance(e, dict)]
    except Exception:
        logger.warning("Failed to load world_events.json", exc_info=True)
        return []


def _save_events_to_disk() -> None:
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_events, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.warning("Failed to save world_events.json", exc_info=True)


def reload_events() -> int:
    """Reload events from disk. Returns the count loaded."""
    global _events
    _events = _load_events_from_disk()
    return len(_events)


def get_all_events(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return all events, optionally including inactive ones."""
    if include_inactive:
        return list(_events)
    return [e for e in _events if e.get("active", True)]


def get_active_events_for_character(style_id: str) -> list[dict[str, Any]]:
    """Return active, non-expired events that affect the given character."""
    sid = (style_id or "").strip().lower()
    if not sid:
        return []
    today = date.today()
    results: list[dict[str, Any]] = []
    for ev in _events:
        if not ev.get("active", True):
            continue
        expires = _parse_date(ev.get("expires"))
        if expires and expires < today:
            continue
        affects = ev.get("affects")
        if isinstance(affects, dict) and sid in affects:
            results.append(ev)
    return results


def build_world_events_prompt_block(style_id: str) -> str:
    """Build the prompt injection block for all active events affecting a character."""
    events = get_active_events_for_character(style_id)
    if not events:
        return ""
    sid = (style_id or "").strip().lower()
    lines = ["# Current World Events"]
    for ev in events:
        context = ev["affects"][sid]
        summary = ev.get("summary", "")
        if summary:
            lines.append(f"[{summary}]")
        lines.append(str(context))
    lines.append(
        "Weave these events into your responses naturally when relevant "
        "\u2014 don't announce them unless asked."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runtime mutation helpers (used by owner commands)
# ---------------------------------------------------------------------------

def add_event(event: dict[str, Any], *, save: bool = True) -> bool:
    """Add a new event. Returns False if an event with the same id exists."""
    eid = str(event.get("id") or "").strip().lower()
    if not eid:
        return False
    for existing in _events:
        if str(existing.get("id") or "").strip().lower() == eid:
            return False
    ev = dict(event)
    ev["id"] = eid
    ev.setdefault("active", True)
    _events.append(ev)
    if save:
        _save_events_to_disk()
    return True


def toggle_event(event_id: str, active: bool, *, save: bool = True) -> bool:
    """Activate or deactivate an event by id."""
    eid = (event_id or "").strip().lower()
    for ev in _events:
        if str(ev.get("id") or "").strip().lower() == eid:
            ev["active"] = bool(active)
            if save:
                _save_events_to_disk()
            return True
    return False


def remove_event(event_id: str, *, save: bool = True) -> bool:
    """Permanently remove an event by id."""
    global _events
    eid = (event_id or "").strip().lower()
    before = len(_events)
    _events = [
        ev for ev in _events
        if str(ev.get("id") or "").strip().lower() != eid
    ]
    removed = len(_events) < before
    if removed and save:
        _save_events_to_disk()
    return removed


# Load events at import time (safe no-op if file doesn't exist).
reload_events()
