"""
Stripe SDK wrapper for Famosi (USA payments).

Provides two async-compatible functions:
  - create_checkout_session: Create a Stripe Checkout Session for a user subscription.
  - verify_webhook: Validate an incoming Stripe webhook signature.

Requirements: 13.3, 13.4
"""

import asyncio
from functools import partial
from typing import Any

import stripe
import stripe.error
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)


def _get_stripe_client() -> stripe.StripeClient:
    """Return a configured Stripe client using the secret key from settings."""
    return stripe.StripeClient(api_key=settings.stripe_secret_key)


async def create_checkout_session(
    user_id: int,
    price_id: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
    subscription_data: dict[str, Any] | None = None,
) -> stripe.checkout.Session:
    """
    Create a Stripe Checkout Session for a subscription purchase.

    This wraps the synchronous Stripe SDK call in an executor so it is safe
    to await inside async FastAPI handlers without blocking the event loop.

    Args:
        user_id: Internal user ID — stored as metadata on the session.
        price_id: Stripe Price ID (e.g. ``price_xxx``) for the subscription tier.
        success_url: URL Stripe redirects the customer to after successful payment.
        cancel_url: URL Stripe redirects the customer to if they cancel checkout.
        customer_email: Pre-fill the customer's email on the Checkout page.
        subscription_data: Optional extra kwargs forwarded to
            ``subscription_data`` on the session (e.g. ``trial_period_days``).

    Returns:
        A ``stripe.checkout.Session`` object. Access ``session.url`` to get the
        redirect URL to send to the user.

    Raises:
        stripe.error.StripeError: On any Stripe API error.
    """
    client = _get_stripe_client()

    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"famosi_user_id": str(user_id)},
    }

    if customer_email:
        params["customer_email"] = customer_email

    if subscription_data:
        params["subscription_data"] = subscription_data

    loop = asyncio.get_event_loop()
    session: stripe.checkout.Session = await loop.run_in_executor(
        None,
        partial(client.checkout.sessions.create, **params),
    )

    logger.info(
        "stripe_checkout_session_created",
        user_id=user_id,
        session_id=session.id,
        price_id=price_id,
    )

    return session


def verify_webhook(payload: bytes | str, sig_header: str, secret: str) -> bool:
    """
    Validate a Stripe webhook signature using ``stripe.Webhook.construct_event``.

    This is intentionally synchronous because it only performs local HMAC
    verification — no I/O takes place — and is typically called from a
    FastAPI route that already manages its own async context.

    Args:
        payload: Raw request body bytes (or str) received from Stripe.
        sig_header: Value of the ``Stripe-Signature`` HTTP header.
        secret: Webhook endpoint signing secret from the Stripe dashboard
                (maps to ``STRIPE_WEBHOOK_SECRET`` in settings).

    Returns:
        ``True`` if the signature is valid, ``False`` otherwise.
    """
    try:
        stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=secret,
        )
        return True
    except stripe.error.SignatureVerificationError:
        logger.warning("stripe_webhook_signature_invalid")
        return False
    except Exception as exc:  # noqa: BLE001 — intentional broad catch for robustness
        logger.error("stripe_webhook_verification_error", error=str(exc))
        return False
