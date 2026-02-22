"""Tests for Stripe webhook event handlers (core/stripe_webhook.py)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to build fake Stripe event payloads
# ---------------------------------------------------------------------------

def _checkout_session(*, event_type: str, user_id: int = 12345, **extra) -> dict:
    base = {
        "id": "cs_test_abc",
        "subscription": "sub_test_123",
        "customer": "cus_test_456",
        "metadata": {"type": event_type, "user_id": str(user_id)},
    }
    base["metadata"].update(extra.pop("extra_metadata", {}))
    base.update(extra)
    return base


def _subscription_obj(*, user_id: int = 12345, cancel: bool = False, period_end_ts: int = 1700000000) -> dict:
    return {
        "id": "sub_test_123",
        "metadata": {"type": "pro_subscription", "user_id": str(user_id)},
        "cancel_at_period_end": cancel,
        "current_period_end": period_end_ts,
        "cancellation_details": {"reason": "test", "comment": ""},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_externals(monkeypatch):
    """Disable DMs, owner notifications, and Stripe API calls for all tests."""
    import core.stripe_webhook as wh

    monkeypatch.setattr(wh, "_try_dm_user", AsyncMock())
    monkeypatch.setattr(wh, "_notify_owners", AsyncMock())

    fake_stripe = MagicMock()
    fake_sub = {
        "current_period_end": 1700000000,
        "metadata": {"type": "pro_subscription", "user_id": "12345"},
    }
    fake_stripe.Subscription.retrieve = MagicMock(return_value=fake_sub)
    monkeypatch.setattr(wh, "_get_stripe", lambda: fake_stripe)


# ---------------------------------------------------------------------------
# handle_checkout_completed
# ---------------------------------------------------------------------------

class TestCheckoutCompleted:

    @pytest.mark.asyncio
    async def test_pro_subscription_activates_premium(self):
        from core.stripe_webhook import handle_checkout_completed

        session_obj = _checkout_session(event_type="pro_subscription")

        with patch("core.stripe_webhook._activate_premium", new_callable=AsyncMock) as mock_act:
            await handle_checkout_completed(session_obj)
            mock_act.assert_awaited_once()
            call_kw = mock_act.call_args.kwargs
            assert call_kw["user_id"] == 12345
            assert call_kw["stripe_sub_id"] == "sub_test_123"
            assert call_kw["stripe_cust_id"] == "cus_test_456"

    @pytest.mark.asyncio
    async def test_missing_user_id_skips(self):
        from core.stripe_webhook import handle_checkout_completed

        session_obj = _checkout_session(event_type="pro_subscription", user_id=0)
        session_obj["metadata"]["user_id"] = ""

        with patch("core.stripe_webhook._activate_premium", new_callable=AsyncMock) as mock_act:
            await handle_checkout_completed(session_obj)
            mock_act.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gift_purchase_activates_gift(self):
        from core.stripe_webhook import handle_checkout_completed

        session_obj = _checkout_session(
            event_type="gift_purchase",
            extra_metadata={
                "gifter_user_id": "111",
                "recipient_user_id": "222",
                "months": "3",
            },
        )

        with patch("core.stripe_webhook.activate_gift_premium", new_callable=AsyncMock, create=True) as mock_gift:
            with patch("utils.premium.activate_gift_premium", new_callable=AsyncMock) as mock_real:
                await handle_checkout_completed(session_obj)
                mock_real.assert_awaited_once()
                kw = mock_real.call_args.kwargs
                assert kw["recipient_user_id"] == 222
                assert kw["gifter_user_id"] == 111
                assert kw["months"] == 3

    @pytest.mark.asyncio
    async def test_points_purchase_credits_wallet(self):
        from core.stripe_webhook import handle_checkout_completed

        session_obj = _checkout_session(
            event_type="points_purchase",
            extra_metadata={"points_amount": "500", "guild_id": "0"},
        )

        with patch("core.stripe_webhook._credit_points", new_callable=AsyncMock) as mock_credit:
            await handle_checkout_completed(session_obj)
            mock_credit.assert_awaited_once()
            kw = mock_credit.call_args.kwargs
            assert kw["user_id"] == 12345
            assert kw["points"] == 500

    @pytest.mark.asyncio
    async def test_unknown_type_is_noop(self):
        from core.stripe_webhook import handle_checkout_completed

        session_obj = _checkout_session(event_type="something_new")

        with patch("core.stripe_webhook._activate_premium", new_callable=AsyncMock) as mock_act:
            with patch("core.stripe_webhook._credit_points", new_callable=AsyncMock) as mock_credit:
                await handle_checkout_completed(session_obj)
                mock_act.assert_not_awaited()
                mock_credit.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_invoice_paid
# ---------------------------------------------------------------------------

class TestInvoicePaid:

    @pytest.mark.asyncio
    async def test_renews_period_end(self):
        from core.stripe_webhook import handle_invoice_paid

        invoice = {"subscription": "sub_test_123"}

        with patch("core.stripe_webhook._update_period_end", new_callable=AsyncMock) as mock_upd:
            await handle_invoice_paid(invoice)
            mock_upd.assert_awaited_once()
            kw = mock_upd.call_args.kwargs
            assert kw["user_id"] == 12345

    @pytest.mark.asyncio
    async def test_no_subscription_skips(self):
        from core.stripe_webhook import handle_invoice_paid

        invoice = {"subscription": ""}

        with patch("core.stripe_webhook._update_period_end", new_callable=AsyncMock) as mock_upd:
            await handle_invoice_paid(invoice)
            mock_upd.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_subscription_updated
# ---------------------------------------------------------------------------

class TestSubscriptionUpdated:

    @pytest.mark.asyncio
    async def test_cancel_at_period_end_updates(self):
        from core.stripe_webhook import handle_subscription_updated

        sub = _subscription_obj(cancel=True, period_end_ts=1700000000)

        with patch("core.stripe_webhook._update_period_end", new_callable=AsyncMock) as mock_upd:
            await handle_subscription_updated(sub)
            mock_upd.assert_awaited_once()
            kw = mock_upd.call_args.kwargs
            assert kw["user_id"] == 12345

    @pytest.mark.asyncio
    async def test_reactivated_updates(self):
        from core.stripe_webhook import handle_subscription_updated

        sub = _subscription_obj(cancel=False, period_end_ts=1700000000)

        with patch("core.stripe_webhook._update_period_end", new_callable=AsyncMock) as mock_upd:
            await handle_subscription_updated(sub)
            mock_upd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_sub_ignored(self):
        from core.stripe_webhook import handle_subscription_updated

        sub = {
            "id": "sub_unknown",
            "metadata": {},
            "cancel_at_period_end": True,
            "current_period_end": 1700000000,
        }

        with patch("core.stripe_webhook._lookup_user_by_subscription", new_callable=AsyncMock, return_value=0):
            with patch("core.stripe_webhook._update_period_end", new_callable=AsyncMock) as mock_upd:
                await handle_subscription_updated(sub)
                mock_upd.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_subscription_deleted
# ---------------------------------------------------------------------------

class TestSubscriptionDeleted:

    @pytest.mark.asyncio
    async def test_deactivates_premium(self):
        from core.stripe_webhook import handle_subscription_deleted

        sub = _subscription_obj()

        with patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact:
            await handle_subscription_deleted(sub)
            mock_deact.assert_awaited_once()
            kw = mock_deact.call_args.kwargs
            assert kw["user_id"] == 12345

    @pytest.mark.asyncio
    async def test_unknown_sub_ignored(self):
        from core.stripe_webhook import handle_subscription_deleted

        sub = {"id": "sub_unknown", "metadata": {}}

        with patch("core.stripe_webhook._lookup_user_by_subscription", new_callable=AsyncMock, return_value=0):
            with patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact:
                await handle_subscription_deleted(sub)
                mock_deact.assert_not_awaited()


# ---------------------------------------------------------------------------
# Rate-limiter utility
# ---------------------------------------------------------------------------

class TestRateLimiter:

    def test_allows_under_limit(self):
        from core.stripe_webhook import _rate_limit_ok, _rate_timestamps
        _rate_timestamps.clear()
        assert _rate_limit_ok() is True

    def test_rejects_over_limit(self):
        import time
        from core.stripe_webhook import _rate_limit_ok, _rate_timestamps, _RATE_LIMIT_MAX
        _rate_timestamps.clear()
        now = time.monotonic()
        for _ in range(_RATE_LIMIT_MAX):
            _rate_timestamps.append(now)
        assert _rate_limit_ok() is False
        _rate_timestamps.clear()
