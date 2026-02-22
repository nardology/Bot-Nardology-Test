# core/stripe_webhook.py
"""Stripe webhook receiver running as an aiohttp web server inside the bot process.

Railway provides a PORT env var. We bind to 0.0.0.0:PORT so Stripe can reach us.
The webhook verifies signatures, then dispatches to handlers that update
UserPremiumEntitlement and the points wallet.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from datetime import datetime, timezone

import config

log = logging.getLogger("stripe.webhook")

# ---------------------------------------------------------------------------
# In-memory rate limiter for the webhook endpoint (sliding window)
# ---------------------------------------------------------------------------
_RATE_LIMIT_MAX = 60          # max requests per window
_RATE_LIMIT_WINDOW_S = 60     # window size in seconds
_rate_timestamps: collections.deque[float] = collections.deque()


def _rate_limit_ok() -> bool:
    """Return True if the request should be allowed, False to reject (429)."""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_S
    while _rate_timestamps and _rate_timestamps[0] < cutoff:
        _rate_timestamps.popleft()
    if len(_rate_timestamps) >= _RATE_LIMIT_MAX:
        return False
    _rate_timestamps.append(now)
    return True

# We store a reference to the Discord bot so we can DM users on payment events.
_bot = None


def _get_stripe():
    import stripe as _s
    _s.api_key = config.STRIPE_SECRET_KEY
    return _s


# ---------------------------------------------------------------------------
# Premium helpers (thin wrappers around utils/premium.py)
# ---------------------------------------------------------------------------

async def _activate_premium(
    *,
    user_id: int,
    stripe_sub_id: str,
    stripe_cust_id: str,
    period_end: datetime,
) -> None:
    """Set a user to Pro via Stripe subscription."""
    from utils.premium import activate_stripe_premium
    await activate_stripe_premium(
        user_id=user_id,
        stripe_sub_id=stripe_sub_id,
        stripe_cust_id=stripe_cust_id,
        period_end=period_end,
    )
    log.info("Activated Pro for user=%s sub=%s", user_id, stripe_sub_id)


async def _deactivate_premium(*, user_id: int) -> None:
    """Downgrade a user back to free."""
    from utils.premium import deactivate_stripe_premium
    await deactivate_stripe_premium(user_id=user_id)
    log.info("Deactivated Pro for user=%s", user_id)


async def _update_period_end(*, user_id: int, period_end: datetime) -> None:
    """Update the subscription_period_end for a user (renewal or grace period)."""
    from utils.db import get_sessionmaker
    from utils.models import UserPremiumEntitlement
    Session = get_sessionmaker()
    async with Session() as session:
        ent = await session.get(UserPremiumEntitlement, int(user_id))
        if ent is not None:
            ent.subscription_period_end = period_end
            ent.updated_at = datetime.now(timezone.utc)
            await session.commit()


async def _credit_points(*, user_id: int, points: int, guild_id: int = 0) -> None:
    """Credit purchased points to a user's global wallet."""
    from utils.points_store import adjust_points
    new_bal = await adjust_points(
        guild_id=guild_id,
        user_id=user_id,
        delta=points,
        reason="stripe_purchase",
        meta={"points": points},
    )
    log.info("Credited %d points to user=%s (new balance=%d)", points, user_id, new_bal)


async def _try_dm_user(user_id: int, message: str) -> None:
    """Best-effort DM to a Discord user. Never raises."""
    try:
        if _bot is None:
            return
        user = await _bot.fetch_user(int(user_id))
        if user:
            await user.send(message[:2000])
    except Exception:
        log.debug("Could not DM user %s", user_id, exc_info=True)


async def _notify_owners(message: str) -> None:
    """Best-effort DM to all bot owners. Never raises."""
    for owner_id in config.BOT_OWNER_IDS:
        await _try_dm_user(owner_id, message)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def handle_checkout_completed(session_obj: dict) -> None:
    """Handle checkout.session.completed — activates Pro, credits points, or activates gift."""
    metadata = session_obj.get("metadata") or {}
    event_type = metadata.get("type", "")

    if event_type == "pro_subscription":
        user_id = int(metadata.get("user_id") or 0)
        sub_id = str(session_obj.get("subscription") or "")
        cust_id = str(session_obj.get("customer") or "")

        if not user_id:
            log.warning("checkout.session.completed: missing user_id in metadata")
            return

        # Retrieve subscription to get period_end.
        stripe = _get_stripe()
        period_end = datetime.now(timezone.utc)
        if sub_id:
            try:
                sub = await asyncio.to_thread(stripe.Subscription.retrieve, sub_id)
                ts = sub.get("current_period_end")
                if ts:
                    period_end = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except Exception:
                log.exception("Failed to retrieve subscription %s", sub_id)

        await _activate_premium(
            user_id=user_id,
            stripe_sub_id=sub_id,
            stripe_cust_id=cust_id,
            period_end=period_end,
        )
        await _try_dm_user(
            user_id,
            "**Pro activated!** You now have Bot-Nardology Pro. "
            "Enjoy 3x rolls, longer AI responses, conversation memory, and more!",
        )
        await _notify_owners(
            f"\U0001f4b0 **New Pro Subscription!**\n"
            f"**User:** <@{user_id}> (ID: {user_id})\n"
            f"**Subscription:** `{sub_id}`\n"
            f"**Renews:** {period_end.strftime('%B %d, %Y') if period_end else 'N/A'}",
        )

    elif event_type == "gift_purchase":
        gifter_user_id = int(metadata.get("gifter_user_id") or 0)
        recipient_user_id = int(metadata.get("recipient_user_id") or 0)
        months = int(metadata.get("months") or 0)
        stripe_session_id = str(session_obj.get("id") or "")

        if not gifter_user_id or not recipient_user_id or not months:
            log.warning("checkout.session.completed: missing gift metadata")
            return

        from utils.premium import activate_gift_premium
        await activate_gift_premium(
            recipient_user_id=recipient_user_id,
            gifter_user_id=gifter_user_id,
            months=months,
            stripe_session_id=stripe_session_id,
        )
        await _try_dm_user(
            recipient_user_id,
            f"**You received a gift!** <@{gifter_user_id}> gifted you "
            f"**{months} month{'s' if months != 1 else ''}** of Bot-Nardology Pro! "
            f"Enjoy 3x rolls, longer AI responses, conversation memory, and more!",
        )
        await _try_dm_user(
            gifter_user_id,
            f"**Gift sent!** Your gift of **{months} month{'s' if months != 1 else ''}** "
            f"of Bot-Nardology Pro has been delivered to <@{recipient_user_id}>!",
        )
        await _notify_owners(
            f"\U0001f381 **New Premium Gift!**\n"
            f"**Gifter:** <@{gifter_user_id}> (ID: {gifter_user_id})\n"
            f"**Recipient:** <@{recipient_user_id}> (ID: {recipient_user_id})\n"
            f"**Duration:** {months} month{'s' if months != 1 else ''}",
        )

    elif event_type == "points_purchase":
        user_id = int(metadata.get("user_id") or 0)
        guild_id = int(metadata.get("guild_id") or 0)
        points = int(metadata.get("points_amount") or 0)

        if not user_id or not points:
            log.warning("checkout.session.completed: missing user_id or points_amount")
            return

        await _credit_points(user_id=user_id, points=points, guild_id=guild_id)
        await _try_dm_user(
            user_id,
            f"**Points purchased!** {points:,} points have been added to your wallet. "
            f"Use `/points balance` to check your balance.",
        )
        await _notify_owners(
            f"\U0001f4b0 **New Points Purchase!**\n"
            f"**Purchaser:** <@{user_id}> (ID: {user_id})\n"
            f"**Points:** {points:,}\n"
            f"**Server:** {guild_id}",
        )

    else:
        log.info("checkout.session.completed: unknown type=%r, ignoring", event_type)


async def handle_invoice_paid(invoice: dict) -> None:
    """Handle invoice.paid — renews Pro subscription (updates period_end)."""
    sub_id = str(invoice.get("subscription") or "")
    if not sub_id:
        return

    # Retrieve the subscription to get metadata and new period_end.
    stripe = _get_stripe()
    try:
        sub = await asyncio.to_thread(stripe.Subscription.retrieve, sub_id)
    except Exception:
        log.exception("Failed to retrieve subscription %s for invoice.paid", sub_id)
        return

    metadata = sub.get("metadata") or {}
    user_id = int(metadata.get("user_id") or 0)
    is_pro = metadata.get("type") == "pro_subscription"

    # Fallback: check our DB if metadata is missing
    if not is_pro or not user_id:
        log.debug(
            "invoice.paid: metadata missing or incomplete (type=%r, user_id=%s, sub=%s). "
            "Attempting DB fallback.",
            metadata.get("type"), user_id, sub_id,
        )
        db_user = await _lookup_user_by_subscription(sub_id)
        if db_user:
            user_id = user_id or db_user
            is_pro = True
            log.info("invoice.paid: DB fallback found user=%s for sub=%s", user_id, sub_id)

    if not is_pro or not user_id:
        log.debug("invoice.paid: not a pro subscription or unknown user, ignoring (sub=%s)", sub_id)
        return

    ts = sub.get("current_period_end")
    if ts:
        period_end = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        await _update_period_end(user_id=user_id, period_end=period_end)
        log.info("Renewed Pro for user=%s until %s", user_id, period_end.isoformat())


async def _lookup_user_by_subscription(sub_id: str) -> int:
    """Fallback: look up user_id from user_premium_entitlements by stripe_subscription_id.

    Returns user_id or 0 if not found.
    """
    from utils.db import get_sessionmaker
    from utils.models import UserPremiumEntitlement

    try:
        from sqlalchemy import select
    except Exception:
        return 0

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(UserPremiumEntitlement.user_id)
            .where(UserPremiumEntitlement.stripe_subscription_id == sub_id)
            .limit(1)
        )
        row = res.first()
        if row:
            return int(row[0] or 0)
    return 0


async def handle_subscription_updated(sub: dict) -> None:
    """Handle customer.subscription.updated — detect cancel_at_period_end."""
    sub_id = str(sub.get("id") or "")
    metadata = sub.get("metadata") or {}

    user_id = int(metadata.get("user_id") or 0)
    is_pro = metadata.get("type") == "pro_subscription"

    # Fallback: if metadata doesn't identify this as a pro subscription,
    # check whether this subscription ID is already stored in our DB.
    if not is_pro or not user_id:
        log.debug(
            "subscription.updated: metadata missing or incomplete (type=%r, user_id=%s, sub=%s). "
            "Attempting DB fallback.",
            metadata.get("type"), user_id, sub_id,
        )
        if sub_id:
            db_user = await _lookup_user_by_subscription(sub_id)
            if db_user:
                user_id = user_id or db_user
                is_pro = True
                log.info("subscription.updated: DB fallback found user=%s for sub=%s", user_id, sub_id)

    if not is_pro or not user_id:
        log.debug("subscription.updated: not a pro subscription or unknown user, ignoring (sub=%s)", sub_id)
        return

    cancel_at_period_end = bool(sub.get("cancel_at_period_end", False))
    ts = sub.get("current_period_end")
    period_end = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None

    log.info(
        "subscription.updated: user=%s cancel_at_period_end=%s period_end=%s sub=%s",
        user_id, cancel_at_period_end, period_end, sub_id,
    )

    if cancel_at_period_end and period_end:
        # User cancelled but stays active until period end.
        await _update_period_end(user_id=user_id, period_end=period_end)
        log.info("Subscription cancelled at period end for user=%s (until %s)", user_id, period_end.isoformat())
        await _try_dm_user(
            user_id,
            f"Your Bot-Nardology Pro subscription has been **cancelled**. "
            f"Pro features remain active until **{period_end.strftime('%B %d, %Y')}**.",
        )

        # Extract cancellation reason
        cancel_details = sub.get("cancellation_details") or {}
        cancel_reason = cancel_details.get("reason") or "not provided"
        cancel_comment = cancel_details.get("comment") or ""
        reason_text = cancel_reason
        if cancel_comment:
            reason_text = f"{cancel_reason} \u2014 \"{cancel_comment}\""

        await _notify_owners(
            f"\u274c **Pro Subscription Cancelled!**\n"
            f"**User:** <@{user_id}> (ID: {user_id})\n"
            f"**Subscription:** `{sub_id}`\n"
            f"**Reason:** {reason_text}\n"
            f"**Pro active until:** {period_end.strftime('%B %d, %Y')}",
        )
    elif not cancel_at_period_end and period_end:
        # Reactivated (un-cancelled).
        await _update_period_end(user_id=user_id, period_end=period_end)
        log.info("Subscription reactivated for user=%s", user_id)


async def handle_subscription_deleted(sub: dict) -> None:
    """Handle customer.subscription.deleted — subscription fully ended."""
    sub_id = str(sub.get("id") or "")
    metadata = sub.get("metadata") or {}

    user_id = int(metadata.get("user_id") or 0)
    is_pro = metadata.get("type") == "pro_subscription"

    # Fallback: check our DB if metadata is missing
    if not is_pro or not user_id:
        log.debug(
            "subscription.deleted: metadata missing or incomplete (type=%r, user_id=%s, sub=%s). "
            "Attempting DB fallback.",
            metadata.get("type"), user_id, sub_id,
        )
        if sub_id:
            db_user = await _lookup_user_by_subscription(sub_id)
            if db_user:
                user_id = user_id or db_user
                is_pro = True
                log.info("subscription.deleted: DB fallback found user=%s for sub=%s", user_id, sub_id)

    if not is_pro or not user_id:
        log.debug("subscription.deleted: not a pro subscription or unknown user, ignoring (sub=%s)", sub_id)
        return

    await _deactivate_premium(user_id=user_id)
    log.info("Subscription deleted for user=%s sub=%s", user_id, sub_id)
    await _try_dm_user(
        user_id,
        "Your Bot-Nardology Pro subscription has **expired**. "
        "You are now on the free tier. Use `/premium subscribe` to resubscribe!",
    )


async def _lookup_user_by_customer(cust_id: str) -> int:
    """Resolve a Stripe customer ID to a Discord user ID via the StripeCustomer table.

    Returns discord_user_id or 0 if not found.
    """
    if not cust_id:
        return 0
    try:
        from sqlalchemy import select
        from utils.db import get_sessionmaker
        from utils.models import StripeCustomer
    except Exception:
        return 0

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(StripeCustomer.discord_user_id)
            .where(StripeCustomer.stripe_customer_id == cust_id)
            .limit(1)
        )
        row = res.first()
        if row:
            return int(row[0] or 0)
    return 0


async def handle_charge_refunded(charge: dict) -> None:
    """Handle charge.refunded — reverse the original purchase.

    Policy:
    - pro_subscription  -> deactivate premium, DM user
    - points_purchase   -> deduct points (clamped to 0), DM user
    - gift_purchase     -> deactivate recipient premium, DM both
    """
    cust_id = str(charge.get("customer") or "")
    user_id = await _lookup_user_by_customer(cust_id)
    metadata = charge.get("metadata") or {}
    purchase_type = metadata.get("type", "")
    amount_refunded = charge.get("amount_refunded", 0)

    if not user_id:
        fallback_uid = int(metadata.get("user_id") or 0)
        if fallback_uid:
            user_id = fallback_uid
        else:
            log.warning(
                "charge.refunded: could not resolve customer %s to a Discord user", cust_id
            )
            await _notify_owners(
                f"\u26a0\ufe0f **Stripe Refund (unknown user)**\n"
                f"Customer: `{cust_id}`\n"
                f"Amount refunded: ${amount_refunded / 100:.2f}\n"
                f"Metadata: `{metadata}`"
            )
            return

    log.info(
        "charge.refunded: user=%s type=%s amount_refunded=%s customer=%s",
        user_id, purchase_type, amount_refunded, cust_id,
    )

    if purchase_type == "pro_subscription":
        await _deactivate_premium(user_id=user_id)
        await _try_dm_user(
            user_id,
            "Your Bot-Nardology Pro subscription payment was **refunded**. "
            "Your Pro access has been deactivated.",
        )

    elif purchase_type == "points_purchase":
        points = int(metadata.get("points_amount") or 0)
        if points > 0:
            from utils.points_store import adjust_points, get_balance
            old_bal = await get_balance(guild_id=0, user_id=user_id)
            new_bal = await adjust_points(
                guild_id=0,
                user_id=user_id,
                delta=-points,
                reason="stripe_refund",
                meta={"refunded_points": points},
            )
            shortfall = points - (old_bal - new_bal)
            if shortfall > 0:
                log.warning(
                    "charge.refunded: user=%s had insufficient points; "
                    "shortfall=%d (wanted -%d, old_bal=%d, new_bal=%d)",
                    user_id, shortfall, points, old_bal, new_bal,
                )
            await _try_dm_user(
                user_id,
                f"A refund of **{points:,} points** has been processed. "
                f"Your new balance is **{new_bal:,}** points.",
            )

    elif purchase_type == "gift_purchase":
        recipient_id = int(metadata.get("recipient_user_id") or 0)
        if recipient_id:
            await _deactivate_premium(user_id=recipient_id)
            await _try_dm_user(
                recipient_id,
                "A Pro gift you received has been **refunded**. "
                "Your Pro access has been deactivated.",
            )
        await _try_dm_user(
            user_id,
            "Your Bot-Nardology Pro gift purchase has been **refunded**.",
        )

    else:
        log.info("charge.refunded: unknown purchase type=%r for user=%s; no reversal action taken", purchase_type, user_id)

    await _notify_owners(
        f"\U0001f4b8 **Stripe Refund**\n"
        f"**User:** <@{user_id}> (ID: {user_id})\n"
        f"**Type:** {purchase_type or 'unknown'}\n"
        f"**Amount refunded:** ${amount_refunded / 100:.2f}\n"
        f"**Customer:** `{cust_id}`"
    )


async def handle_dispute_created(dispute: dict) -> None:
    """Handle charge.dispute.created — auto-ban the user and revoke premium.

    A dispute (chargeback) is a serious event; per policy the user is banned.
    """
    charge_obj = dispute.get("charge") or ""
    cust_id = ""

    if isinstance(charge_obj, dict):
        cust_id = str(charge_obj.get("customer") or "")
    else:
        # charge_obj is just the charge ID string; try to retrieve it
        try:
            stripe = _get_stripe()
            ch = await asyncio.to_thread(stripe.Charge.retrieve, str(charge_obj))
            cust_id = str(ch.get("customer") or "")
        except Exception:
            log.exception("dispute: failed to retrieve charge %s", charge_obj)

    user_id = await _lookup_user_by_customer(cust_id)
    if not user_id:
        log.warning(
            "charge.dispute.created: could not resolve customer %s to a Discord user",
            cust_id,
        )
        await _notify_owners(
            f"\u26a0\ufe0f **Stripe Dispute (unknown user)**\n"
            f"Customer: `{cust_id}`\n"
            f"Dispute ID: `{dispute.get('id', '?')}`"
        )
        return

    log.warning("charge.dispute.created: banning user=%s for chargeback", user_id)

    from utils.mod_actions import ban_user
    await ban_user(user_id=user_id, reason="Stripe dispute/chargeback")
    await _deactivate_premium(user_id=user_id)

    await _try_dm_user(
        user_id,
        "Your account has been **banned** from Bot-Nardology due to a "
        "payment dispute (chargeback). If you believe this is an error, "
        "please contact support.",
    )

    dispute_amount = dispute.get("amount", 0)
    await _notify_owners(
        f"\U0001f6a8 **Stripe Dispute (Chargeback)**\n"
        f"**User:** <@{user_id}> (ID: {user_id})\n"
        f"**Dispute ID:** `{dispute.get('id', '?')}`\n"
        f"**Amount:** ${dispute_amount / 100:.2f}\n"
        f"**Customer:** `{cust_id}`\n"
        f"**Action:** User has been **auto-banned** and premium revoked."
    )


# ---------------------------------------------------------------------------
# aiohttp webhook server
# ---------------------------------------------------------------------------

async def _check_idempotency(event_id: str) -> bool:
    """Return True if this event was already processed. Best-effort via Redis."""
    if not event_id:
        return False
    try:
        from utils.backpressure import get_redis_or_none
        r = await get_redis_or_none()
        if r is None:
            return False
        key = f"stripe:evt:{event_id}"
        already = await r.get(key)
        return bool(already)
    except Exception:
        return False


async def _mark_event_processed(event_id: str) -> None:
    """Record that this event has been processed. Best-effort via Redis (48h TTL)."""
    if not event_id:
        return
    try:
        from utils.backpressure import get_redis_or_none
        r = await get_redis_or_none()
        if r is None:
            return
        key = f"stripe:evt:{event_id}"
        await r.set(key, "1", ex=48 * 3600)
    except Exception:
        pass


async def _handle_stripe_post(request):
    """POST /stripe/webhook — verify signature and dispatch."""
    from aiohttp import web

    # --- Rate limiting ---
    if not _rate_limit_ok():
        log.warning("Webhook rate limit exceeded")
        return web.Response(status=429, text="Too many requests")

    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature", "")

    # --- Signature verification ---
    _is_prod = str(getattr(config, "ENVIRONMENT", "prod")).strip().lower() != "dev"

    if not config.STRIPE_WEBHOOK_SECRET:
        if _is_prod:
            log.error("STRIPE_WEBHOOK_SECRET not set in production; rejecting webhook")
            return web.Response(status=403, text="Webhook secret not configured")
        log.warning("STRIPE_WEBHOOK_SECRET not set (dev mode); accepting webhook without verification")
        try:
            event = json.loads(payload)
        except Exception:
            return web.Response(status=400, text="Bad JSON")
    else:
        stripe = _get_stripe()
        try:
            event = stripe.Webhook.construct_event(
                payload.decode("utf-8"),
                sig_header,
                config.STRIPE_WEBHOOK_SECRET,
            )
        except Exception as exc:
            exc_name = type(exc).__name__
            if "SignatureVerification" in exc_name:
                log.warning("Webhook signature verification failed")
                return web.Response(status=400, text="Invalid signature")
            log.exception("Webhook construction error")
            return web.Response(status=400, text="Bad request")

    event_id = str(event.get("id") or "")
    event_type = event.get("type", "")
    data_obj = (event.get("data") or {}).get("object") or {}

    log.info("Stripe event: %s (id=%s)", event_type, event_id or "?")

    # --- Idempotency: skip already-processed events ---
    if event_id and await _check_idempotency(event_id):
        log.info("Duplicate Stripe event %s (id=%s); skipping", event_type, event_id)
        return web.Response(status=200, text="ok (duplicate)")

    # Debug: log metadata for subscription events
    if event_type in (
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
    ):
        meta = data_obj.get("metadata") if isinstance(data_obj, dict) else None
        log.info("  -> event metadata: %r  sub_id=%s", meta, data_obj.get("id") or data_obj.get("subscription"))

    try:
        if event_type == "checkout.session.completed":
            await handle_checkout_completed(data_obj)
        elif event_type == "invoice.paid":
            await handle_invoice_paid(data_obj)
        elif event_type == "customer.subscription.updated":
            await handle_subscription_updated(data_obj)
        elif event_type == "customer.subscription.deleted":
            await handle_subscription_deleted(data_obj)
        elif event_type == "charge.refunded":
            await handle_charge_refunded(data_obj)
        elif event_type == "charge.dispute.created":
            await handle_dispute_created(data_obj)
        else:
            log.debug("Unhandled Stripe event type: %s", event_type)
    except Exception:
        log.exception("Error handling Stripe event %s (id=%s)", event_type, event_id)
        return web.Response(status=500, text="Internal error")

    # Prometheus: count successfully handled events
    try:
        from utils.prom import stripe_events_total
        stripe_events_total.labels(event_type=event_type).inc()
    except Exception:
        pass

    # Mark event as processed only after successful handling
    await _mark_event_processed(event_id)
    return web.Response(status=200, text="ok")


async def _handle_health(request):
    """GET / — simple health check for Railway."""
    from aiohttp import web
    return web.Response(status=200, text="Bot-Nardology is running")


async def _handle_metrics(request):
    """GET /metrics — Prometheus scrape endpoint."""
    from aiohttp import web
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
    except ImportError:
        return web.Response(status=501, text="prometheus_client not installed")


async def start_webhook_server(bot) -> None:
    """Start the aiohttp web server for Stripe webhooks.

    Called from bot.py setup_hook. Runs in the background.

    IMPORTANT: The health-check endpoint (GET /) is ALWAYS registered so
    Railway can verify the container is alive, even if Stripe is not
    configured.  Without this, Railway stays stuck on "Creating containers".
    """
    global _bot
    _bot = bot

    from aiohttp import web

    app = web.Application()
    app.router.add_get("/", _handle_health)
    app.router.add_get("/metrics", _handle_metrics)

    if config.STRIPE_SECRET_KEY:
        app.router.add_post("/stripe/webhook", _handle_stripe_post)
    else:
        log.info("STRIPE_SECRET_KEY not set; Stripe webhook endpoint disabled (health-check still active).")

    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Webhook/health server listening on 0.0.0.0:%d (stripe=%s)", port, bool(config.STRIPE_SECRET_KEY))
