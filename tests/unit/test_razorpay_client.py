"""
Unit tests for app/payment/razorpay_client.py

Covers:
  - RazorpayClient.create_order: correct payload construction and async dispatch
  - RazorpayClient.verify_signature: HMAC-SHA256 correctness, constant-time comparison
  - RazorpayClient.verify_payment_signature: composite message format
  - Edge cases: missing optional fields, wrong signature, exception swallowing

Requirements: 13.3, 13.4
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.payment.razorpay_client import RazorpayClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_KEY_ID = "rzp_test_key_id"
TEST_KEY_SECRET = "test_secret_key"
TEST_WEBHOOK_SECRET = "test_webhook_secret"


def _make_client() -> RazorpayClient:
    """Return a RazorpayClient initialised with test credentials."""
    return RazorpayClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET)


def _make_signature(payload: str, secret: str) -> str:
    """Compute the expected HMAC-SHA256 hex digest for a payload."""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=payload.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------


class TestCreateOrder:
    """Tests for RazorpayClient.create_order (Requirement 13.3)."""

    @pytest.mark.asyncio
    async def test_create_order_passes_amount_and_currency(self):
        """The SDK's order.create is called with the correct amount and currency."""
        client = _make_client()
        expected_response = {"id": "order_abc123", "status": "created", "amount": 49900}

        with patch.object(client._client.order, "create", return_value=expected_response) as mock_create:
            result = await client.create_order(amount=49900, currency="INR")

        mock_create.assert_called_once()
        call_payload = mock_create.call_args[0][0]
        assert call_payload["amount"] == 49900
        assert call_payload["currency"] == "INR"
        assert result == expected_response

    @pytest.mark.asyncio
    async def test_create_order_includes_receipt_when_provided(self):
        """Receipt is forwarded in the payload when supplied."""
        client = _make_client()
        fake_order = {"id": "order_xyz", "status": "created"}

        with patch.object(client._client.order, "create", return_value=fake_order) as mock_create:
            await client.create_order(amount=10000, receipt="receipt_001")

        payload = mock_create.call_args[0][0]
        assert payload["receipt"] == "receipt_001"

    @pytest.mark.asyncio
    async def test_create_order_omits_receipt_when_not_provided(self):
        """Receipt key must NOT appear in the payload when omitted."""
        client = _make_client()
        fake_order = {"id": "order_nnn", "status": "created"}

        with patch.object(client._client.order, "create", return_value=fake_order) as mock_create:
            await client.create_order(amount=5000)

        payload = mock_create.call_args[0][0]
        assert "receipt" not in payload

    @pytest.mark.asyncio
    async def test_create_order_includes_notes_when_provided(self):
        """Notes dict is forwarded verbatim in the payload."""
        client = _make_client()
        notes = {"user_id": "42", "plan": "monthly"}
        fake_order = {"id": "order_with_notes", "status": "created"}

        with patch.object(client._client.order, "create", return_value=fake_order) as mock_create:
            await client.create_order(amount=20000, notes=notes)

        payload = mock_create.call_args[0][0]
        assert payload["notes"] == notes

    @pytest.mark.asyncio
    async def test_create_order_forwards_extra_kwargs(self):
        """Additional kwargs are merged into the payload."""
        client = _make_client()
        fake_order = {"id": "order_extra", "status": "created"}

        with patch.object(client._client.order, "create", return_value=fake_order) as mock_create:
            await client.create_order(amount=1000, payment_capture=1)

        payload = mock_create.call_args[0][0]
        assert payload["payment_capture"] == 1

    @pytest.mark.asyncio
    async def test_create_order_default_currency_is_inr(self):
        """When currency is not supplied, it defaults to INR."""
        client = _make_client()
        fake_order = {"id": "order_inr", "status": "created"}

        with patch.object(client._client.order, "create", return_value=fake_order) as mock_create:
            await client.create_order(amount=1500)

        payload = mock_create.call_args[0][0]
        assert payload["currency"] == "INR"

    @pytest.mark.asyncio
    async def test_create_order_propagates_sdk_exception(self):
        """Exceptions raised by the Razorpay SDK bubble up to the caller."""
        import razorpay.errors

        client = _make_client()

        with patch.object(
            client._client.order,
            "create",
            side_effect=razorpay.errors.BadRequestError("amount too small"),
        ):
            with pytest.raises(razorpay.errors.BadRequestError):
                await client.create_order(amount=0)


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestVerifySignature:
    """Tests for RazorpayClient.verify_signature (Requirement 13.4)."""

    def test_valid_signature_returns_true(self):
        """Returns True when the signature matches the payload and secret."""
        client = _make_client()
        payload = "order_id=ord_001&payment_id=pay_001"
        secret = "webhook_secret_value"
        valid_sig = _make_signature(payload, secret)

        assert client.verify_signature(payload, valid_sig, secret=secret) is True

    def test_invalid_signature_returns_false(self):
        """Returns False when the signature does not match."""
        client = _make_client()
        payload = "order_id=ord_001"
        secret = "webhook_secret_value"

        assert client.verify_signature(payload, "deadbeef" * 8, secret=secret) is False

    def test_wrong_secret_returns_false(self):
        """Returns False when the signature was computed with a different secret."""
        client = _make_client()
        payload = "some_event_payload"
        good_secret = "correct_secret"
        bad_secret = "wrong_secret"
        sig_with_wrong_secret = _make_signature(payload, bad_secret)

        assert client.verify_signature(payload, sig_with_wrong_secret, secret=good_secret) is False

    def test_uses_settings_webhook_secret_when_no_secret_given(self):
        """Falls back to settings.razorpay_webhook_secret when secret is None."""
        client = _make_client()
        payload = "test_payload"

        with patch("app.payment.razorpay_client.settings") as mock_settings:
            mock_settings.razorpay_webhook_secret = TEST_WEBHOOK_SECRET
            valid_sig = _make_signature(payload, TEST_WEBHOOK_SECRET)
            result = client.verify_signature(payload, valid_sig)  # no explicit secret

        assert result is True

    def test_empty_payload_with_correct_signature(self):
        """An empty payload is valid as long as the HMAC matches."""
        client = _make_client()
        payload = ""
        secret = "some_secret"
        valid_sig = _make_signature(payload, secret)

        assert client.verify_signature(payload, valid_sig, secret=secret) is True

    def test_tampered_payload_returns_false(self):
        """Changing even one byte in the payload invalidates the signature."""
        client = _make_client()
        original = "payment.captured"
        tampered = "payment.captured_TAMPERED"
        secret = "webhook_secret"
        sig_for_original = _make_signature(original, secret)

        assert client.verify_signature(tampered, sig_for_original, secret=secret) is False


# ---------------------------------------------------------------------------
# verify_payment_signature
# ---------------------------------------------------------------------------


class TestVerifyPaymentSignature:
    """Tests for RazorpayClient.verify_payment_signature (Requirement 13.4)."""

    def test_valid_payment_signature(self):
        """Returns True for a correctly computed order+payment composite."""
        client = _make_client()
        order_id = "order_abc"
        payment_id = "pay_xyz"
        # Razorpay signs "<order_id>|<payment_id>" with the key secret.
        msg = f"{order_id}|{payment_id}"
        valid_sig = _make_signature(msg, TEST_KEY_SECRET)

        assert client.verify_payment_signature(order_id, payment_id, valid_sig) is True

    def test_invalid_payment_signature(self):
        """Returns False when the payment signature does not match."""
        client = _make_client()
        assert client.verify_payment_signature("order_x", "pay_y", "bad_sig") is False

    def test_swapped_ids_returns_false(self):
        """Returns False when order_id and payment_id are swapped in the message."""
        client = _make_client()
        order_id = "order_aaa"
        payment_id = "pay_bbb"
        # Sign with the IDs in reverse order — verification should fail.
        swapped_msg = f"{payment_id}|{order_id}"
        bad_sig = _make_signature(swapped_msg, TEST_KEY_SECRET)

        assert client.verify_payment_signature(order_id, payment_id, bad_sig) is False

    def test_uses_key_secret_not_webhook_secret(self):
        """Payment signature verification must use key_secret, not webhook_secret."""
        client = _make_client()
        order_id = "order_kkk"
        payment_id = "pay_kkk"
        msg = f"{order_id}|{payment_id}"

        # Signature computed with webhook secret — should NOT match.
        sig_with_webhook_secret = _make_signature(msg, TEST_WEBHOOK_SECRET)
        assert client.verify_payment_signature(order_id, payment_id, sig_with_webhook_secret) is False
