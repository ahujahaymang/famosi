"""
Subscription / payment handler for Famosi.

Exposes the ``/subscribe`` command which:
1. Detects the user's country from their stored ``User.country`` field.
2. Routes to Razorpay (India — ``IN``) or Stripe (USA — ``US``).
3. Presents an inline keyboard so the user can pick Monthly or Annual tier.
4. On tier selection, creates an order / checkout session with the appropriate
   payment client and sends the resulting payment link back to the user.

Pricing
-------
+----------+---------+---------+
| Tier     | India   | USA     |
+----------+---------+---------+
| Monthly  | ₹299/mo | $4.99/mo|
| Annual   | ₹2499/yr| $39.99/yr|
+----------+---------+---------+

Supported regions (Req 13.3)
----------------------------
- ``IN`` → Razorpay (UPI / cards)
- ``US`` → Stripe (cards)
- Any other ``country`` code → unsupported-region message; no action taken.

Privacy contract
----------------
- NEVER log sensitive payment data (order IDs beyond structural fields,
  customer details, card numbers, UPI VPAs, etc.).
- Log only structural / operational fields: ``telegram_user_id`` (numeric),
  country code, tier, provider name.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app.dependencies import _AsyncSessionFactory
from app.models.user import User

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pricing constants
# ---------------------------------------------------------------------------

# India — Razorpay amounts are in paise (1 INR = 100 paise)
_INR_MONTHLY_PAISE: int = 299_00   # ₹299
_INR_ANNUAL_PAISE: int = 2499_00   # ₹2499

# USA — Stripe amounts are in cents (1 USD = 100 cents)
_USD_MONTHLY_CENTS: int = 499      # $4.99
_USD_ANNUAL_CENTS: int = 3999      # $39.99

# Human-readable price strings for display
_INDIA_PRICES = {
    "monthly": "₹299/month",
    "annual": "₹2,499/year",
}
_USA_PRICES = {
    "monthly": "$4.99/month",
    "annual": "$39.99/year",
}

# Callback data prefixes used on inline keyboard buttons
_CB_TIER_PREFIX = "subscribe_tier:"   # e.g. "subscribe_tier:IN:monthly"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tier_keyboard(country: str) -> InlineKeyboardMarkup:
    """Build the Monthly / Annual selection keyboard for *country*."""
    prices = _INDIA_PRICES if country == "IN" else _USA_PRICES
    keyboard = [
        [
            InlineKeyboardButton(
                f"📅 Monthly — {prices['monthly']}",
                callback_data=f"{_CB_TIER_PREFIX}{country}:monthly",
            ),
        ],
        [
            InlineKeyboardButton(
                f"🗓️ Annual — {prices['annual']} (save ~16%)" if country == "IN"
                else f"🗓️ Annual — {prices['annual']} (save ~33%)",
                callback_data=f"{_CB_TIER_PREFIX}{country}:annual",
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _get_user(telegram_user_id: int) -> User | None:
    """Load the ``User`` row for *telegram_user_id*; return ``None`` if absent."""
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# /subscribe command
# ---------------------------------------------------------------------------

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle ``/subscribe``.

    Loads the user's country from the DB.  Routes to the correct provider or
    shows an unsupported-region message (Req 13.3).
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_user_id = update.effective_user.id
    user = await _get_user(telegram_user_id)

    if user is None or not user.onboarding_complete:
        await update.message.reply_text(
            "Please complete onboarding first with /start before subscribing."
        )
        return

    country = (user.country or "").upper()
    logger.info(
        "subscribe_command_received",
        telegram_user_id=telegram_user_id,
        country=country,
    )

    if country == "IN":
        provider_label = "Razorpay (UPI / cards)"
        prices = _INDIA_PRICES
    elif country == "US":
        provider_label = "Stripe (cards)"
        prices = _USA_PRICES
    else:
        # Unsupported region — inform the user and take no further action (Req 13.3)
        logger.info(
            "subscribe_unsupported_region",
            telegram_user_id=telegram_user_id,
            country=country,
        )
        await update.message.reply_text(
            "🌍 Subscriptions are currently available only for users in India (IN) "
            "and the United States (US).\n\n"
            "We're working to expand to more regions soon. Thank you for your patience!"
        )
        return

    text = (
        f"💳 *Famosi Subscription*\n\n"
        f"Payment provider: {provider_label}\n\n"
        f"Choose your plan:\n"
        f"• Monthly — {prices['monthly']}\n"
        f"• Annual  — {prices['annual']}\n\n"
        "Select a plan below to continue:"
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_tier_keyboard(country),
    )


# ---------------------------------------------------------------------------
# Tier selection callback
# ---------------------------------------------------------------------------

async def handle_tier_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle inline keyboard callbacks for tier selection.

    Callback data format: ``subscribe_tier:{COUNTRY}:{TIER}``
    e.g. ``subscribe_tier:IN:monthly`` or ``subscribe_tier:US:annual``

    Creates the appropriate payment order/session and replies with the link.
    """
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    assert update.effective_user is not None
    telegram_user_id = update.effective_user.id

    # Parse callback data
    raw = query.data or ""
    # Expected: "subscribe_tier:<COUNTRY>:<TIER>"
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != "subscribe_tier":
        logger.warning(
            "subscribe_tier_invalid_callback",
            telegram_user_id=telegram_user_id,
            callback_data=raw,
        )
        await query.edit_message_text("Something went wrong. Please try /subscribe again.")
        return

    _, country, tier = parts
    country = country.upper()
    tier = tier.lower()

    if tier not in ("monthly", "annual"):
        await query.edit_message_text("Invalid tier selected. Please try /subscribe again.")
        return

    logger.info(
        "subscribe_tier_selected",
        telegram_user_id=telegram_user_id,
        country=country,
        tier=tier,
    )

    # Dispatch to the correct payment provider
    if country == "IN":
        await _initiate_razorpay_checkout(query, telegram_user_id, tier)
    elif country == "US":
        await _initiate_stripe_checkout(query, telegram_user_id, tier)
    else:
        await query.edit_message_text(
            "Subscriptions are not available in your region yet."
        )


# ---------------------------------------------------------------------------
# Razorpay checkout (India)
# ---------------------------------------------------------------------------

async def _initiate_razorpay_checkout(
    query,  # telegram.CallbackQuery
    telegram_user_id: int,
    tier: str,
) -> None:
    """
    Create a Razorpay order and send the payment link to the user.

    Uses ``app.payment.razorpay_client.RazorpayClient`` when available.
    Falls back to a configuration-error message when Razorpay credentials
    are missing or the SDK call fails.
    """
    amount_paise = _INR_MONTHLY_PAISE if tier == "monthly" else _INR_ANNUAL_PAISE
    price_label = _INDIA_PRICES[tier]

    try:
        from app.config import settings
        from app.payment.razorpay_client import RazorpayClient  # type: ignore[import]

        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            raise RuntimeError("Razorpay credentials not configured")

        client = RazorpayClient(
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret,
        )
        # Load internal user_id (PK) so the webhook handler can resolve the user.
        user = await _get_user(telegram_user_id)
        internal_user_id = str(user.id) if user else str(telegram_user_id)

        order = await client.create_order(
            amount=amount_paise,
            currency="INR",
            notes={
                "user_id": internal_user_id,
                "tier": tier,
            },
        )
        order_id: str = order.get("id", "")
        payment_link: str = order.get("short_url", "")

        logger.info(
            "razorpay_order_created",
            telegram_user_id=telegram_user_id,
            tier=tier,
        )

        if payment_link:
            await query.edit_message_text(
                f"✅ *{tier.capitalize()} plan — {price_label}*\n\n"
                f"Complete your payment via the link below:\n{payment_link}\n\n"
                "_This link expires in 15 minutes._",
                parse_mode="Markdown",
            )
        else:
            # Razorpay orders do not always return a short_url without
            # Payment Links API; surface the order ID for reference.
            await query.edit_message_text(
                f"✅ *{tier.capitalize()} plan — {price_label}*\n\n"
                f"Your Razorpay Order ID: `{order_id}`\n\n"
                "Please complete payment via the Razorpay checkout. "
                "Contact support if you need assistance.",
                parse_mode="Markdown",
            )

    except ImportError:
        logger.warning(
            "razorpay_client_unavailable",
            telegram_user_id=telegram_user_id,
        )
        await query.edit_message_text(
            "⚠️ Razorpay payments are temporarily unavailable. "
            "Please try again later or contact support."
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "razorpay_order_failed",
            telegram_user_id=telegram_user_id,
            tier=tier,
            error=str(exc),
        )
        await query.edit_message_text(
            "❌ Could not initiate payment. Please try again or contact support."
        )


# ---------------------------------------------------------------------------
# Stripe checkout (USA)
# ---------------------------------------------------------------------------

async def _initiate_stripe_checkout(
    query,  # telegram.CallbackQuery
    telegram_user_id: int,
    tier: str,
) -> None:
    """
    Create a Stripe Checkout Session and send the URL to the user.

    Uses ``app.payment.stripe_client.StripeClient`` when available.
    Falls back to a configuration-error message when Stripe credentials
    are missing or the SDK call fails.
    """
    price_label = _USA_PRICES[tier]

    try:
        from app.config import settings
        from app.payment.stripe_client import create_checkout_session as _stripe_create  # type: ignore[import]

        if not settings.stripe_secret_key:
            raise RuntimeError("Stripe credentials not configured")

        stripe_session = await _stripe_create(
            user_id=telegram_user_id,
            price_id=settings.stripe_price_id or f"price_{tier}",
            success_url="https://t.me/famosi_bot?start=payment_success",
            cancel_url="https://t.me/famosi_bot?start=payment_cancelled",
            subscription_data={"metadata": {"tier": tier}},
        )
        checkout_url: str = getattr(stripe_session, "url", "") or ""

        logger.info(
            "stripe_checkout_session_created",
            telegram_user_id=telegram_user_id,
            tier=tier,
        )

        if checkout_url:
            await query.edit_message_text(
                f"✅ *{tier.capitalize()} plan — {price_label}*\n\n"
                f"Complete your payment via the link below:\n{checkout_url}\n\n"
                "_This link expires in 24 hours._",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"✅ *{tier.capitalize()} plan — {price_label}*\n\n"
                "Payment session created. Please contact support if the "
                "checkout link was not delivered.",
                parse_mode="Markdown",
            )

    except ImportError:
        logger.warning(
            "stripe_client_unavailable",
            telegram_user_id=telegram_user_id,
        )
        await query.edit_message_text(
            "⚠️ Stripe payments are temporarily unavailable. "
            "Please try again later or contact support."
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "stripe_checkout_failed",
            telegram_user_id=telegram_user_id,
            tier=tier,
            error=str(exc),
        )
        await query.edit_message_text(
            "❌ Could not initiate payment. Please try again or contact support."
        )


# ---------------------------------------------------------------------------
# PTB Application registration
# ---------------------------------------------------------------------------

def register(application: Application) -> None:
    """
    Register the ``/subscribe`` command and its tier-selection callbacks on
    *application*.

    Called from ``app.main._register_handlers``::

        from app.bot.handlers.payment_handler import register as register_payment
        register_payment(bot_app)
    """
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(
        CallbackQueryHandler(
            handle_tier_selection,
            pattern=r"^subscribe_tier:[A-Z]{2}:(monthly|annual)$",
        )
    )
    logger.debug("payment_handler_registered")


__all__ = [
    "cmd_subscribe",
    "handle_tier_selection",
    "register",
]
