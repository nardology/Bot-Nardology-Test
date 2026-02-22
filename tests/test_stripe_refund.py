"""Tests for Stripe refund and dispute handlers (core/stripe_webhook.py)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to build fake Stripe payloads
# ---------------------------------------------------------------------------

def _charge(
    *,
    purchase_type: str = "pro_subscription",
    user_id: int = 12345,
    customer: str = "cus_test_456",
    amount_refunded: int = 999,
    extra_metadata: dict | None = None,
) -> dict:
    meta = {"type": purchase_type, "user_id": str(user_id)}
    if extra_metadata:
        meta.update(extra_metadata)
    return {
        "id": "ch_test_abc",
        "customer": customer,
        "amount_refunded": amount_refunded,
        "refunded": True,
        "metadata": meta,
    }


def _dispute(*, customer: str = "cus_test_456", amount: int = 1999) -> dict:
    return {
        "id": "dp_test_001",
        "charge": {
            "id": "ch_test_abc",
            "customer": customer,
        },
        "amount": amount,
    }


def _dispute_charge_id_only(*, charge_id: str = "ch_test_abc", amount: int = 1999) -> dict:
    """Dispute where `charge` is just a string ID (Stripe sometimes sends this)."""
    return {
        "id": "dp_test_002",
        "charge": charge_id,
        "amount": amount,
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
    fake_charge = {"customer": "cus_test_456"}
    fake_stripe.Charge.retrieve = MagicMock(return_value=fake_charge)
    monkeypatch.setattr(wh, "_get_stripe", lambda: fake_stripe)


# ---------------------------------------------------------------------------
# handle_charge_refunded
# ---------------------------------------------------------------------------

class TestChargeRefunded:

    @pytest.mark.asyncio
    async def test_pro_subscription_deactivates_premium(self):
        from core.stripe_webhook import handle_charge_refunded

        charge = _charge(purchase_type="pro_subscription")

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=12345),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
        ):
            await handle_charge_refunded(charge)
            mock_deact.assert_awaited_once()
            assert mock_deact.call_args.kwargs["user_id"] == 12345

    @pytest.mark.asyncio
    async def test_points_purchase_deducts_points(self):
        from core.stripe_webhook import handle_charge_refunded

        charge = _charge(
            purchase_type="points_purchase",
            extra_metadata={"points_amount": "500"},
            amount_refunded=500,
        )

        mock_adjust = AsyncMock(return_value=100)
        mock_balance = AsyncMock(return_value=600)

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=12345),
            patch("core.stripe_webhook.adjust_points", mock_adjust, create=True),
            patch("utils.points_store.adjust_points", mock_adjust),
            patch("utils.points_store.get_balance", mock_balance),
        ):
            await handle_charge_refunded(charge)
            mock_adjust.assert_awaited_once()
            kw = mock_adjust.call_args.kwargs
            assert kw["delta"] == -500
            assert kw["user_id"] == 12345
            assert kw["reason"] == "stripe_refund"

    @pytest.mark.asyncio
    async def test_gift_purchase_deactivates_recipient(self):
        from core.stripe_webhook import handle_charge_refunded

        charge = _charge(
            purchase_type="gift_purchase",
            extra_metadata={"recipient_user_id": "222"},
        )

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=12345),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
        ):
            await handle_charge_refunded(charge)
            mock_deact.assert_awaited_once()
            assert mock_deact.call_args.kwargs["user_id"] == 222

    @pytest.mark.asyncio
    async def test_unknown_customer_logs_warning(self):
        from core.stripe_webhook import handle_charge_refunded, _notify_owners

        charge = _charge(customer="cus_unknown")
        charge["metadata"]["user_id"] = ""

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=0),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
        ):
            await handle_charge_refunded(charge)
            mock_deact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_type_no_crash(self):
        from core.stripe_webhook import handle_charge_refunded

        charge = _charge(purchase_type="something_new")

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=12345),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
        ):
            await handle_charge_refunded(charge)
            mock_deact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_to_metadata_user_id(self):
        """If _lookup_user_by_customer returns 0 but metadata has user_id, use that."""
        from core.stripe_webhook import handle_charge_refunded

        charge = _charge(purchase_type="pro_subscription", user_id=99999)

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=0),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
        ):
            await handle_charge_refunded(charge)
            mock_deact.assert_awaited_once()
            assert mock_deact.call_args.kwargs["user_id"] == 99999


# ---------------------------------------------------------------------------
# handle_dispute_created
# ---------------------------------------------------------------------------

class TestDisputeCreated:

    @pytest.mark.asyncio
    async def test_dispute_bans_user_and_revokes_premium(self):
        from core.stripe_webhook import handle_dispute_created

        dispute = _dispute()

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=12345),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
            patch("utils.mod_actions.ban_user", new_callable=AsyncMock) as mock_ban,
        ):
            await handle_dispute_created(dispute)
            mock_ban.assert_awaited_once()
            ban_kw = mock_ban.call_args.kwargs
            assert ban_kw["user_id"] == 12345
            assert "dispute" in ban_kw["reason"].lower() or "chargeback" in ban_kw["reason"].lower()
            mock_deact.assert_awaited_once()
            assert mock_deact.call_args.kwargs["user_id"] == 12345

    @pytest.mark.asyncio
    async def test_dispute_unknown_customer_no_crash(self):
        from core.stripe_webhook import handle_dispute_created

        dispute = _dispute(customer="cus_unknown")

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=0),
            patch("utils.mod_actions.ban_user", new_callable=AsyncMock) as mock_ban,
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
        ):
            await handle_dispute_created(dispute)
            mock_ban.assert_not_awaited()
            mock_deact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispute_with_charge_id_string(self):
        """When Stripe sends the charge as just an ID string, we retrieve it."""
        from core.stripe_webhook import handle_dispute_created

        dispute = _dispute_charge_id_only()

        with (
            patch("core.stripe_webhook._lookup_user_by_customer", new_callable=AsyncMock, return_value=12345),
            patch("core.stripe_webhook._deactivate_premium", new_callable=AsyncMock) as mock_deact,
            patch("utils.mod_actions.ban_user", new_callable=AsyncMock) as mock_ban,
        ):
            await handle_dispute_created(dispute)
            mock_ban.assert_awaited_once()
            mock_deact.assert_awaited_once()
