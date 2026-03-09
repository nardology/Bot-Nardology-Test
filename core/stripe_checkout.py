# core/stripe_checkout.py
"""Stripe Checkout Session creation helpers.

All Stripe API calls are synchronous (the `stripe` SDK is sync-only), so we
wrap them in asyncio.to_thread() to keep the event loop unblocked.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import config

log = logging.getLogger("stripe.checkout")

# Lazy-init the stripe module (so the bot doesn't crash if stripe isn't installed yet).
_stripe = None


def _get_stripe():
    global _stripe
    if _stripe is None:
        import stripe as _s
        _s.api_key = config.STRIPE_SECRET_KEY
        _stripe = _s
    return _stripe


# ---------------------------------------------------------------------------
# Stripe Customer reuse
# ---------------------------------------------------------------------------

async def get_or_create_stripe_customer(*, discord_user_id: int, discord_username: str = "") -> str:
    """Return an existing Stripe customer ID for this Discord user, or create one.

    Looks up the mapping in Postgres first; falls back to creating a new
    Stripe customer and persisting the mapping.
    """
    from utils.db import get_sessionmaker
    from utils.models import StripeCustomer

    try:
        from sqlalchemy import select
    except Exception:
        raise RuntimeError("sqlalchemy is required for Stripe integration")

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(StripeCustomer.stripe_customer_id)
            .where(StripeCustomer.discord_user_id == int(discord_user_id))
            .limit(1)
        )
        row = res.scalar_one_or_none()
        if row:
            return str(row)

    # No existing mapping -- create a Stripe customer.
    stripe = _get_stripe()
    cust = await asyncio.to_thread(
        stripe.Customer.create,
        metadata={"discord_user_id": str(discord_user_id)},
        name=discord_username or f"Discord User {discord_user_id}",
    )
    cust_id = str(cust["id"])

    # Persist the mapping.
    async with Session() as session:
        session.add(StripeCustomer(
            discord_user_id=int(discord_user_id),
            stripe_customer_id=cust_id,
        ))
        try:
            await session.commit()
        except Exception:
            # Race condition: another request might have inserted first.
            await session.rollback()
            async with Session() as s2:
                res2 = await s2.execute(
                    select(StripeCustomer.stripe_customer_id)
                    .where(StripeCustomer.discord_user_id == int(discord_user_id))
                    .limit(1)
                )
                existing = res2.scalar_one_or_none()
                if existing:
                    return str(existing)
            # If we still can't find it, just return the one we created.

    return cust_id


# ---------------------------------------------------------------------------
# Pro subscription checkout (now user-level)
# ---------------------------------------------------------------------------

async def create_pro_checkout(
    *,
    user_id: int,
    username: str = "",
) -> str:
    """Create a Stripe Checkout Session for the $5/month Pro subscription.

    Premium is now per-user. Returns the checkout URL.
    """
    if not config.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    if not config.STRIPE_PRICE_PRO_MONTHLY:
        raise RuntimeError("STRIPE_PRICE_PRO_MONTHLY is not configured")

    stripe = _get_stripe()
    customer_id = await get_or_create_stripe_customer(
        discord_user_id=user_id,
        discord_username=username,
    )

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": config.STRIPE_PRICE_PRO_MONTHLY, "quantity": 1}],
        metadata={
            "type": "pro_subscription",
            "user_id": str(user_id),
        },
        subscription_data={
            "metadata": {
                "type": "pro_subscription",
                "user_id": str(user_id),
            },
        },
        success_url=config.STRIPE_SUCCESS_URL,
        cancel_url=config.STRIPE_CANCEL_URL,
    )
    url = str(session.get("url") or "")
    if not url:
        raise RuntimeError("Stripe did not return a checkout URL")
    return url


# ---------------------------------------------------------------------------
# Gift checkout (one-time payment for gifted premium months)
# ---------------------------------------------------------------------------

async def create_gift_checkout(
    *,
    gifter_user_id: int,
    recipient_user_id: int,
    months: int,
    username: str = "",
) -> str:
    """Create a Stripe Checkout Session for a gift premium purchase.

    This is a one-time payment using an inline price_data so we don't
    need a separate Stripe product for gifts. The unit amount comes from
    config.STRIPE_GIFT_UNIT_AMOUNT_CENTS (default $4.99 per month).
    Returns the checkout URL.
    """
    if not config.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")

    stripe = _get_stripe()
    customer_id = await get_or_create_stripe_customer(
        discord_user_id=gifter_user_id,
        discord_username=username,
    )

    unit_amount = int(config.STRIPE_GIFT_UNIT_AMOUNT_CENTS)
    month_count = int(months)

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="payment",
        customer=customer_id,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": f"Bot-Nardology Pro Gift ({month_count} month{'s' if month_count != 1 else ''})",
                },
                "unit_amount": unit_amount,
            },
            "quantity": month_count,
        }],
        metadata={
            "type": "gift_purchase",
            "gifter_user_id": str(gifter_user_id),
            "recipient_user_id": str(recipient_user_id),
            "months": str(month_count),
        },
        success_url=config.STRIPE_SUCCESS_URL,
        cancel_url=config.STRIPE_CANCEL_URL,
    )
    url = str(session.get("url") or "")
    if not url:
        raise RuntimeError("Stripe did not return a checkout URL")
    return url


# ---------------------------------------------------------------------------
# Points purchase checkout
# ---------------------------------------------------------------------------

async def create_points_checkout(
    *,
    guild_id: int,
    user_id: int,
    username: str = "",
    price_id: str,
    points_amount: int,
) -> str:
    """Create a Stripe Checkout Session for a one-time points purchase.

    Returns the checkout URL.
    """
    if not config.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")

    stripe = _get_stripe()
    customer_id = await get_or_create_stripe_customer(
        discord_user_id=user_id,
        discord_username=username,
    )

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="payment",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        metadata={
            "type": "points_purchase",
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "points_amount": str(points_amount),
        },
        success_url=config.STRIPE_SUCCESS_URL,
        cancel_url=config.STRIPE_CANCEL_URL,
    )
    url = str(session.get("url") or "")
    if not url:
        raise RuntimeError("Stripe did not return a checkout URL")
    return url


# ---------------------------------------------------------------------------
# Billing portal (for cancellation / payment method management)
# ---------------------------------------------------------------------------

async def get_billing_portal_url(*, stripe_customer_id: str) -> str:
    """Create a Stripe Billing Portal session and return the URL."""
    if not config.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe = _get_stripe()
    session = await asyncio.to_thread(
        stripe.billing_portal.Session.create,
        customer=stripe_customer_id,
        return_url=config.STRIPE_SUCCESS_URL,
    )
    return str(session.get("url") or "")
