"""
Unit tests for app/bot/middleware/request_id.py

Tests verify:
  - request_id is a valid UUID4 string assigned per update
  - telegram_user_id is bound when effective_user is present
  - telegram_user_id is NOT bound when effective_user is None
  - structlog context is cleared after handler dispatch (even on error)
  - user name / message content is never bound to context
  - _update_type returns the correct label for each update variant
  - RequestIdMiddleware correctly subclasses Application

Requirements: 14.6, 16.1
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
import structlog.contextvars
from telegram import Update

from app.bot.middleware.request_id import RequestIdMiddleware, _update_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(
    *,
    update_type: str = "message",
    user_id: int | None = 12345,
) -> MagicMock:
    """Build a minimal MagicMock that looks like a telegram.Update."""
    update = MagicMock(spec=Update)

    # Set all known update type fields to None first
    for field in (
        "message",
        "edited_message",
        "callback_query",
        "inline_query",
        "chosen_inline_result",
        "channel_post",
        "edited_channel_post",
        "pre_checkout_query",
        "shipping_query",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
    ):
        setattr(update, field, None)

    # Set the requested update type
    setattr(update, update_type, MagicMock())

    # effective_user
    if user_id is not None:
        mock_user = MagicMock()
        mock_user.id = user_id
        # Ensure name/username are NOT accessed (would be a test failure if they were)
        mock_user.first_name = "SHOULD_NOT_APPEAR_IN_LOGS"
        mock_user.username = "SHOULD_NOT_APPEAR_IN_LOGS"
        update.effective_user = mock_user
    else:
        update.effective_user = None

    return update


async def _run_middleware(update: object) -> dict[str, Any]:
    """
    Instantiate RequestIdMiddleware, call process_update, and capture the
    structlog context that was bound during the parent's process_update call.
    """
    captured: dict[str, Any] = {}

    # When patching an instance method on a class, Python passes `self` as
    # the first argument, so the replacement must accept (self, upd).
    async def _fake_parent_process_update(self: Any, upd: object) -> None:
        # Capture what is bound at the time the handler "runs"
        captured.update(structlog.contextvars.get_contextvars())

    with patch.object(
        RequestIdMiddleware.__bases__[0],  # Application
        "process_update",
        new=_fake_parent_process_update,
    ):
        # Build a minimal middleware instance without a real bot
        middleware = object.__new__(RequestIdMiddleware)
        await middleware.process_update(update)

    return captured


# ---------------------------------------------------------------------------
# Tests for _update_type
# ---------------------------------------------------------------------------


class TestUpdateType:
    def test_message(self) -> None:
        assert _update_type(_make_update(update_type="message")) == "message"

    def test_edited_message(self) -> None:
        assert (
            _update_type(_make_update(update_type="edited_message")) == "edited_message"
        )

    def test_callback_query(self) -> None:
        assert (
            _update_type(_make_update(update_type="callback_query")) == "callback_query"
        )

    def test_inline_query(self) -> None:
        assert _update_type(_make_update(update_type="inline_query")) == "inline_query"

    def test_poll(self) -> None:
        assert _update_type(_make_update(update_type="poll")) == "poll"

    def test_poll_answer(self) -> None:
        assert _update_type(_make_update(update_type="poll_answer")) == "poll_answer"

    def test_non_update_object_returns_unknown(self) -> None:
        assert _update_type("not_an_update") == "unknown"
        assert _update_type(42) == "unknown"
        assert _update_type(None) == "unknown"

    def test_all_fields_none_returns_other(self) -> None:
        update = _make_update(update_type="message")
        update.message = None  # turn off the one we set
        assert _update_type(update) == "other"


# ---------------------------------------------------------------------------
# Tests for RequestIdMiddleware
# ---------------------------------------------------------------------------


class TestRequestIdMiddleware:
    """Tests that verify context binding behaviour of the middleware."""

    @pytest.mark.asyncio
    async def test_request_id_is_uuid4(self) -> None:
        """Every update must get a valid UUID4 request_id."""
        captured = await _run_middleware(_make_update())

        assert "request_id" in captured
        # Must be a valid UUID
        parsed = uuid.UUID(captured["request_id"], version=4)
        assert str(parsed) == captured["request_id"]

    @pytest.mark.asyncio
    async def test_request_id_unique_per_update(self) -> None:
        """Two successive updates must receive different request_ids."""
        captured1 = await _run_middleware(_make_update())
        captured2 = await _run_middleware(_make_update())

        assert captured1["request_id"] != captured2["request_id"]

    @pytest.mark.asyncio
    async def test_telegram_user_id_bound_when_present(self) -> None:
        """telegram_user_id must be bound when effective_user is available."""
        captured = await _run_middleware(_make_update(user_id=99999))

        assert "telegram_user_id" in captured
        assert captured["telegram_user_id"] == 99999

    @pytest.mark.asyncio
    async def test_telegram_user_id_not_bound_when_absent(self) -> None:
        """telegram_user_id must NOT be bound when effective_user is None."""
        captured = await _run_middleware(_make_update(user_id=None))

        assert "telegram_user_id" not in captured

    @pytest.mark.asyncio
    async def test_user_name_never_bound(self) -> None:
        """User name, username, and first_name must never appear in context."""
        captured = await _run_middleware(_make_update(user_id=12345))

        for key in ("first_name", "last_name", "username", "name", "user_name"):
            assert key not in captured, f"Privacy violation: '{key}' found in context"

    @pytest.mark.asyncio
    async def test_no_message_content_in_context(self) -> None:
        """
        Message content keys must never be bound to context
        (privacy contract from app/main.py).
        """
        captured = await _run_middleware(_make_update())

        prohibited_keys = {
            "text", "message_text", "user_message", "raw_text",
            "food_name", "symptom_name", "medication_name",
        }
        for key in prohibited_keys:
            assert key not in captured, f"Privacy violation: '{key}' found in context"

    @pytest.mark.asyncio
    async def test_context_cleared_after_handler(self) -> None:
        """
        After process_update returns the structlog context must be empty
        so there is no leakage into subsequent work.
        """
        await _run_middleware(_make_update(user_id=42))

        remaining = structlog.contextvars.get_contextvars()
        assert remaining == {}, f"Context leak detected: {remaining}"

    @pytest.mark.asyncio
    async def test_context_cleared_even_on_handler_error(self) -> None:
        """
        Context must be cleared even when the handler raises an exception.
        """

        async def _raising_parent_process_update(self: Any, upd: object) -> None:
            raise RuntimeError("handler exploded")

        with patch.object(
            RequestIdMiddleware.__bases__[0],
            "process_update",
            new=_raising_parent_process_update,
        ):
            middleware = object.__new__(RequestIdMiddleware)
            with pytest.raises(RuntimeError, match="handler exploded"):
                await middleware.process_update(_make_update())

        remaining = structlog.contextvars.get_contextvars()
        assert remaining == {}, f"Context leak after exception: {remaining}"

    def test_is_application_subclass(self) -> None:
        """RequestIdMiddleware must subclass Application for PTB compatibility."""
        from telegram.ext import Application

        assert issubclass(RequestIdMiddleware, Application)
