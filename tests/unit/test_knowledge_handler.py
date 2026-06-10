"""
Unit tests for app/bot/handlers/knowledge_handler.py

Tests the RAG knowledge pipeline with realistic pregnancy questions.
Covers:
  - Normal RAG path: chunks retrieved → reasoning tier used
  - RAG miss (empty chunks) → LLM fallback with gestational context
  - Partner role: dad_support category filter applied
  - Retrieval failure → None returned (dispatcher handles error)
  - LLM completion failure → graceful fallback

Requirements: 14.4, 15.4, 15.6, 18.1
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(telegram_user_id: int = 77) -> MagicMock:
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context(db_user_id: int | None = 5, role: str = "mom",
                  due_date: date | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    if db_user_id is not None:
        user_obj = MagicMock()
        user_obj.id = db_user_id
        user_obj.role = MagicMock()
        user_obj.role.value = role
        user_obj.due_date = due_date or (date.today() + timedelta(days=100))
        user_obj.lmp_date = None
        ctx.bot_data = {"current_user": user_obj}
    else:
        ctx.bot_data = {"current_user": None}
    return ctx


def _make_llm_client(content: str = "That is safe during pregnancy.") -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = content
    response.model = "anthropic.claude-sonnet-4-5-20251001-v1:0"
    response.tokens_used = 200
    client.complete = AsyncMock(return_value=response)
    return client


def _make_chunk(text: str, doc_id: int = 1) -> MagicMock:
    chunk = MagicMock()
    chunk.content = text
    chunk.document_id = doc_id
    return chunk


# ---------------------------------------------------------------------------
# RAG path: chunks found → reasoning tier
# ---------------------------------------------------------------------------

class TestKnowledgeHandlerWithRAG:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("question", [
        "Is it safe to eat sushi during pregnancy?",
        "Can I exercise in the third trimester?",
        "What foods are rich in folic acid?",
        "Is it normal to feel this tired at 8 weeks?",
        "What should I expect at my 20-week anatomy scan?",
        "How much iron do I need per day when pregnant?",
        "Are headaches normal in the first trimester?",
        "Is it safe to drink herbal tea during pregnancy?",
    ])
    async def test_knowledge_question_returns_answer(self, question: str):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        context = _make_context()
        route_result = MagicMock()
        client = _make_llm_client("Sushi with raw fish should generally be avoided during pregnancy.")

        chunks = [_make_chunk("Raw fish can contain harmful bacteria. ACOG recommends avoiding sushi.")]

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=chunks)

            text, meta = await handle_knowledge_intent(
                update, context, client, route_result, question
            )

        assert text is not None
        assert len(text) > 20
        assert meta["model_used"] is not None

    @pytest.mark.asyncio
    async def test_uses_reasoning_tier_for_medical_questions(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        context = _make_context()
        route_result = MagicMock()
        client = _make_llm_client()

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[_make_chunk("some context")])

            await handle_knowledge_intent(
                update, context, client, route_result, "Is caffeine safe?"
            )

        # complete() was called with "reasoning" tier
        call_args = client.complete.call_args
        assert call_args.args[0] == "reasoning"


# ---------------------------------------------------------------------------
# RAG miss → LLM fallback
# ---------------------------------------------------------------------------

class TestKnowledgeHandlerRAGMiss:

    @pytest.mark.asyncio
    async def test_rag_miss_uses_llm_fallback(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        context = _make_context()
        route_result = MagicMock()
        client = _make_llm_client("That is generally safe during pregnancy, but ask your midwife.")

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[])  # no chunks

            text, meta = await handle_knowledge_intent(
                update, context, client, route_result, "What is the nuchal translucency test?"
            )

        assert text is not None
        # Should still return something useful, not the error message
        assert len(text) > 10

    @pytest.mark.asyncio
    async def test_rag_miss_injects_gestational_context(self):
        """LLM fallback system prompt should include gestational stage."""
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        due_date = date.today() + timedelta(days=140)  # ~20 weeks
        context = _make_context(due_date=due_date)
        route_result = MagicMock()
        client = _make_llm_client()

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[])

            await handle_knowledge_intent(
                update, context, client, route_result, "Is it normal to feel kicks now?"
            )

        # System prompt should mention gestational weeks
        call_args = client.complete.call_args
        messages = call_args.args[1]
        system_msg = next((m for m in messages if m["role"] == "system"), None)
        assert system_msg is not None
        assert "week" in system_msg["content"].lower() or "pregnant" in system_msg["content"].lower()


# ---------------------------------------------------------------------------
# Partner role: dad_support category filter
# ---------------------------------------------------------------------------

class TestPartnerCategoryFilter:

    @pytest.mark.asyncio
    async def test_partner_role_uses_dad_support_filter(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent
        from app.models.knowledge_document import KnowledgeCategory

        update = _make_update()
        context = _make_context(role="partner")
        route_result = MagicMock()
        client = _make_llm_client("Here is how you can support your partner.")

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[_make_chunk("dad support content")])

            await handle_knowledge_intent(
                update, context, client, route_result,
                "How can I support my partner during the second trimester?"
            )

        call_kwargs = mock_retriever.retrieve.call_args.kwargs
        assert call_kwargs.get("category") == KnowledgeCategory.dad_support

    @pytest.mark.asyncio
    async def test_mom_role_no_category_filter(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        context = _make_context(role="mom")
        route_result = MagicMock()
        client = _make_llm_client()

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[_make_chunk("general content")])

            await handle_knowledge_intent(
                update, context, client, route_result, "Is sushi safe?"
            )

        call_kwargs = mock_retriever.retrieve.call_args.kwargs
        assert call_kwargs.get("category") is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestKnowledgeHandlerErrors:

    @pytest.mark.asyncio
    async def test_retrieval_failure_returns_none(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        context = _make_context()
        route_result = MagicMock()
        client = _make_llm_client()

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(side_effect=RuntimeError("pgvector down"))

            text, meta = await handle_knowledge_intent(
                update, context, client, route_result, "Is sushi safe?"
            )

        assert text is None

    @pytest.mark.asyncio
    async def test_llm_failure_after_rag_returns_none(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update()
        context = _make_context()
        route_result = MagicMock()
        client = MagicMock()
        client.complete = AsyncMock(side_effect=RuntimeError("Bedrock down"))

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[_make_chunk("some text")])

            text, meta = await handle_knowledge_intent(
                update, context, client, route_result, "Is caffeine safe?"
            )

        assert text is None
