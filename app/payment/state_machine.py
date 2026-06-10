"""
Payment and subscription state machine (Req 13).

Manages the lifecycle:
    new_user → TRIAL (7 days)
    TRIAL + no payment → GRACE (48-hour read-only)
    GRACE expires → INACTIVE
    payment success → ACTIVE
    payment fail (3 retries) → INACTIVE
    subscription ends → INACTIVE

Supported payment regions:
    - India  → Razorpay
    - USA    → Stripe

All DB mutations accept an async SQLAlchemy session (``db``).
Bot notifications accept the ``telegram.Bot`` object from python-telegram-bot
so that reminder / notification messages can be sent within job contexts.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription, SubscriptionStatus

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Region → payment provider mapping (Req 13.3)
# ---------------------------------------------------------------------------

#: ISO 3166-1 alpha-2 country codes that are currently supported.
SUPPORTED_REGIONS: dict[str, str] = {
    "IN": "razorpay",
    "US": "stripe",
}

UNSUPPORTED_REGION_MESSAGE = (
    "Subscription is not yet available in your region. "
    "We currently support India and the USA. "
    "We'll notify you when we expand to more regions."
)

# ---------------------------------------------------------------------------
# Subscription tier copy shown when the trial expires (Req 13.2)
# ---------------------------------------------------------------------------

SUBSCRIPTION_TIERS_MESSAGE = (
    "Your 7-day free trial has ended 🎉\n\n"
    "To continue full access, choose a plan:\n"
    "• *Monthly* — ₹299/month (India) | $4.99/month (USA)\n"
    "• *Annual*  — ₹2,499/year (India) | $39.99/year (USA)\n\n"
    "Reply /subscribe to get started, or keep browsing your existing data "
    "in read-only mode for the next 48 hours."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


async def _get_subscription(user_id: int, db: AsyncSession) -> Optional[Subscription]:
    """Fetch the subscription row for *user_id*, or ``None`` if not found."""
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# PaymentStateMachine
# ---------------------------------------------------------------------------


class PaymentStateMachine:
    """
    Encapsulates all subscription state transitions.

    Each public method corresponds to a lifecycle event.  Methods that send
    Telegram messages accept an optional ``bot`` argument so they can be
    called from background jobs (which have a bot instance) as well as from
    request handlers (which may prefer to handle messaging themselves).

    All methods are ``async`` because they write to the database.
    """

    # ------------------------------------------------------------------
    # Trial activation (Req 13.1)
    # ------------------------------------------------------------------

    async def activate_trial(self, user_id: int, db: AsyncSession) -> Subscription:
        """
        Create a ``Subscription`` row in TRIAL status for *user_id*.

        Called once immediately after onboarding + consent are complete.
        Idempotent: if a subscription row already exists, it is returned
        unchanged to avoid duplicate rows.

        Req 13.1 — trial_start = now(), trial_end = now() + 7 days,
                   subscription_status = 'trial'.
        """
        now = _utcnow()

        existing = await _get_subscription(user_id, db)
        if existing is not None:
            logger.info(
                "activate_trial.already_exists",
                user_id=user_id,
                status=existing.subscription_status,
            )
            return existing

        subscription = Subscription(
            user_id=user_id,
            subscription_status=SubscriptionStatus.trial,
            payment_status="none",
            trial_start=now,
            trial_end=now + timedelta(days=7),
            payment_retry_count=0,
        )
        db.add(subscription)
        await db.flush()  # assign PK without committing outer transaction

        logger.info(
            "activate_trial.created",
            user_id=user_id,
            trial_end=subscription.trial_end.isoformat(),
        )
        return subscription

    # ------------------------------------------------------------------
    # Trial expiry → GRACE (Req 13.2)
    # ------------------------------------------------------------------

    async def on_trial_expired(
        self,
        user_id: int,
        db: AsyncSession,
        bot=None,
    ) -> Optional[Subscription]:
        """
        Transition subscription from TRIAL → GRACE.

        Also presents subscription tier options to the user via Telegram
        (Req 13.2).  If *bot* is ``None``, the Telegram message is skipped
        (caller is responsible for delivering it).

        Returns the updated ``Subscription`` or ``None`` if not found.
        """
        subscription = await _get_subscription(user_id, db)
        if subscription is None:
            logger.warning("on_trial_expired.not_found", user_id=user_id)
            return None

        if subscription.subscription_status != SubscriptionStatus.trial:
            logger.info(
                "on_trial_expired.skipped",
                user_id=user_id,
                current_status=subscription.subscription_status,
            )
            return subscription

        subscription.subscription_status = SubscriptionStatus.grace
        await db.flush()

        logger.info("on_trial_expired.transitioned_to_grace", user_id=user_id)

        # Notify the user about available subscription tiers (Req 13.2)
        if bot is not None:
            try:
                await _send_message(bot, user_id, db, SUBSCRIPTION_TIERS_MESSAGE)
            except Exception:
                logger.exception(
                    "on_trial_expired.notification_failed", user_id=user_id
                )

        return subscription

    # ------------------------------------------------------------------
    # GRACE expiry → INACTIVE
    # ------------------------------------------------------------------

    async def on_grace_expired(
        self,
        user_id: int,
        db: AsyncSession,
        bot=None,
    ) -> Optional[Subscription]:
        """
        Transition subscription from GRACE → INACTIVE once the 48-hour
        grace period has passed without payment.

        If *bot* is provided, the user is notified that access is now
        restricted.
        """
        subscription = await _get_subscription(user_id, db)
        if subscription is None:
            logger.warning("on_grace_expired.not_found", user_id=user_id)
            return None

        if subscription.subscription_status != SubscriptionStatus.grace:
            logger.info(
                "on_grace_expired.skipped",
                user_id=user_id,
                current_status=subscription.subscription_status,
            )
            return subscription

        subscription.subscription_status = SubscriptionStatus.inactive
        await db.flush()

        logger.info("on_grace_expired.transitioned_to_inactive", user_id=user_id)

        if bot is not None:
            message = (
                "Your grace period has ended. Your account is now inactive. "
                "Reply /subscribe to reactivate your subscription and regain full access."
            )
            try:
                await _send_message(bot, user_id, db, message)
            except Exception:
                logger.exception(
                    "on_grace_expired.notification_failed", user_id=user_id
                )

        return subscription

    # ------------------------------------------------------------------
    # Payment success → ACTIVE (Req 13.4)
    # ------------------------------------------------------------------

    async def on_payment_success(
        self,
        user_id: int,
        provider_sub_id: str,
        period_end: datetime,
        db: AsyncSession,
        provider: Optional[str] = None,
        provider_customer_id: Optional[str] = None,
    ) -> Optional[Subscription]:
        """
        Transition subscription to ACTIVE on a successful payment.

        Sets:
          - ``subscription_status`` = 'active'
          - ``provider_sub_id`` = the provider's subscription identifier
          - ``current_period_end`` = billing period end date
          - ``payment_retry_count`` reset to 0
          - ``payment_status`` = 'paid'

        Req 13.4 — update subscription_status to 'active' and record
                   payment_status and subscription end date.
        """
        subscription = await _get_subscription(user_id, db)
        if subscription is None:
            logger.warning("on_payment_success.not_found", user_id=user_id)
            return None

        subscription.subscription_status = SubscriptionStatus.active
        subscription.provider_sub_id = provider_sub_id
        subscription.current_period_end = period_end
        subscription.payment_status = "paid"
        subscription.payment_retry_count = 0

        if provider is not None:
            subscription.payment_provider = provider
        if provider_customer_id is not None:
            subscription.provider_customer_id = provider_customer_id

        await db.flush()

        logger.info(
            "on_payment_success.activated",
            user_id=user_id,
            period_end=period_end.isoformat(),
        )
        return subscription

    # ------------------------------------------------------------------
    # Payment failure with retry logic (Req 13.5)
    # ------------------------------------------------------------------

    async def on_payment_failure(
        self,
        user_id: int,
        db: AsyncSession,
        bot=None,
    ) -> Optional[Subscription]:
        """
        Increment ``payment_retry_count``; deactivate after 3 failures.

        If the retry count reaches 3, ``subscription_status`` is set to
        'inactive' and the user is notified via Telegram.

        Req 13.5 — retry up to 3 times; on 3rd failure set INACTIVE
                   and notify user.
        """
        subscription = await _get_subscription(user_id, db)
        if subscription is None:
            logger.warning("on_payment_failure.not_found", user_id=user_id)
            return None

        subscription.payment_retry_count += 1
        subscription.payment_status = "failed"

        logger.info(
            "on_payment_failure.retry_incremented",
            user_id=user_id,
            retry_count=subscription.payment_retry_count,
        )

        if subscription.payment_retry_count >= 3:
            subscription.subscription_status = SubscriptionStatus.inactive

            logger.info(
                "on_payment_failure.deactivated",
                user_id=user_id,
                retry_count=subscription.payment_retry_count,
            )

            # Notify the user (Req 13.5)
            if bot is not None:
                message = (
                    "We were unable to process your payment after 3 attempts. "
                    "Your subscription has been deactivated. "
                    "Please update your payment method and reply /subscribe to reactivate."
                )
                try:
                    await _send_message(bot, user_id, db, message)
                except Exception:
                    logger.exception(
                        "on_payment_failure.notification_failed", user_id=user_id
                    )

        await db.flush()
        return subscription

    # ------------------------------------------------------------------
    # Subscription expiry → INACTIVE (Req 13.6)
    # ------------------------------------------------------------------

    async def on_subscription_expired(
        self,
        user_id: int,
        db: AsyncSession,
        bot=None,
    ) -> Optional[Subscription]:
        """
        Mark subscription INACTIVE when the billing period ends without renewal.

        Notifies the user within 24 hours (Req 13.6).  If *bot* is provided
        the notification is sent immediately; otherwise the caller is
        responsible for delivery within the 24-hour window.
        """
        subscription = await _get_subscription(user_id, db)
        if subscription is None:
            logger.warning("on_subscription_expired.not_found", user_id=user_id)
            return None

        subscription.subscription_status = SubscriptionStatus.inactive
        subscription.payment_status = "expired"
        await db.flush()

        logger.info("on_subscription_expired.deactivated", user_id=user_id)

        # Notify user within 24 hours (Req 13.6)
        if bot is not None:
            message = (
                "Your subscription has ended. "
                "Reply /subscribe to renew and continue full access to Famosi."
            )
            try:
                await _send_message(bot, user_id, db, message)
            except Exception:
                logger.exception(
                    "on_subscription_expired.notification_failed", user_id=user_id
                )

        return subscription

    # ------------------------------------------------------------------
    # Renewal reminder (Req 13.7)
    # ------------------------------------------------------------------

    async def check_renewal_reminder(
        self,
        user_id: int,
        db: AsyncSession,
        bot,
    ) -> bool:
        """
        Send a once-per-day renewal reminder if the subscription is within
        7 days of ``current_period_end``.

        Returns ``True`` if a reminder was sent, ``False`` otherwise.

        Idempotency: the caller (typically a daily job) should ensure this
        method is invoked at most once per user per day; this method does not
        track whether a reminder was already sent today to keep the state
        machine stateless.  The job layer is responsible for that guard.

        Req 13.7 — send renewal reminder once per day while subscription is
                   within 7 days of expiry.
        """
        subscription = await _get_subscription(user_id, db)
        if subscription is None:
            logger.debug("check_renewal_reminder.not_found", user_id=user_id)
            return False

        if subscription.subscription_status != SubscriptionStatus.active:
            return False

        if subscription.current_period_end is None:
            return False

        now = _utcnow()
        days_until_expiry = (subscription.current_period_end - now).days

        if 0 <= days_until_expiry <= 7:
            message = (
                f"⏰ Your Famosi subscription expires in "
                f"{days_until_expiry} day{'s' if days_until_expiry != 1 else ''}. "
                "Reply /subscribe to renew and keep uninterrupted access."
            )
            try:
                await _send_message(bot, user_id, db, message)
                logger.info(
                    "check_renewal_reminder.sent",
                    user_id=user_id,
                    days_until_expiry=days_until_expiry,
                )
                return True
            except Exception:
                logger.exception(
                    "check_renewal_reminder.notification_failed", user_id=user_id
                )

        return False

    # ------------------------------------------------------------------
    # Region check (Req 13.3)
    # ------------------------------------------------------------------

    @staticmethod
    def get_payment_provider(country: str) -> Optional[str]:
        """
        Return the payment provider name for *country* (ISO 3166-1 alpha-2).

        Returns ``None`` for unsupported regions.  Callers should present
        ``UNSUPPORTED_REGION_MESSAGE`` to the user when ``None`` is returned.

        Req 13.3 — only India (Razorpay) and USA (Stripe) are supported.
        """
        return SUPPORTED_REGIONS.get(country.upper())

    @staticmethod
    def is_region_supported(country: str) -> bool:
        """Return ``True`` if *country* is a supported payment region."""
        return country.upper() in SUPPORTED_REGIONS


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _send_message(bot, user_id: int, db: AsyncSession, text: str) -> None:
    """
    Send a Telegram message to the user identified by *user_id*.

    Resolves ``telegram_user_id`` from the DB users table so that callers
    only need to provide the internal ``user_id`` PK.
    """
    from app.models.user import User  # local import to avoid circular dependency

    result = await db.execute(
        select(User.telegram_user_id).where(User.id == user_id)
    )
    telegram_user_id = result.scalar_one_or_none()
    if telegram_user_id is None:
        logger.warning("_send_message.user_not_found", user_id=user_id)
        return

    await bot.send_message(
        chat_id=telegram_user_id,
        text=text,
        parse_mode="Markdown",
    )
