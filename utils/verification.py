from __future__ import annotations

"""
utils/verification.py

Verification system for pack/character creation and editing.

Design:
- Redis-backed ticket storage (pending/approved/denied/auto_approved)
- Trust score tracking per (guild_id, user_id) to enable auto-approve for trusted creators
- Must degrade gracefully if Redis is unavailable (fail-closed: don't allow creation)
"""

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from utils.backpressure import get_redis_or_none

log = logging.getLogger("verification")


KEY_PENDING_TICKETS = "verification:pending"  # Redis SET of ticket_ids
KEY_TICKET_PREFIX = "verification:ticket:"  # verification:ticket:<ticket_id> -> JSON
KEY_TRUST_SCORE_PREFIX = "verification:trust:"  # verification:trust:<guild_id>:<user_id> -> JSON
KEY_AUTO_VERIFY_ENABLED = "verification:auto_enabled"  # Redis string "1" or "0"


def _now() -> int:
    return int(time.time())


def _as_int(x: Any) -> int | None:
    try:
        if x is None:
            return None
        return int(str(x).strip())
    except Exception:
        return None


def _json_dumps(d: Dict[str, Any]) -> str:
    return json.dumps(d, separators=(",", ":"))


def _json_loads(s: Any) -> Dict[str, Any]:
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="ignore")
        data = json.loads(str(s))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# -----------------------------
# Trust score (for auto-approve)
# -----------------------------


async def get_trust_score(*, guild_id: int, user_id: int) -> Dict[str, Any]:
    """Returns trust score metadata: {approved: int, denied: int, score: float, auto_approve: bool}"""
    gid = int(guild_id)
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return {"approved": 0, "denied": 0, "score": 0.0, "auto_approve": False}
    try:
        raw = await r.get(f"{KEY_TRUST_SCORE_PREFIX}{gid}:{uid}")
        if not raw:
            return {"approved": 0, "denied": 0, "score": 0.0, "auto_approve": False}
        d = _json_loads(raw)
        approved = int(d.get("approved", 0) or 0)
        denied = int(d.get("denied", 0) or 0)
        total = approved + denied
        score = (approved / total) if total > 0 else 0.0
        # Auto-approve if: at least 5 approvals AND approval rate >= 80%
        auto_approve = approved >= 5 and score >= 0.8
        return {
            "approved": approved,
            "denied": denied,
            "score": float(score),
            "auto_approve": bool(auto_approve),
        }
    except Exception:
        return {"approved": 0, "denied": 0, "score": 0.0, "auto_approve": False}


async def increment_trust_approval(*, guild_id: int, user_id: int) -> None:
    """Increment approval count for trust score."""
    gid = int(guild_id)
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{KEY_TRUST_SCORE_PREFIX}{gid}:{uid}"
    try:
        raw = await r.get(key)
        d = _json_loads(raw) if raw else {}
        d["approved"] = int(d.get("approved", 0) or 0) + 1
        d["updated_at"] = _now()
        await r.set(key, _json_dumps(d))
    except Exception:
        pass


async def increment_trust_denial(*, guild_id: int, user_id: int) -> None:
    """Increment denial count for trust score."""
    gid = int(guild_id)
    uid = int(user_id)
    r = await get_redis_or_none()
    if r is None:
        return
    key = f"{KEY_TRUST_SCORE_PREFIX}{gid}:{uid}"
    try:
        raw = await r.get(key)
        d = _json_loads(raw) if raw else {}
        d["denied"] = int(d.get("denied", 0) or 0) + 1
        d["updated_at"] = _now()
        await r.set(key, _json_dumps(d))
    except Exception:
        pass


# -----------------------------
# Auto-verify toggle
# -----------------------------


async def is_auto_verify_enabled() -> bool:
    r = await get_redis_or_none()
    if r is None:
        return False  # fail-closed
    try:
        v = await r.get(KEY_AUTO_VERIFY_ENABLED)
        return bool(int(v or 0))
    except Exception:
        return False


async def set_auto_verify_enabled(enabled: bool) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        await r.set(KEY_AUTO_VERIFY_ENABLED, "1" if enabled else "0")
    except Exception:
        pass


# -----------------------------
# Verification tickets
# -----------------------------


async def create_verification_ticket(
    *,
    ticket_type: str,  # "pack_create", "pack_edit", "character_add", "character_edit"
    guild_id: int,
    user_id: int,
    payload: Dict[str, Any],  # The pack/character data to verify
    original_payload: Optional[Dict[str, Any]] = None,  # For edits: what it was before
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a verification ticket. Returns (ok, message, ticket_id).
    If Redis is down, returns (False, error_message, None) - fail-closed.
    """
    r = await get_redis_or_none()
    if r is None:
        return False, "Verification system is temporarily unavailable. Please try again later.", None

    gid = int(guild_id)
    uid = int(user_id)
    ticket_id = uuid.uuid4().hex[:16]

    # Check trust score for auto-approve
    trust = await get_trust_score(guild_id=gid, user_id=uid)
    auto_approve = bool(trust.get("auto_approve", False)) and await is_auto_verify_enabled()

    # Character submissions always require manual review (copyright/identity policy).
    if str(ticket_type) in {"character_add", "character_edit"}:
        auto_approve = False

    ticket = {
        "ticket_id": ticket_id,
        "type": str(ticket_type),
        "status": "auto_approved" if auto_approve else "pending",
        "created_at": _now(),
        "guild_id": gid,
        "user_id": uid,
        "payload": payload,
        "original_payload": original_payload,
        "auto_approved": bool(auto_approve),
        "trust_score": float(trust.get("score", 0.0)),
    }

    try:
        await r.set(f"{KEY_TICKET_PREFIX}{ticket_id}", _json_dumps(ticket), ex=86400 * 30)  # 30 day TTL
        if not auto_approve:
            await r.sadd(KEY_PENDING_TICKETS, ticket_id)
        return True, "ok", ticket_id
    except Exception:
        log.exception("Failed creating verification ticket")
        return False, "Failed to create verification ticket.", None


async def get_ticket(ticket_id: str) -> Dict[str, Any]:
    r = await get_redis_or_none()
    if r is None:
        return {}
    try:
        raw = await r.get(f"{KEY_TICKET_PREFIX}{ticket_id}")
        if not raw:
            return {}
        return _json_loads(raw)
    except Exception:
        return {}


async def update_ticket_status(
    *,
    ticket_id: str,
    status: str,  # "approved", "denied", "auto_approved"
    decided_by: int,
    decision_reason: str = "",
) -> bool:
    """Update ticket status and remove from pending set if approved/denied."""
    r = await get_redis_or_none()
    if r is None:
        return False
    try:
        ticket = await get_ticket(ticket_id)
        if not ticket:
            return False
        ticket["status"] = str(status)
        ticket["decided_at"] = _now()
        ticket["decided_by"] = int(decided_by)
        ticket["decision_reason"] = (decision_reason or "").strip()[:500]
        await r.set(f"{KEY_TICKET_PREFIX}{ticket_id}", _json_dumps(ticket), ex=86400 * 30)
        await r.srem(KEY_PENDING_TICKETS, ticket_id)
        return True
    except Exception:
        return False


async def list_pending_tickets(*, limit: int = 50) -> list[Dict[str, Any]]:
    """List pending verification tickets (not auto-approved)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        ticket_ids = await r.smembers(KEY_PENDING_TICKETS)
        out: list[Dict[str, Any]] = []
        for tid_raw in ticket_ids or []:
            tid = tid_raw.decode("utf-8", errors="ignore") if isinstance(tid_raw, (bytes, bytearray)) else str(tid_raw)
            t = await get_ticket(tid)
            if t and str(t.get("status") or "") == "pending":
                out.append(t)
            if len(out) >= limit:
                break
        # Sort by created_at (oldest first)
        out.sort(key=lambda x: int(x.get("created_at", 0) or 0))
        return out
    except Exception:
        return []


async def list_tickets_by_status(*, status: str, limit: int = 100) -> list[Dict[str, Any]]:
    """List tickets by status (pending, approved, denied, auto_approved)."""
    r = await get_redis_or_none()
    if r is None:
        return []
    try:
        # Get all ticket IDs from pending set, then check each ticket's actual status
        ticket_ids = await r.smembers(KEY_PENDING_TICKETS)
        out: list[Dict[str, Any]] = []
        checked_ids = set()
        
        # Check pending set first
        for tid_raw in ticket_ids or []:
            tid = tid_raw.decode("utf-8", errors="ignore") if isinstance(tid_raw, (bytes, bytearray)) else str(tid_raw)
            if tid in checked_ids:
                continue
            checked_ids.add(tid)
            t = await get_ticket(tid)
            if t and str(t.get("status") or "") == status:
                out.append(t)
            if len(out) >= limit:
                break
        
        # Also scan recent tickets (best-effort, limited by Redis SCAN)
        # This is a simplified approach - in production you might want a separate index
        if len(out) < limit:
            # Try to find more by pattern matching (this is best-effort)
            try:
                cursor = 0
                for _ in range(10):  # Limit scans to avoid performance issues
                    cursor, keys = await r.scan(cursor, match=f"{KEY_TICKET_PREFIX}*", count=100)
                    for key_raw in keys or []:
                        key = key_raw.decode("utf-8", errors="ignore") if isinstance(key_raw, (bytes, bytearray)) else str(key_raw)
                        tid = key.replace(KEY_TICKET_PREFIX, "")
                        if tid in checked_ids:
                            continue
                        checked_ids.add(tid)
                        t = await get_ticket(tid)
                        if t and str(t.get("status") or "") == status:
                            out.append(t)
                        if len(out) >= limit:
                            break
                    if cursor == 0 or len(out) >= limit:
                        break
            except Exception:
                pass
        
        # Sort by created_at (newest first for denied/approved, oldest first for pending)
        reverse = status != "pending"
        out.sort(key=lambda x: int(x.get("created_at", 0) or 0), reverse=reverse)
        return out[:limit]
    except Exception:
        return []


async def get_denied_ticket(ticket_id: str) -> Dict[str, Any]:
    """Get a denied ticket (for appeals)."""
    ticket = await get_ticket(ticket_id)
    if ticket and str(ticket.get("status") or "") == "denied":
        return ticket
    return {}


async def check_auto_approve_expired(*, ticket_id: str, days: int = 5) -> bool:
    """Check if a pending ticket is older than N days (for auto-approve)."""
    ticket = await get_ticket(ticket_id)
    if not ticket:
        return False
    status = str(ticket.get("status") or "")
    if status != "pending":
        return False
    created = int(ticket.get("created_at", 0) or 0)
    if created <= 0:
        return False
    age_days = (_now() - created) / 86400.0
    return age_days >= float(days)


async def should_skip_auto_approve(*, ticket: Dict[str, Any]) -> tuple[bool, str]:
    """
    Check if a ticket should be skipped from auto-approve due to safeguards.
    Returns (should_skip, reason).
    """
    payload = ticket.get("payload") or {}
    ticket_type = str(ticket.get("type") or "")

    # Safeguard 0: All character submissions require manual review (copyright/identity policy).
    if ticket_type in {"character_add", "character_edit"}:
        return True, "All character submissions require manual review"

    # Safeguard 1: Skip private packs (they require passwords, need manual review)
    if ticket_type in {"pack_create", "pack_edit"}:
        if bool(payload.get("private", False)):
            return True, "Private packs require manual review"

    # Safeguard 3: Skip if payload is missing critical fields
    if ticket_type in {"pack_create", "pack_edit"}:
        if not payload.get("pack_id") or not payload.get("name"):
            return True, "Missing required pack fields"

    if ticket_type in {"character_add", "character_edit"}:
        if not payload.get("id") and not payload.get("style_id"):
            return True, "Missing character ID"
        if not payload.get("pack_id"):
            return True, "Missing pack ID"

    return False, ""


async def auto_approve_ticket(*, ticket_id: str, days: int = 5) -> tuple[bool, str]:
    """
    Auto-approve a ticket if it's older than N days and passes safeguards.
    Returns (success, message).
    """
    ticket = await get_ticket(ticket_id)
    if not ticket:
        return False, "Ticket not found"

    status = str(ticket.get("status") or "")
    if status != "pending":
        return False, f"Ticket already {status}"

    # Check age
    if not await check_auto_approve_expired(ticket_id=ticket_id, days=days):
        return False, "Ticket not old enough"

    # Check safeguards
    should_skip, skip_reason = await should_skip_auto_approve(ticket=ticket)
    if should_skip:
        log.info("Skipping auto-approve for ticket %s: %s", ticket_id, skip_reason)
        return False, skip_reason

    # Auto-approve: apply the payload
    gid = int(ticket.get("guild_id") or 0)
    uid = int(ticket.get("user_id") or 0)
    ticket_type = str(ticket.get("type") or "")
    payload = ticket.get("payload") or {}

    try:
        if ticket_type == "pack_create":
            from utils.packs_store import upsert_custom_pack
            from utils.character_registry import merge_pack_payload
            ok = await upsert_custom_pack(payload)
            if ok:
                await increment_trust_approval(guild_id=gid, user_id=uid)
                await update_ticket_status(
                    ticket_id=ticket_id,
                    status="auto_approved",
                    decided_by=0,  # System
                    decision_reason=f"Auto-approved after {days} days",
                )
                try:
                    merge_pack_payload(payload)
                except Exception:
                    pass
                return True, f"Pack {payload.get('pack_id', '')} auto-approved"
            return False, "Failed to create pack"

        elif ticket_type == "pack_edit":
            from utils.packs_store import upsert_custom_pack
            from utils.character_registry import merge_pack_payload
            ok = await upsert_custom_pack(payload)
            if ok:
                await increment_trust_approval(guild_id=gid, user_id=uid)
                await update_ticket_status(
                    ticket_id=ticket_id,
                    status="auto_approved",
                    decided_by=0,
                    decision_reason=f"Auto-approved after {days} days",
                )
                try:
                    merge_pack_payload(payload)
                except Exception:
                    pass
                return True, f"Pack {payload.get('pack_id', '')} auto-updated"
            return False, "Failed to update pack"

        elif ticket_type in {"character_add", "character_edit"}:
            from utils.packs_store import add_character_to_pack, get_custom_pack
            from utils.character_registry import merge_pack_payload
            pack_id = str(payload.get("pack_id") or "")
            ok, msg = await add_character_to_pack(pack_id, payload)
            if ok:
                await increment_trust_approval(guild_id=gid, user_id=uid)
                await update_ticket_status(
                    ticket_id=ticket_id,
                    status="auto_approved",
                    decided_by=0,
                    decision_reason=f"Auto-approved after {days} days",
                )
                try:
                    p = await get_custom_pack(pack_id)
                    if p:
                        merge_pack_payload(p)
                except Exception:
                    pass
                action = "added" if ticket_type == "character_add" else "updated"
                return True, f"Character {payload.get('id', '')} auto-{action}"
            return False, f"Failed to add/update character: {msg}"

        return False, f"Unknown ticket type: {ticket_type}"

    except Exception:
        log.exception("Failed auto-approving ticket %s", ticket_id)
        return False, "Exception during auto-approve"
