"""
Razorpay SDK wrapper for Famosi (India payments).

Provides async-compatible helpers around the synchronous Razorpay Python SDK.
Because the SDK uses `requests` (blocking I/O) every network call is offloaded
to a thread pool via `asyncio.to_thread` so the FastAPI event loop is never
blocked.

Requirements covered: 13.3 (order creation), 13.4 (signature verification).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

import razorpay

from app.config import settings

log = logging.getLogger(__name__)


class RazorpayClient:
    """Thin async wrapper around the Razorpay Python SDK.

    A single module-level instance (`razorpay_client`) is exported for use
    throughout the application.  Callers should not instantiate this class
    directly unless they need a client configured with different credentials
    (e.g. tests).
    """

    def __init__(self, key_id: str, key_secret: str) -> None:
        """Initialise the underlying synchronous Razorpay SDK client.

        Args:
            key_id: Razorpay API key ID (``RAZORPAY_KEY_ID`` env var).
            key_secret: Razorpay API key secret (``RAZORPAY_KEY_SECRET`` env var).
        """
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._key_secret = key_secret

    # ------------------------------------------------------------------
    # Order creation — Requirement 13.3
    # ------------------------------------------------------------------

    async def create_order(
        self,
        amount: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a Razorpay order and return the order dict.

        The ``amount`` must be expressed in the **smallest currency unit**
        (paise for INR).  For example, ₹499 → ``amount=49900``.

        Args:
            amount: Order amount in the smallest currency unit (e.g. paise).
            currency: ISO 4217 currency code; defaults to ``"INR"``.
            receipt: Optional merchant receipt identifier (max 40 chars).
            notes: Optional key-value metadata attached to the order.
            **kwargs: Any additional parameters forwarded to the Razorpay
                ``orders.create`` API.

        Returns:
            The Razorpay order object as a dictionary (includes ``id``,
            ``status``, ``amount``, ``currency``, etc.).

        Raises:
            razorpay.errors.BadRequestError: On invalid request parameters.
            razorpay.errors.ServerError: On upstream Razorpay server errors.
        """
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
        }
        if receipt is not None:
            payload["receipt"] = receipt
        if notes is not None:
            payload["notes"] = notes
        payload.update(kwargs)

        log.debug("razorpay_create_order", extra={"currency": currency})
        # The SDK is synchronous; run it in a thread to stay non-blocking.
        order: dict[str, Any] = await asyncio.to_thread(
            self._client.order.create, payload
        )
        log.debug("razorpay_order_created", extra={"order_id": order.get("id")})
        return order

    # ------------------------------------------------------------------
    # Signature verification — Requirement 13.4
    # ------------------------------------------------------------------

    def verify_signature(
        self,
        payload: str,
        signature: str,
        secret: str | None = None,
    ) -> bool:
        """Verify an HMAC-SHA256 Razorpay webhook or payment signature.

        This is a **synchronous** method because signature verification is
        purely computational — it never makes network requests.  Callers that
        need an awaitable wrapper can call ``asyncio.to_thread`` themselves,
        though it is typically not necessary.

        The algorithm (per Razorpay docs):

        1. Compute ``HMAC-SHA256(payload, secret)`` using the webhook secret.
        2. Hex-encode the digest.
        3. Compare with the provided ``signature`` using a constant-time
           comparison to prevent timing attacks.

        Args:
            payload: The raw request body string (or the pipe-delimited
                composite message for payment/subscription signatures).
            signature: The ``X-Razorpay-Signature`` header value (hex string).
            secret: The HMAC secret to use.  Defaults to
                ``settings.razorpay_webhook_secret`` when ``None``.

        Returns:
            ``True`` when the signature is valid; ``False`` otherwise.
        """
        effective_secret = secret if secret is not None else settings.razorpay_webhook_secret

        try:
            key_bytes = effective_secret.encode("utf-8")
            msg_bytes = payload.encode("utf-8")
            expected = hmac.new(
                key=key_bytes,
                msg=msg_bytes,
                digestmod=hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception as exc:  # pragma: no cover
            log.warning(
                "razorpay_signature_verification_error",
                extra={"error": str(exc)},
            )
            return False

    # ------------------------------------------------------------------
    # Convenience: verify a payment signature (order_id + payment_id)
    # ------------------------------------------------------------------

    def verify_payment_signature(
        self,
        order_id: str,
        payment_id: str,
        signature: str,
    ) -> bool:
        """Verify the signature returned after a successful payment capture.

        Razorpay signs the composite message ``"<order_id>|<payment_id>"``
        using the **key secret** (not the webhook secret).

        Args:
            order_id: Razorpay order ID (``razorpay_order_id``).
            payment_id: Razorpay payment ID (``razorpay_payment_id``).
            signature: The ``razorpay_signature`` value from the payment
                callback.

        Returns:
            ``True`` when the signature is valid; ``False`` otherwise.
        """
        msg = f"{order_id}|{payment_id}"
        return self.verify_signature(msg, signature, secret=self._key_secret)


# ---------------------------------------------------------------------------
# Module-level singleton — import this in other modules.
# ---------------------------------------------------------------------------

razorpay_client = RazorpayClient(
    key_id=settings.razorpay_key_id,
    key_secret=settings.razorpay_key_secret,
)
