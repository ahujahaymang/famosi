"""
Unit tests for app/payment/state_machine.py

Covers:
  - activate_trial: creates subscription, idempotency
  - on_trial_expired: transitions TRIAL → GRACE, skips other statuses
  - on_grace_expired: transitions GRACE → INACTIVE, skips other statuses
  - on_payment_success: transitions any status → ACTIVE, stores metadata
  - on_payment_failure: increments retry count, deactivates at count >= 3
  - on_subscription_expired: sets INACTIVE, updates payment_status
  - check_renewal_reminder: sends reminder within 7-day window, skips outside
  - get_payment_provider / is_region_supported: India, USA, unsupported

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.subscription import Subscription, SubscriptionStatus
from app.payment.state_machine import (
    SUPPORTED_REGIONS,
    UNSUPPORTED_REGION_MESSAGE,
    PaymentStateMachine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_subscription(
    user_id: int = 1,
    status: SubscriptionStatus = SubscriptionStatus.trial,
    retry_count: int = 0,
    current_period_end: datetime | None = None,
    payment_status: str = "none",
) -> MagicMock:
    """
    Build a mock Subscription object with the expected attributes.

    Using MagicMock instead of the ORM class directly avoids the need for
    a live DB session to initialise SQLAlchemy instrumentation.
    """
    now = _utcnow()
    sub = MagicMock(spec=Subscription)
    sub.id = 1
    sub.user_id = user_id
    sub.subscription_status = status
    sub.payment_status = payment_status
    sub.payment_provider = None
    sub.provider_customer_id = None
    sub.provider_sub_id = None
    sub.trial_start = now
    sub.trial_end = now + timedelta(days=7)
    sub.current_period_end = current_period_end
    sub.payment_retry_count = retry_count
    return sub


def _make_db(subscription: Subscription | None) -> AsyncMock:
    """Return a mock AsyncSession that returns *subscription* on select."""
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = subscription
    db.execute.return_value = result_mock
    return db


# ---------------------------------------------------------------------------
# activate_trial
# ---------------------------------------------------------------------------


class TestActivateTrial:
    @pytest.mark.asyncio
    async def test_creates_subscription_with_trial_status(self):
        db = _make_db(None)  # no existing subscription
        sm = PaymentStateMachine()

        result = await sm.activate_trial(user_id=42, db=db)

        db.add.assert_called_once()
        added: Subscription = db.add.call_args[0][0]
        assert added.user_id == 42
        assert added.subscription_status == SubscriptionStatus.trial
        assert added.payment_status == "none"
        assert added.payment_retry_count == 0

    @pytest.mark.asyncio
    async def test_trial_end_is_7_days_after_start(self):
        db = _make_db(None)
        sm = PaymentStateMachine()

        result = await sm.activate_trial(user_id=1, db=db)

        added: Subscription = db.add.call_args[0][0]
        delta = added.trial_end - added.trial_start
        assert delta.days == 7

    @pytest.mark.asyncio
    async def test_idempotent_when_subscription_already_exists(self):
        existing = _make_subscription(user_id=5, status=SubscriptionStatus.active)
        db = _make_db(existing)
        sm = PaymentStateMachine()

        result = await sm.activate_trial(user_id=5, db=db)

        # Should not add a new row
        db.add.assert_not_called()
        assert result is existing


# ---------------------------------------------------------------------------
# on_trial_expired
# ---------------------------------------------------------------------------


class TestOnTrialExpired:
    @pytest.mark.asyncio
    async def test_transitions_trial_to_grace(self):
        sub = _make_subscription(status=SubscriptionStatus.trial)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_trial_expired(user_id=1, db=db)

        assert result.subscription_status == SubscriptionStatus.grace

    @pytest.mark.asyncio
    async def test_skips_transition_if_not_in_trial(self):
        sub = _make_subscription(status=SubscriptionStatus.active)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_trial_expired(user_id=1, db=db)

        assert result.subscription_status == SubscriptionStatus.active

    @pytest.mark.asyncio
    async def test_returns_none_when_subscription_missing(self):
        db = _make_db(None)
        sm = PaymentStateMachine()

        result = await sm.on_trial_expired(user_id=99, db=db)

        assert result is None

    @pytest.mark.asyncio
    async def test_sends_tier_message_when_bot_provided(self):
        sub = _make_subscription(status=SubscriptionStatus.trial)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            await sm.on_trial_expired(user_id=1, db=db, bot=bot)
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_message_when_bot_is_none(self):
        sub = _make_subscription(status=SubscriptionStatus.trial)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            await sm.on_trial_expired(user_id=1, db=db, bot=None)
            mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# on_grace_expired
# ---------------------------------------------------------------------------


class TestOnGraceExpired:
    @pytest.mark.asyncio
    async def test_transitions_grace_to_inactive(self):
        sub = _make_subscription(status=SubscriptionStatus.grace)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_grace_expired(user_id=1, db=db)

        assert result.subscription_status == SubscriptionStatus.inactive

    @pytest.mark.asyncio
    async def test_skips_if_not_in_grace(self):
        sub = _make_subscription(status=SubscriptionStatus.trial)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_grace_expired(user_id=1, db=db)

        assert result.subscription_status == SubscriptionStatus.trial

    @pytest.mark.asyncio
    async def test_returns_none_when_subscription_missing(self):
        db = _make_db(None)
        sm = PaymentStateMachine()

        result = await sm.on_grace_expired(user_id=99, db=db)

        assert result is None


# ---------------------------------------------------------------------------
# on_payment_success
# ---------------------------------------------------------------------------


class TestOnPaymentSuccess:
    @pytest.mark.asyncio
    async def test_sets_status_to_active(self):
        sub = _make_subscription(status=SubscriptionStatus.grace)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        period_end = _utcnow() + timedelta(days=30)

        result = await sm.on_payment_success(
            user_id=1, provider_sub_id="sub_abc123", period_end=period_end, db=db
        )

        assert result.subscription_status == SubscriptionStatus.active

    @pytest.mark.asyncio
    async def test_stores_provider_sub_id_and_period_end(self):
        sub = _make_subscription(status=SubscriptionStatus.trial)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        period_end = _utcnow() + timedelta(days=30)

        result = await sm.on_payment_success(
            user_id=1, provider_sub_id="sub_xyz", period_end=period_end, db=db
        )

        assert result.provider_sub_id == "sub_xyz"
        assert result.current_period_end == period_end

    @pytest.mark.asyncio
    async def test_resets_retry_count_to_zero(self):
        sub = _make_subscription(status=SubscriptionStatus.inactive, retry_count=3)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        period_end = _utcnow() + timedelta(days=30)

        result = await sm.on_payment_success(
            user_id=1, provider_sub_id="sub_001", period_end=period_end, db=db
        )

        assert result.payment_retry_count == 0

    @pytest.mark.asyncio
    async def test_sets_payment_status_to_paid(self):
        sub = _make_subscription(status=SubscriptionStatus.grace)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        period_end = _utcnow() + timedelta(days=30)

        result = await sm.on_payment_success(
            user_id=1, provider_sub_id="sub_001", period_end=period_end, db=db
        )

        assert result.payment_status == "paid"

    @pytest.mark.asyncio
    async def test_returns_none_when_subscription_missing(self):
        db = _make_db(None)
        sm = PaymentStateMachine()

        result = await sm.on_payment_success(
            user_id=99,
            provider_sub_id="sub_001",
            period_end=_utcnow() + timedelta(days=30),
            db=db,
        )

        assert result is None


# ---------------------------------------------------------------------------
# on_payment_failure
# ---------------------------------------------------------------------------


class TestOnPaymentFailure:
    @pytest.mark.asyncio
    async def test_increments_retry_count(self):
        sub = _make_subscription(status=SubscriptionStatus.active, retry_count=0)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_payment_failure(user_id=1, db=db)

        assert result.payment_retry_count == 1
        # Still active after 1 failure
        assert result.subscription_status == SubscriptionStatus.active

    @pytest.mark.asyncio
    async def test_still_active_after_two_failures(self):
        sub = _make_subscription(status=SubscriptionStatus.active, retry_count=1)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_payment_failure(user_id=1, db=db)

        assert result.payment_retry_count == 2
        assert result.subscription_status == SubscriptionStatus.active

    @pytest.mark.asyncio
    async def test_deactivates_after_third_failure(self):
        """Req 13.5 — 3 retries exhausted → INACTIVE."""
        sub = _make_subscription(status=SubscriptionStatus.active, retry_count=2)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_payment_failure(user_id=1, db=db)

        assert result.payment_retry_count == 3
        assert result.subscription_status == SubscriptionStatus.inactive

    @pytest.mark.asyncio
    async def test_notifies_user_when_deactivated(self):
        sub = _make_subscription(status=SubscriptionStatus.active, retry_count=2)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            await sm.on_payment_failure(user_id=1, db=db, bot=bot)
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_notification_on_first_failure(self):
        sub = _make_subscription(status=SubscriptionStatus.active, retry_count=0)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            await sm.on_payment_failure(user_id=1, db=db, bot=bot)
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_subscription_missing(self):
        db = _make_db(None)
        sm = PaymentStateMachine()

        result = await sm.on_payment_failure(user_id=99, db=db)

        assert result is None


# ---------------------------------------------------------------------------
# on_subscription_expired
# ---------------------------------------------------------------------------


class TestOnSubscriptionExpired:
    @pytest.mark.asyncio
    async def test_sets_status_inactive(self):
        sub = _make_subscription(status=SubscriptionStatus.active)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_subscription_expired(user_id=1, db=db)

        assert result.subscription_status == SubscriptionStatus.inactive

    @pytest.mark.asyncio
    async def test_sets_payment_status_expired(self):
        sub = _make_subscription(status=SubscriptionStatus.active)
        db = _make_db(sub)
        sm = PaymentStateMachine()

        result = await sm.on_subscription_expired(user_id=1, db=db)

        assert result.payment_status == "expired"

    @pytest.mark.asyncio
    async def test_notifies_user_within_24h(self):
        """Req 13.6 — notify user via bot when provided."""
        sub = _make_subscription(status=SubscriptionStatus.active)
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            await sm.on_subscription_expired(user_id=1, db=db, bot=bot)
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_subscription_missing(self):
        db = _make_db(None)
        sm = PaymentStateMachine()

        result = await sm.on_subscription_expired(user_id=99, db=db)

        assert result is None


# ---------------------------------------------------------------------------
# check_renewal_reminder
# ---------------------------------------------------------------------------


class TestCheckRenewalReminder:
    @pytest.mark.asyncio
    async def test_sends_reminder_when_within_7_days(self):
        """Req 13.7 — reminder sent when <= 7 days until expiry."""
        period_end = _utcnow() + timedelta(days=5)
        sub = _make_subscription(
            status=SubscriptionStatus.active, current_period_end=period_end
        )
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            sent = await sm.check_renewal_reminder(user_id=1, db=db, bot=bot)
            assert sent is True
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_reminder_when_outside_7_day_window(self):
        period_end = _utcnow() + timedelta(days=30)
        sub = _make_subscription(
            status=SubscriptionStatus.active, current_period_end=period_end
        )
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            sent = await sm.check_renewal_reminder(user_id=1, db=db, bot=bot)
            assert sent is False
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_reminder_when_not_active(self):
        period_end = _utcnow() + timedelta(days=3)
        sub = _make_subscription(
            status=SubscriptionStatus.inactive, current_period_end=period_end
        )
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            sent = await sm.check_renewal_reminder(user_id=1, db=db, bot=bot)
            assert sent is False
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_reminder_when_period_end_is_none(self):
        sub = _make_subscription(
            status=SubscriptionStatus.active, current_period_end=None
        )
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            sent = await sm.check_renewal_reminder(user_id=1, db=db, bot=bot)
            assert sent is False
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_when_subscription_missing(self):
        db = _make_db(None)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        sent = await sm.check_renewal_reminder(user_id=99, db=db, bot=bot)

        assert sent is False

    @pytest.mark.asyncio
    async def test_sends_reminder_on_expiry_day(self):
        """Boundary: 0 days remaining still triggers the reminder."""
        period_end = _utcnow() + timedelta(hours=2)
        sub = _make_subscription(
            status=SubscriptionStatus.active, current_period_end=period_end
        )
        db = _make_db(sub)
        sm = PaymentStateMachine()
        bot = AsyncMock()

        with patch(
            "app.payment.state_machine._send_message", new_callable=AsyncMock
        ) as mock_send:
            sent = await sm.check_renewal_reminder(user_id=1, db=db, bot=bot)
            assert sent is True


# ---------------------------------------------------------------------------
# get_payment_provider / is_region_supported  (Req 13.3)
# ---------------------------------------------------------------------------


class TestRegionSupport:
    def test_india_uses_razorpay(self):
        assert PaymentStateMachine.get_payment_provider("IN") == "razorpay"

    def test_usa_uses_stripe(self):
        assert PaymentStateMachine.get_payment_provider("US") == "stripe"

    def test_unsupported_region_returns_none(self):
        assert PaymentStateMachine.get_payment_provider("GB") is None
        assert PaymentStateMachine.get_payment_provider("AU") is None

    def test_case_insensitive_lookup(self):
        assert PaymentStateMachine.get_payment_provider("in") == "razorpay"
        assert PaymentStateMachine.get_payment_provider("us") == "stripe"

    def test_india_is_supported(self):
        assert PaymentStateMachine.is_region_supported("IN") is True

    def test_usa_is_supported(self):
        assert PaymentStateMachine.is_region_supported("US") is True

    def test_unsupported_region_is_not_supported(self):
        assert PaymentStateMachine.is_region_supported("DE") is False

    def test_unsupported_region_message_is_defined(self):
        assert UNSUPPORTED_REGION_MESSAGE
        assert "India" in UNSUPPORTED_REGION_MESSAGE
        assert "USA" in UNSUPPORTED_REGION_MESSAGE

    def test_supported_regions_dict_contains_both_providers(self):
        assert "IN" in SUPPORTED_REGIONS
        assert "US" in SUPPORTED_REGIONS
        assert SUPPORTED_REGIONS["IN"] == "razorpay"
        assert SUPPORTED_REGIONS["US"] == "stripe"
