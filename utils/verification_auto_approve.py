from __future__ import annotations

"""Background auto-approve loop for verification tickets.

This runs inside the bot process and periodically checks for pending tickets
older than 5 days (configurable) and auto-approves them if:
- Auto-verify is enabled
- Ticket passes safeguards (not private, etc.)
"""

import asyncio
import logging
import os

from utils.verification import (
    is_auto_verify_enabled,
    list_pending_tickets,
    auto_approve_ticket,
    check_auto_approve_expired,
)

logger = logging.getLogger("verification.auto_approve")

_task: asyncio.Task | None = None


def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        v = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        v = default
    return max(min_value, v)


async def _process_old_tickets() -> None:
    """Check for tickets older than 5 days and auto-approve them (if enabled and safe)."""
    # Check if auto-verify is enabled
    if not await is_auto_verify_enabled():
        return

    # Get all pending tickets (we'll filter by age)
    tickets = await list_pending_tickets(limit=1000)  # Process up to 1000 at a time
    if not tickets:
        return

    days_threshold = _get_env_int("VERIFICATION_AUTO_APPROVE_DAYS", 5, min_value=1)
    processed = 0
    approved = 0
    skipped = 0
    failed = 0

    for ticket in tickets:
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            continue

        # Check if ticket is old enough
        if not await check_auto_approve_expired(ticket_id=ticket_id, days=days_threshold):
            continue

        processed += 1
        success, msg = await auto_approve_ticket(ticket_id=ticket_id, days=days_threshold)
        if success:
            approved += 1
            logger.info("Auto-approved ticket %s: %s", ticket_id, msg)
        else:
            # Check if it was skipped due to safeguards (expected) or failed (unexpected)
            if "requires manual review" in msg.lower() or "skip" in msg.lower():
                skipped += 1
                logger.debug("Skipped auto-approve for ticket %s: %s", ticket_id, msg)
            else:
                failed += 1
                logger.warning("Failed auto-approving ticket %s: %s", ticket_id, msg)

        # Gentle pacing to avoid overwhelming Redis/DB
        if processed % 10 == 0:
            await asyncio.sleep(0.5)

    if processed > 0:
        logger.info(
            "Auto-approve cycle: processed=%d approved=%d skipped=%d failed=%d",
            processed,
            approved,
            skipped,
            failed,
        )


async def _loop() -> None:
    """Main background loop that runs periodically."""
    interval = _get_env_int("VERIFICATION_AUTO_APPROVE_INTERVAL_S", 3600, min_value=300)  # Default: 1 hour
    logger.info("Starting verification auto-approve loop (interval=%ds)", interval)

    while True:
        try:
            await _process_old_tickets()
        except Exception:
            logger.exception("verification auto-approve tick failed")
        await asyncio.sleep(interval)


def start_verification_auto_approve_loop() -> None:
    """Start the background auto-approve loop once per process."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    logger.info("Started verification auto-approve background task")
