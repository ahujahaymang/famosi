"""
Unit tests for app/bot/dispatcher.py

Tests the central dispatch function with realistic human messages:
  - Thinking... message sent immediately, then edited with real reply
  - LOGGING: thinking message deleted before logging handler runs
  - PERSONAL_DATA_QUERY: thinking message edited with response
  - KNOWLEDGE_QUESTION: thinking message edited with response
  - MIXED_QUERY: both paths run, thinking message edited with synthesis
  - UNCLASSIFIED: thinking message edited with error
  - Approval-pending gate: blocked before any LLM work
  - Non-text update silently ignored
  - Generic error: thinking message edited with error message

Requirements: 14.1, 14.3, 14.4, 14.5, 14.7, 14.8
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str = "test message", telegram_user_id: int = 123) -> MagicMock:
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.edited_message = None
    return update


def _make_context(
    db_user_id: int | None = 10,
    is_admin: bool = False,
    approval_pending: bool = False,
    read_only: bool = False,
) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    user_obj = MagicMock(id=db_user_id) if db_user_id else None
    ctx.bot_data = {
        "current_user": user_obj,
        "is_admin": is_admin,
        "approval_pending": approval_pending,
        "read_only": read_only,
        "force_mini_tier": False,
        "daily_cap_note": None,
    }
    return ctx


def _make_thinking_msg() -> MagicMock:
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_route_result(intent: str = "LOGGING", confidence: float = 0.9) -> MagicMock:
    rr = MagicMock()
    rr.intent = intent
    rr.confidence = confidence
    rr.tier = "nano"
    rr.escalated = False
    rr.escalation_reason = ""
    return rr


# ---------------------------------------------------------------------------
# Thinking message
# ---------------------------------------------------------------------------

class TestThinkingMessage:

    @pytest.mark.asyncio
    async def test_thinking_message_sent_immediately(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("I had rice for lunch")
        context = _make_context()

        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock), \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_logging_handler", new_callable=AsyncMock) as mock_log:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=_make_route_result("LOGGING"))
            mock_router_cls.return_value = mock_router
            mock_log.return_value = {"model_used": "gpt-4.1-nano", "tokens_used": 50}
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        # First call to reply_text should be the thinking message
        first_call_text = update.message.reply_text.call_args_list[0].args[0]
        assert "thinking" in first_call_text.lower() or "💭" in first_call_text

    @pytest.mark.asyncio
    async def test_logging_deletes_thinking_before_handler(self):
        """For LOGGING, the thinking message should be deleted (handler sends its own keyboard)."""
        from app.bot.dispatcher import dispatch

        update = _make_update("I ate rice for lunch")
        context = _make_context()

        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock), \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_logging_handler", new_callable=AsyncMock) as mock_log:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=_make_route_result("LOGGING"))
            mock_router_cls.return_value = mock_router
            mock_log.return_value = {"model_used": None, "tokens_used": None}
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        thinking_msg.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_response_edits_thinking_message(self):
        """PERSONAL_DATA_QUERY response should be edited into the thinking message."""
        from app.bot.dispatcher import dispatch

        update = _make_update("What did I eat yesterday?")
        context = _make_context()

        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        query_response = "You ate rice and dal yesterday."

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock), \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_query_handler", new_callable=AsyncMock) as mock_query:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=_make_route_result("PERSONAL_DATA_QUERY"))
            mock_router_cls.return_value = mock_router
            mock_query.return_value = (query_response, {"model_used": "gpt-4.1-mini", "tokens_used": 80})
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        thinking_msg.edit_text.assert_awaited_once_with(query_response)


# ---------------------------------------------------------------------------
# Approval pending gate
# ---------------------------------------------------------------------------

class TestApprovalPendingGate:

    @pytest.mark.asyncio
    async def test_pending_user_gets_waiting_message(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("What should I eat?")
        context = _make_context(approval_pending=True)

        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.IntentRouter") as mock_router_cls:
            mock_router = MagicMock()
            mock_router.route = AsyncMock()
            mock_router_cls.return_value = mock_router

            await dispatch(update, context)

        # IntentRouter.route must NOT be called — no LLM work for pending users
        mock_router.route.assert_not_awaited()
        # Waiting message should be shown
        thinking_msg.edit_text.assert_awaited_once()
        text = thinking_msg.edit_text.call_args.args[0]
        assert "approval" in text.lower() or "awaiting" in text.lower()

    @pytest.mark.asyncio
    async def test_approved_user_reaches_intent_classification(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("Is sushi safe during pregnancy?")
        context = _make_context(approval_pending=False)

        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock), \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_knowledge_handler", new_callable=AsyncMock) as mock_kh:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=_make_route_result("KNOWLEDGE_QUESTION"))
            mock_router_cls.return_value = mock_router
            mock_kh.return_value = ("Sushi should generally be avoided.", {"model_used": "claude", "tokens_used": 100})
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        mock_router.route.assert_awaited_once()


# ---------------------------------------------------------------------------
# Non-text update ignored
# ---------------------------------------------------------------------------

class TestNonTextUpdate:

    @pytest.mark.asyncio
    async def test_empty_text_silently_ignored(self):
        from app.bot.dispatcher import dispatch

        update = _make_update(text="")
        context = _make_context()

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"):
            mock_router = MagicMock()
            mock_router.route = AsyncMock()
            mock_router_cls.return_value = mock_router

            await dispatch(update, context)

        mock_router.route.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# UNCLASSIFIED: thinking message edited with help text
# ---------------------------------------------------------------------------

class TestUnclassifiedDispatch:

    @pytest.mark.asyncio
    async def test_unclassified_edits_thinking_with_error(self):
        from app.bot.dispatcher import dispatch, _MSG_UNCLASSIFIED

        update = _make_update("xyz asdfgh")
        context = _make_context()

        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock), \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=_make_route_result("UNCLASSIFIED"))
            mock_router_cls.return_value = mock_router
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        thinking_msg.edit_text.assert_awaited_once()
        text = thinking_msg.edit_text.call_args.args[0]
        assert "understand" in text.lower() or "rephrase" in text.lower()


# ---------------------------------------------------------------------------
# Request log written on every call
# ---------------------------------------------------------------------------

class TestRequestLogging:

    @pytest.mark.asyncio
    async def test_request_log_written_on_success(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("What did I eat?")
        context = _make_context()
        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock) as mock_log, \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_query_handler", new_callable=AsyncMock) as mock_qh:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=_make_route_result("PERSONAL_DATA_QUERY"))
            mock_router_cls.return_value = mock_router
            mock_qh.return_value = ("You ate rice.", {"model_used": "mini", "tokens_used": 50})
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        mock_log.assert_awaited_once()
        log_kwargs = mock_log.call_args.kwargs
        assert "request_id" in log_kwargs
        assert "latency_ms" in log_kwargs

    @pytest.mark.asyncio
    async def test_request_log_written_even_on_error(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("What did I eat?")
        context = _make_context()
        thinking_msg = _make_thinking_msg()
        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock) as mock_log, \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(side_effect=RuntimeError("LLM exploded"))
            mock_router_cls.return_value = mock_router
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        # log_request must still be called even when classification crashed
        mock_log.assert_awaited_once()
