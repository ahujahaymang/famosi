"""
FastAPI router for payment provider webhook endpoints.

Handles incoming webhook events from Razorpay (India) and Stripe (USA) and
routes them to the PaymentStateMachine in ``app/payment/state_machine.py``.

Endpoints
---------
POST /payment/razorpay/webhook
    Validates the HMAC-SHA256 signature in the ``X-Razorpay-Signature``
    header (Req 13.4) and dispatches ``payment.captured`` events to
    ``state_machine.on_payment_success`` (Req 13.4).

POST /payment/stripe/webhook
    Validates the webhook signature via the Stripe SDK using the
    ``Stripe-Signature`` header (Req 13.4) and routes:
    - ``checkout.session.completed``       → ``state_machine.on_payment_success``
    - ``invoice.payment_failed``           → ``state_machine.on_payment_failure``
    - ``customer.subscription.deleted``    → ``state_machine.on_subscription_expired``
    (Req 13.4, 13.5)

Security
--------
Both endpoints validate provider signatures **before** any DB access.  An
HTTP 400 is returned for invalid signatures; the response body deliberately
contains no detail to avoid leaking information.

All DB mutations are delegated to ``PaymentStateMachine``; this module only
handles HTTP concerns.

Design patterns (matching ``app/api/routes/webhook.py``)
---------------------------------------------------------
- ``state_machine`` import is guarded with a try/except so this module can be
  imported before ``app/payment/state_machine.py`` has been written.
- structlog is used for all log statements; payment identifiers (e.g. order
  IDs, customer IDs) are logged at INFO level, but no PII or card data.
- HTTP 200 is always returned to the payment provider on success so they do
  not re-deliver the event; non-retryable errors return 400.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# PaymentStateMachine import — guarded so main.py can import this module
# before app/payment/state_machine.py has been written (task 27.1).
# ---------------------------------------------------------------------------

try:
    from app.payment.state_machine import PaymentStateMachine  # type: ignore[import]

    _state_machine_available = True
except ImportError:
    _state_machine_available = False
    log.debug(
        "state_machine_not_yet_available",
        hint="app/payment/state_machine.py has not been created yet; "
        "webhook events will be acknowledged but not processed",
    )

# ---------------------------------------------------------------------------
# Razorpay webhook
# ---------------------------------------------------------------------------


def _verify_razorpay_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Validate a Razorpay webhook signature.

    Razorpay computes HMAC-SHA256 over the raw request body using the webhook
    secret and passes the hex digest in ``X-Razorpay-Signature``.

    Returns ``True`` when the computed digest matches ``signature``, using a
    constant-time comparison to prevent timing attacks.
    """
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/razorpay/webhook", status_code=200)
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Receive and process Razorpay webhook events.

    Flow:
      1. Read raw body (required for HMAC verification).
      2. Validate ``X-Razorpay-Signature`` header — 400 on mismatch or
         if the header is absent (Req 13.4).
      3. Parse JSON payload.
      4. Route ``payment.captured`` events to ``state_machine.on_payment_success``.
      5. Return HTTP 200 so Razorpay does not re-deliver the event.

    All other event types are acknowledged (HTTP 200) but not acted on,
    allowing the endpoint to be extended without breakage.
    """
    # -- 1. Read raw body --------------------------------------------------
    body: bytes = await request.body()

    # -- 2. Validate signature ---------------------------------------------
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not signature:
        log.warning("razorpay_webhook_missing_signature")
        raise HTTPException(status_code=400, detail="Missing signature")

    webhook_secret = settings.razorpay_webhook_secret
    if not webhook_secret:
        # Webhook secret not configured — reject to prevent insecure processing.
        log.error(
            "razorpay_webhook_secret_not_configured",
            hint="Set RAZORPAY_WEBHOOK_SECRET in .env",
        )
        raise HTTPException(status_code=400, detail="Webhook not configured")

    if not _verify_razorpay_signature(body, signature, webhook_secret):
        log.warning("razorpay_webhook_invalid_signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # -- 3. Parse JSON payload ---------------------------------------------
    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("razorpay_webhook_invalid_json", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event: str = payload.get("event", "")
    log.info("razorpay_webhook_received", event=event)

    # -- 4. Route events ---------------------------------------------------
    if not _state_machine_available:
        log.warning(
            "razorpay_webhook_state_machine_unavailable",
            event=event,
            hint="app/payment/state_machine.py not found; event not processed",
        )
        return Response(content='{"ok":true}', media_type="application/json", status_code=200)

    try:
        if event == "payment.captured":
            await _handle_razorpay_payment_captured(payload, db)
        else:
            log.debug("razorpay_webhook_unhandled_event", event=event)
    except Exception as exc:  # noqa: BLE001
        # Log and swallow so Razorpay receives HTTP 200 and does not retry.
        log.error(
            "razorpay_webhook_processing_error",
            event=event,
            error=str(exc),
            exc_info=True,
        )

    # -- 5. Return HTTP 200 ------------------------------------------------
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)


async def _handle_razorpay_payment_captured(
    payload: dict[str, Any],
    db: AsyncSession,
) -> None:
    """
    Handle a Razorpay ``payment.captured`` event.

    The payment entity contains:
    - ``payload.payment.entity.notes.user_id``  — internal user ID embedded
      in the order notes at checkout creation time.
    - ``payload.payment.entity.subscription_id``  — Razorpay subscription ID
      (may be absent for one-time payments).
    - ``payload.payment.entity.id``  — Razorpay payment ID.

    On success delegates to ``PaymentStateMachine.on_payment_success``.
    """
    entity: dict[str, Any] = (
        payload.get("payload", {}).get("payment", {}).get("entity", {})
    )
    notes: dict[str, Any] = entity.get("notes", {})

    raw_user_id = notes.get("user_id")
    if raw_user_id is None:
        log.warning(
            "razorpay_payment_captured_missing_user_id",
            payment_id=entity.get("id"),
            hint="user_id must be embedded in Razorpay order notes at checkout creation",
        )
        return

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        log.warning(
            "razorpay_payment_captured_invalid_user_id",
            raw_user_id=raw_user_id,
        )
        return

    provider_sub_id: str | None = entity.get("subscription_id") or entity.get("id")

    log.info(
        "razorpay_payment_captured",
        user_id=user_id,
        provider_sub_id=provider_sub_id,
    )

    state_machine = PaymentStateMachine()
    await state_machine.on_payment_success(
        user_id=user_id,
        provider_sub_id=provider_sub_id,
        period_end=None,  # Razorpay one-time; period_end resolved by state machine
        db=db,
    )


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------


@router.post("/stripe/webhook", status_code=200)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Receive and process Stripe webhook events.

    Flow:
      1. Read raw body (Stripe requires the original bytes for signature
         verification — do NOT parse JSON first).
      2. Validate ``Stripe-Signature`` header via ``stripe.Webhook.construct_event``
         — 400 on mismatch or absent header (Req 13.4).
      3. Route events to the appropriate state machine handler:
         - ``checkout.session.completed``     → ``on_payment_success``
         - ``invoice.payment_failed``         → ``on_payment_failure``  (Req 13.5)
         - ``customer.subscription.deleted``  → ``on_subscription_expired``
      4. Return HTTP 200 so Stripe does not re-deliver the event.
    """
    import stripe as _stripe  # local import — not always installed in all environments

    # -- 1. Read raw body --------------------------------------------------
    body: bytes = await request.body()

    # -- 2. Validate signature ---------------------------------------------
    sig_header = request.headers.get("Stripe-Signature", "")
    if not sig_header:
        log.warning("stripe_webhook_missing_signature")
        raise HTTPException(status_code=400, detail="Missing signature")

    webhook_secret = settings.stripe_webhook_secret
    if not webhook_secret:
        log.error(
            "stripe_webhook_secret_not_configured",
            hint="Set STRIPE_WEBHOOK_SECRET in .env",
        )
        raise HTTPException(status_code=400, detail="Webhook not configured")

    try:
        event = _stripe.Webhook.construct_event(
            payload=body,
            sig_header=sig_header,
            secret=webhook_secret,
        )
    except _stripe.error.SignatureVerificationError as exc:
        log.warning("stripe_webhook_invalid_signature", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid signature") from exc
    except ValueError as exc:
        log.warning("stripe_webhook_invalid_payload", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    event_type: str = event["type"]
    log.info("stripe_webhook_received", event_type=event_type, event_id=event["id"])

    # -- 3. Route events ---------------------------------------------------
    if not _state_machine_available:
        log.warning(
            "stripe_webhook_state_machine_unavailable",
            event_type=event_type,
            hint="app/payment/state_machine.py not found; event not processed",
        )
        return Response(content='{"ok":true}', media_type="application/json", status_code=200)

    try:
        if event_type == "checkout.session.completed":
            await _handle_stripe_checkout_completed(event["data"]["object"], db)
        elif event_type == "invoice.payment_failed":
            await _handle_stripe_invoice_payment_failed(event["data"]["object"], db)
        elif event_type == "customer.subscription.deleted":
            await _handle_stripe_subscription_deleted(event["data"]["object"], db)
        else:
            log.debug("stripe_webhook_unhandled_event_type", event_type=event_type)
    except Exception as exc:  # noqa: BLE001
        # Log and swallow so Stripe receives HTTP 200 and does not retry.
        log.error(
            "stripe_webhook_processing_error",
            event_type=event_type,
            error=str(exc),
            exc_info=True,
        )

    # -- 4. Return HTTP 200 ------------------------------------------------
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)


# ---------------------------------------------------------------------------
# Stripe event handlers
# ---------------------------------------------------------------------------


async def _get_user_id_from_stripe_metadata(
    obj: dict[str, Any],
    obj_type: str,
) -> int | None:
    """
    Extract the internal ``user_id`` from a Stripe object's metadata.

    Convention: ``user_id`` is stored in ``metadata.user_id`` on the Checkout
    Session, Invoice, or Subscription object at creation time.
    """
    metadata: dict[str, Any] = obj.get("metadata") or {}
    raw = metadata.get("user_id")
    if raw is None:
        log.warning(
            "stripe_missing_user_id_in_metadata",
            object_type=obj_type,
            object_id=obj.get("id"),
        )
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            "stripe_invalid_user_id_in_metadata",
            object_type=obj_type,
            raw_user_id=raw,
        )
        return None


async def _handle_stripe_checkout_completed(
    session: dict[str, Any],
    db: AsyncSession,
) -> None:
    """
    Handle ``checkout.session.completed``.

    Activates the subscription for the user identified in session metadata.
    ``subscription`` holds the Stripe subscription ID; ``current_period_end``
    is retrieved from the subscription object if available.
    """
    user_id = await _get_user_id_from_stripe_metadata(session, "checkout.session")
    if user_id is None:
        return

    provider_sub_id: str | None = session.get("subscription")

    # Attempt to get period_end from session metadata or subscription expansion.
    # If not present here, state_machine resolves it via a Stripe API call.
    period_end = None

    log.info(
        "stripe_checkout_completed",
        user_id=user_id,
        provider_sub_id=provider_sub_id,
    )

    state_machine = PaymentStateMachine()
    await state_machine.on_payment_success(
        user_id=user_id,
        provider_sub_id=provider_sub_id,
        period_end=period_end,
        db=db,
    )


async def _handle_stripe_invoice_payment_failed(
    invoice: dict[str, Any],
    db: AsyncSession,
) -> None:
    """
    Handle ``invoice.payment_failed``.

    Increments ``payment_retry_count``; if count reaches 3 the state machine
    transitions the user to INACTIVE (Req 13.5).
    """
    user_id = await _get_user_id_from_stripe_metadata(invoice, "invoice")
    if user_id is None:
        return

    log.info(
        "stripe_invoice_payment_failed",
        user_id=user_id,
        invoice_id=invoice.get("id"),
        attempt_count=invoice.get("attempt_count"),
    )

    state_machine = PaymentStateMachine()
    await state_machine.on_payment_failure(user_id=user_id, db=db)


async def _handle_stripe_subscription_deleted(
    subscription: dict[str, Any],
    db: AsyncSession,
) -> None:
    """
    Handle ``customer.subscription.deleted``.

    Transitions the user to INACTIVE and notifies them within 24 hours
    (Req 13.6, delegated to state machine).
    """
    user_id = await _get_user_id_from_stripe_metadata(subscription, "subscription")
    if user_id is None:
        return

    log.info(
        "stripe_subscription_deleted",
        user_id=user_id,
        subscription_id=subscription.get("id"),
    )

    state_machine = PaymentStateMachine()
    await state_machine.on_subscription_expired(user_id=user_id, db=db)
