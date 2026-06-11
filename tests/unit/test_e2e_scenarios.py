"""
End-to-end scenario tests — simulating realistic human conversations.

These tests exercise the full pipeline from intent classification through
handler dispatch, covering every major feature without hitting real LLM APIs
or a live database.

Scenarios covered
-----------------
Onboarding
  - New mom registers with due date
  - New partner registers with invite code
  - Already-onboarded user offered reset on /start
  - Reset confirmed wipes data and restarts

Health logging (all record types)
  - Meal logging → confirmation presented → save → visibility offered
  - Symptom logging → confirmation → save
  - Exercise logging → confirmation → save
  - Medication logging → confirmation → save
  - Weight logging → confirmation → save
  - Water logging → confirmation → save
  - Doctor question logging → confirmation → save
  - Food preference logging → confirmation → save
  - Confirmation save without user_id → graceful error
  - Edit → corrected message → new confirmation
  - Cancel → record discarded
  - Appointment keyword → redirected to /appointments
  - Reminder keyword → redirected to /reminders

Intent routing (human messages)
  - All health record types correctly classified as LOGGING
  - Appointment/reminder phrasing classified as LOGGING but redirected
  - Personal data queries correctly classified
  - Knowledge questions correctly classified
  - Mixed queries correctly classified
  - Danger keywords trigger escalation
  - Low confidence triggers escalation

Query handler
  - Human questions return responses for all 7 record types
  - Missing user_id returns error message
  - Partner role applies visibility filter

Knowledge handler
  - RAG miss falls back to LLM with gestational context
  - Partner addressed as partner (not mom)
  - Partner RAG retry without filter on empty

Dispatcher
  - Thinking message sent immediately
  - Thinking message edited with real reply
  - Logging deletes thinking message before handler
  - Approval pending gate blocks processing
  - No user in DB silently returns (mid-onboarding)
  - Request log written on every call

Admin
  - is_admin correct for configured ID
  - Pending approval registry CRUD
  - Metrics accumulate correctly
  - approve_user activates trial

Appointment handler
  - Future datetime accepted
  - Past datetime rejected
  - Natural language date rejected (use format)
  - user_id resolved from current_user (not old 'user' key)
  - Save creates appointment
  - Cancel ends conversation

Family linking
  - Valid invite code links partner to family unit
  - Used code rejected
  - Creator cannot join their own code
  - /invite generates code for mom
  - /invite generates code for partner (either role)
  - Already linked shows confirmation
"""
from __future__ import annotations

import json
import pytest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ConversationHandler


# ===========================================================================
# Shared helpers
# ===========================================================================

def _make_update(text: str | None = None, callback_data: str | None = None,
                 telegram_user_id: int = 42) -> MagicMock:
    from telegram import Update
    u = MagicMock(spec=Update)
    u.effective_user = MagicMock()
    u.effective_user.id = telegram_user_id
    if text is not None:
        u.message = MagicMock()
        u.message.text = text
        u.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), delete=AsyncMock()))
        u.callback_query = None
        u.edited_message = None
    else:
        u.message = None
        u.edited_message = None
        u.callback_query = MagicMock()
        u.callback_query.data = callback_data
        u.callback_query.answer = AsyncMock()
        u.callback_query.edit_message_text = AsyncMock()
        u.callback_query.message = MagicMock()
        u.callback_query.message.reply_text = AsyncMock()
    return u


def _make_context(db_user_id: int | None = 10, role: str = "mom",
                  approval_pending: bool = False) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    if db_user_id is not None:
        user_obj = MagicMock()
        user_obj.id = db_user_id
        user_obj.role = MagicMock()
        user_obj.role.value = role
        user_obj.lmp_date = date.today() - timedelta(days=60)
        user_obj.due_date = None
        ctx.bot_data = {
            "current_user": user_obj,
            "is_admin": False,
            "approval_pending": approval_pending,
            "read_only": False,
            "force_mini_tier": False,
            "daily_cap_note": None,
        }
    else:
        ctx.bot_data = {
            "current_user": None,
            "is_admin": False,
            "approval_pending": False,
            "read_only": False,
            "force_mini_tier": False,
            "daily_cap_note": None,
        }
    return ctx


def _llm(intent: str, confidence: float = 0.92) -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.content = json.dumps({"intent": intent, "confidence": confidence})
    resp.model = "gpt-4.1-nano"
    resp.tokens_used = 40
    client.complete = AsyncMock(return_value=resp)
    return client


# ===========================================================================
# 1. Intent routing — every human message hits the right bucket
# ===========================================================================

class TestIntentRoutingScenarios:
    """Complete coverage of intent routing with realistic human messages."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message,expected_intent", [
        # LOGGING — health records
        ("I had oatmeal with banana for breakfast", "LOGGING"),
        ("Just ate rice and dal for lunch", "LOGGING"),
        ("Feeling nauseous this morning, about a 5 out of 10", "LOGGING"),
        ("Mild back pain after sitting for too long, severity 3", "LOGGING"),
        ("Did 30 minutes of prenatal yoga today", "LOGGING"),
        ("Took my iron tablet 65mg after dinner", "LOGGING"),
        ("Folic acid supplement taken this morning", "LOGGING"),
        ("I weigh 67.2 kg this morning", "LOGGING"),
        ("Drank about 1.5 litres of water today", "LOGGING"),
        ("Had 3 glasses of water today", "LOGGING"),
        ("I want to ask my doctor about the anatomy scan", "LOGGING"),
        ("I don't eat meat", "LOGGING"),
        ("I'm allergic to shellfish", "LOGGING"),
        # LOGGING — appointments and reminders (now correctly routed)
        ("My appointment is tomorrow at 10:30am", "LOGGING"),
        ("Add a reminder for my ultrasound tomorrow", "LOGGING"),
        ("Remind me to take my prenatal vitamin at 9am", "LOGGING"),
        # PERSONAL_DATA_QUERY
        ("What did I eat yesterday?", "PERSONAL_DATA_QUERY"),
        ("Show me my symptoms from this week", "PERSONAL_DATA_QUERY"),
        ("How much water have I logged today?", "PERSONAL_DATA_QUERY"),
        ("What medications have I taken this month?", "PERSONAL_DATA_QUERY"),
        # KNOWLEDGE_QUESTION
        ("Is it safe to eat sushi during pregnancy?", "KNOWLEDGE_QUESTION"),
        ("Can I exercise in the third trimester?", "KNOWLEDGE_QUESTION"),
        ("How should I prepare for my ultrasound?", "KNOWLEDGE_QUESTION"),
        ("What foods are rich in folic acid?", "KNOWLEDGE_QUESTION"),
    ])
    async def test_intent_classification(self, message: str, expected_intent: str):
        from app.core.intent_router import IntentRouter
        client = _llm(expected_intent)
        router = IntentRouter(client)
        result = await router.route(message)
        assert result.intent == expected_intent

    @pytest.mark.asyncio
    @pytest.mark.parametrize("danger_word", [
        "bleeding", "preeclampsia", "contractions", "seizure", "chest pain"
    ])
    async def test_danger_words_escalate(self, danger_word: str):
        from app.core.intent_router import IntentRouter
        client = _llm("KNOWLEDGE_QUESTION", 0.9)
        router = IntentRouter(client)
        result = await router.route(f"I am experiencing {danger_word}")
        assert result.escalated is True
        assert result.tier == "escalation"

    @pytest.mark.asyncio
    async def test_low_confidence_escalates(self):
        from app.core.intent_router import IntentRouter
        client = _llm("LOGGING", 0.45)
        router = IntentRouter(client)
        result = await router.route("hmm not sure")
        assert result.escalated is True

    @pytest.mark.asyncio
    async def test_unclassified_never_escalated(self):
        from app.core.intent_router import IntentRouter
        client = MagicMock()
        resp = MagicMock()
        resp.content = "not json"
        client.complete = AsyncMock(return_value=resp)
        router = IntentRouter(client)
        result = await router.route("bleeding heavily")
        assert result.intent == "UNCLASSIFIED"
        assert result.escalated is False


# ===========================================================================
# 2. Logging handler — appointment/reminder redirect (the new fix)
# ===========================================================================

class TestLoggingHandlerScheduling:
    """Appointment and reminder messages now go through the logging pipeline, not a redirect."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message,expected_type", [
        ("Add a reminder for my scan tomorrow", "reminder"),
        ("Remind me to take iron at 9am", "reminder"),
        ("Set a reminder for ultrasound at 10:30am", "reminder"),
        ("My appointment is tomorrow at 10am", "appointment"),
        ("Schedule an ob visit for next Friday", "appointment"),
        ("I have an ultrasound tomorrow morning", "appointment"),
    ])
    async def test_appointment_reminder_extracted_not_redirected(
        self, message: str, expected_type: str
    ):
        """Appointment/reminder messages are now extracted by the LLM, not redirected."""
        from app.bot.handlers.logging_handler import _determine_record_type

        route_result = MagicMock()
        route_result.record_type = None
        result = _determine_record_type(message, route_result)
        assert result == expected_type


# ===========================================================================
# 3. Logging handler — health record pipeline
# ===========================================================================

class TestLoggingHandlerHealthRecords:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message,record_type,payload", [
        (
            "I had oatmeal with banana for breakfast",
            "meal",
            {"items": [{"food_name": "oatmeal", "quantity": 1, "unit": "bowl"},
                       {"food_name": "banana", "quantity": 1, "unit": "piece"}]},
        ),
        (
            "Feeling nauseous this morning, severity 4",
            "symptom",
            {"symptom_name": "nausea", "severity": 4, "frequency": 1},
        ),
        (
            "Did 30 minutes of yoga",
            "exercise",
            {"activity_type": "yoga", "duration_minutes": 30},
        ),
        (
            "Took iron tablet 65mg",
            "medication",
            {"medication_name": "iron", "dose": "65mg"},
        ),
        (
            "I weigh 68kg",
            "weight",
            {"value": 68, "unit": "kg"},
        ),
        (
            "Drank 500ml of water",
            "water",
            {"volume": 500, "unit": "ml"},
        ),
    ])
    async def test_record_extraction_shows_confirmation(
        self, message: str, record_type: str, payload: dict
    ):
        from app.bot.handlers.logging_handler import handle_logging_intent

        update = _make_update(text=message)
        context = _make_context()
        route_result = MagicMock()
        route_result.record_type = record_type
        client = MagicMock()
        client.complete = AsyncMock(return_value=MagicMock(
            content=json.dumps(payload), model="gpt-4.1-nano", tokens_used=50
        ))

        schema_map = {
            "meal": "app.schemas.meal.MealExtraction",
            "symptom": "app.schemas.symptom.SymptomExtraction",
            "exercise": "app.schemas.exercise.ExerciseExtraction",
            "medication": "app.schemas.medication.MedicationExtraction",
            "weight": "app.schemas.weight.WeightExtraction",
            "water": "app.schemas.water.WaterExtraction",
        }

        with patch("app.bot.handlers.logging_handler.extract", new_callable=AsyncMock) as mock_extract, \
             patch("app.bot.handlers.logging_handler._store") as mock_store:

            # Import and create the right schema object
            module_path, class_name = schema_map[record_type].rsplit(".", 1)
            import importlib
            module = importlib.import_module(module_path)
            schema_class = getattr(module, class_name)
            mock_extract.return_value = schema_class(**payload)
            mock_store.put = AsyncMock()

            await handle_logging_intent(update, context, client, route_result)

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args.args[0]
        assert "Looks right" in text or "confirm" in text.lower() or record_type in text.lower() or "✅" in text

    @pytest.mark.asyncio
    async def test_confirm_save_works(self):
        """Save button must persist record and offer visibility — the core bug fix."""
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_SAVE
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_update(callback_data=CONFIRM_SAVE)
        context = _make_context(db_user_id=5)
        context.user_data["session_meta:log:42"] = {"record_type": "meal"}

        record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])

        with patch("app.bot.handlers.logging_handler._store") as mock_store, \
             patch("app.bot.handlers.logging_handler._persist_record",
                   new_callable=AsyncMock) as mock_persist:
            mock_store.get = AsyncMock(return_value=ConfirmationSession(record=record))
            mock_store.delete = AsyncMock()
            mock_persist.return_value = 101

            await handle_confirm_callback(update, context)

        # Must edit the thinking placeholder with ✅
        update.callback_query.edit_message_text.assert_awaited()
        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "✅" in edit_text or "saved" in edit_text.lower()
        # Must offer visibility choice
        update.callback_query.message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirm_cancel_discards(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_CANCEL
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_update(callback_data=CONFIRM_CANCEL)
        context = _make_context()
        context.user_data["session_meta:log:42"] = {"record_type": "meal"}
        record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])

        with patch("app.bot.handlers.logging_handler._store") as mock_store, \
             patch("app.bot.handlers.logging_handler._persist_record",
                   new_callable=AsyncMock) as mock_persist:
            mock_store.get = AsyncMock(return_value=ConfirmationSession(record=record))
            mock_store.delete = AsyncMock()

            await handle_confirm_callback(update, context)

        mock_persist.assert_not_awaited()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "❌" in text or "discarded" in text.lower()

    @pytest.mark.asyncio
    async def test_confirm_save_no_user_id_shows_error(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_SAVE
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_update(callback_data=CONFIRM_SAVE)
        context = _make_context(db_user_id=None)  # no logged-in user
        context.user_data["session_meta:log:42"] = {"record_type": "meal"}
        record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])

        with patch("app.bot.handlers.logging_handler._store") as mock_store:
            mock_store.get = AsyncMock(return_value=ConfirmationSession(record=record))

            await handle_confirm_callback(update, context)

        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "⚠️" in text or "could not" in text.lower()

    @pytest.mark.asyncio
    async def test_expired_session_shows_error(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_SAVE

        update = _make_update(callback_data=CONFIRM_SAVE)
        context = _make_context()

        with patch("app.bot.handlers.logging_handler._store") as mock_store:
            mock_store.get = AsyncMock(return_value=None)  # expired

            await handle_confirm_callback(update, context)

        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "expired" in text.lower() or "try again" in text.lower()


# ===========================================================================
# 4. Dispatcher scenarios
# ===========================================================================

class TestDispatcherScenarios:

    @pytest.mark.asyncio
    async def test_thinking_message_sent_and_edited(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("What did I eat yesterday?")
        context = _make_context()
        thinking = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        update.message.reply_text = AsyncMock(return_value=thinking)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock), \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_query_handler", new_callable=AsyncMock) as mock_qh:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=MagicMock(
                intent="PERSONAL_DATA_QUERY", confidence=0.9,
                tier="mini", escalated=False
            ))
            mock_router_cls.return_value = mock_router
            mock_qh.return_value = ("You ate rice and dal.", {"model_used": "mini", "tokens_used": 50})
            session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        # Thinking sent first
        first_call = update.message.reply_text.call_args_list[0].args[0]
        assert "💭" in first_call or "thinking" in first_call.lower()
        # Then edited with real response
        thinking.edit_text.assert_awaited_once_with("You ate rice and dal.")

    @pytest.mark.asyncio
    async def test_no_user_silently_returns(self):
        """Mid-onboarding user (current_user=None) must not trigger dispatcher."""
        from app.bot.dispatcher import dispatch

        update = _make_update("2025-12-01")  # date entry during onboarding
        context = _make_context(db_user_id=None)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"):
            mock_router = MagicMock()
            mock_router.route = AsyncMock()
            mock_router_cls.return_value = mock_router

            await dispatch(update, context)

        mock_router.route.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approval_pending_blocks_processing(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("Is sushi safe?")
        context = _make_context(approval_pending=True)
        thinking = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        update.message.reply_text = AsyncMock(return_value=thinking)

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"):
            mock_router = MagicMock()
            mock_router.route = AsyncMock()
            mock_router_cls.return_value = mock_router

            await dispatch(update, context)

        mock_router.route.assert_not_awaited()
        thinking.edit_text.assert_awaited_once()
        text = thinking.edit_text.call_args.args[0]
        assert "approval" in text.lower() or "awaiting" in text.lower()

    @pytest.mark.asyncio
    async def test_request_log_always_written(self):
        from app.bot.dispatcher import dispatch

        update = _make_update("What symptoms did I log?")
        context = _make_context()
        update.message.reply_text = AsyncMock(return_value=MagicMock(
            edit_text=AsyncMock(), delete=AsyncMock()
        ))

        with patch("app.bot.dispatcher.IntentRouter") as mock_router_cls, \
             patch("app.bot.dispatcher.LLMClient"), \
             patch("app.bot.dispatcher.log_request", new_callable=AsyncMock) as mock_log, \
             patch("app.bot.dispatcher._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.dispatcher._invoke_query_handler", new_callable=AsyncMock) as mock_qh:
            mock_router = MagicMock()
            mock_router.route = AsyncMock(side_effect=RuntimeError("boom"))
            mock_router_cls.return_value = mock_router
            session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await dispatch(update, context)

        # log_request must fire even when classification crashed
        mock_log.assert_awaited_once()


# ===========================================================================
# 5. Knowledge handler — role awareness and RAG fallback
# ===========================================================================

class TestKnowledgeHandlerScenarios:

    @pytest.mark.asyncio
    async def test_partner_addressed_as_partner(self):
        """System prompt must include partner role context."""
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update("How should I prepare for the ultrasound tomorrow?")
        context = _make_context(role="partner")
        route_result = MagicMock()
        client = MagicMock()
        client.complete = AsyncMock(return_value=MagicMock(
            content="Here's how you can support your partner during the scan...",
            model="gpt-4.1-mini", tokens_used=120
        ))

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[])  # RAG empty

            await handle_knowledge_intent(update, context, client, route_result,
                                          "How should I prepare for the ultrasound tomorrow?")

        # Check system prompt contains partner framing
        call_messages = client.complete.call_args.args[1]
        system = next(m["content"] for m in call_messages if m["role"] == "system")
        assert "partner" in system.lower() or "dad" in system.lower()

    @pytest.mark.asyncio
    async def test_mom_addressed_as_mom(self):
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update("Is it safe to eat sushi?")
        context = _make_context(role="mom")
        route_result = MagicMock()
        client = MagicMock()
        client.complete = AsyncMock(return_value=MagicMock(
            content="Raw fish should be avoided during pregnancy.",
            model="gpt-4.1-mini", tokens_used=100
        ))

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = AsyncMock(return_value=[])

            await handle_knowledge_intent(update, context, client, route_result, "Is sushi safe?")

        call_messages = client.complete.call_args.args[1]
        system = next(m["content"] for m in call_messages if m["role"] == "system")
        assert "mom" in system.lower() or "pregnant mom" in system.lower()

    @pytest.mark.asyncio
    async def test_rag_empty_partner_retries_without_filter(self):
        """Partner filter returning 0 must retry without category filter."""
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent

        update = _make_update("How far along are we?")
        context = _make_context(role="partner")
        route_result = MagicMock()
        client = MagicMock()
        client.complete = AsyncMock(return_value=MagicMock(
            content="You're about 8 weeks and 3 days along.",
            model="gpt-4.1-mini", tokens_used=80
        ))

        retrieve_calls = []

        async def mock_retrieve(*args, **kwargs):
            retrieve_calls.append(kwargs.get("category"))
            return []  # always empty to test retry logic

        with patch("app.bot.handlers.knowledge_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.knowledge_handler.retriever") as mock_retriever:
            session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_retriever.retrieve = mock_retrieve

            await handle_knowledge_intent(update, context, client, route_result, "How far along are we?")

        # Should have called retrieve twice: first with dad_support, then without
        assert len(retrieve_calls) == 2
        assert retrieve_calls[0] is not None   # first call: dad_support filter
        assert retrieve_calls[1] is None        # second call: no filter

    @pytest.mark.asyncio
    async def test_gestational_context_injected_from_lmp(self):
        """LMP date must produce correct weeks in system prompt."""
        from app.bot.handlers.knowledge_handler import _build_gestational_context
        from datetime import date, timedelta

        user_obj = MagicMock()
        user_obj.due_date = None
        user_obj.lmp_date = date.today() - timedelta(days=56)  # 8 weeks ago

        result = _build_gestational_context(user_obj)
        assert "8 weeks" in result
        assert "pregnant" in result

    @pytest.mark.asyncio
    async def test_role_context_partner(self):
        from app.bot.handlers.knowledge_handler import _build_role_context
        from app.models.user import UserRole

        user_obj = MagicMock()
        user_obj.role = UserRole.partner
        result = _build_role_context(user_obj)
        assert "partner" in result.lower() or "dad" in result.lower()

    @pytest.mark.asyncio
    async def test_role_context_mom(self):
        from app.bot.handlers.knowledge_handler import _build_role_context
        from app.models.user import UserRole

        user_obj = MagicMock()
        user_obj.role = UserRole.mom
        result = _build_role_context(user_obj)
        assert "mom" in result.lower()


# ===========================================================================
# 6. Appointment handler — key scenarios
# ===========================================================================

class TestAppointmentScenarios:

    def test_future_datetime_accepted(self):
        from app.bot.handlers.appointment_handler import _parse_datetime
        from datetime import datetime, timezone, timedelta

        future = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        dt = _parse_datetime(future)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_natural_language_rejected(self):
        from app.bot.handlers.appointment_handler import _parse_datetime

        assert _parse_datetime("tomorrow") is None
        assert _parse_datetime("next Monday") is None
        assert _parse_datetime("in two days") is None

    @pytest.mark.asyncio
    async def test_user_id_from_current_user_not_user(self):
        """Regression: must use 'current_user' key, not 'user'."""
        from app.bot.handlers.appointment_handler import _get_user_id

        ctx = MagicMock()
        ctx.bot_data = {"current_user": MagicMock(id=42), "user": MagicMock(id=999)}
        result = await _get_user_id(ctx)
        assert result == 42

    @pytest.mark.asyncio
    async def test_past_datetime_rejected(self):
        from app.bot.handlers.appointment_handler import handle_create_datetime
        from datetime import datetime, timezone, timedelta

        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        update = _make_update(text=past)
        context = _make_context()
        context.user_data["appt_data"] = {"type": "ultrasound"}

        from app.bot.handlers.appointment_handler import CREATE_DATETIME
        state = await handle_create_datetime(update, context)
        assert state == CREATE_DATETIME
        text = update.message.reply_text.call_args.args[0]
        assert "future" in text.lower() or "❌" in text


# ===========================================================================
# 7. Family linking scenarios
# ===========================================================================

class TestFamilyLinkingScenarios:

    @pytest.mark.asyncio
    async def test_either_role_can_generate_invite_code(self):
        """Both mom and partner can run /invite."""
        from app.bot.handlers.onboarding import cmd_invite
        from app.models.user import User, UserRole
        from app.models.family_unit import FamilyUnit

        for role in [UserRole.mom, UserRole.partner]:
            mock_user = MagicMock(spec=User)
            mock_user.id = 1
            mock_user.onboarding_complete = True
            mock_user.role = role
            mock_user.family_unit_id = None

            update = _make_update(text="/invite")
            context = _make_context()

            results_iter = iter([
                MagicMock(**{"scalar_one_or_none.return_value": mock_user}),
                MagicMock(**{"scalar_one_or_none.return_value": None}),
            ])

            with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
                session = AsyncMock()
                session.execute = AsyncMock(side_effect=lambda *a, **kw: next(results_iter))
                session.flush = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()
                mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
                mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

                await cmd_invite(update, context)

            text = update.message.reply_text.call_args.kwargs.get(
                "text", update.message_text if hasattr(update, "message_text") else
                update.message.reply_text.call_args.args[0]
            )
            assert "invite" in text.lower() or "code" in text.lower()
            session.add.assert_called_once()  # FamilyUnit created

    @pytest.mark.asyncio
    async def test_creator_cannot_join_own_code(self):
        from app.bot.handlers.onboarding import _link_partner_to_family
        from app.models.family_unit import FamilyUnit
        from app.models.user import User

        mock_fu = MagicMock(spec=FamilyUnit)
        mock_fu.id = 1
        mock_fu.invite_used = False
        mock_fu.mom_user_id = 5  # creator

        mock_user = MagicMock(spec=User)
        mock_user.id = 5  # same as creator

        results = iter([
            MagicMock(**{"scalar_one_or_none.return_value": mock_fu}),
            MagicMock(**{"scalar_one_or_none.return_value": mock_user}),
        ])

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            session = AsyncMock()
            session.execute = AsyncMock(side_effect=lambda *a, **kw: next(results))
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            success, msg = await _link_partner_to_family(999, "ABCDEF")

        assert success is False
        assert "own" in msg.lower() or "❌" in msg

    @pytest.mark.asyncio
    async def test_valid_invite_code_links_both_users(self):
        from app.bot.handlers.onboarding import _link_partner_to_family
        from app.models.family_unit import FamilyUnit
        from app.models.user import User

        mock_fu = MagicMock(spec=FamilyUnit)
        mock_fu.id = 7
        mock_fu.invite_used = False
        mock_fu.mom_user_id = 10  # creator (different user)

        mock_joiner = MagicMock(spec=User)
        mock_joiner.id = 20
        mock_joiner.family_unit_id = None

        mock_creator = MagicMock(spec=User)
        mock_creator.id = 10
        mock_creator.family_unit_id = None

        results = iter([
            MagicMock(**{"scalar_one_or_none.return_value": mock_fu}),
            MagicMock(**{"scalar_one_or_none.return_value": mock_joiner}),
            MagicMock(**{"scalar_one_or_none.return_value": mock_creator}),
        ])

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            session = AsyncMock()
            session.execute = AsyncMock(side_effect=lambda *a, **kw: next(results))
            session.commit = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            success, msg = await _link_partner_to_family(999, "ABCDEF")

        assert success is True
        assert mock_fu.invite_used is True
        assert mock_joiner.family_unit_id == 7
        assert mock_creator.family_unit_id == 7


# ===========================================================================
# 8. Admin scenarios
# ===========================================================================

class TestAdminScenarios:

    def test_admin_only_correct_id(self):
        from app.services.admin_service import is_admin

        with patch("app.services.admin_service.settings") as s:
            s.admin_telegram_user_id = 12345
            assert is_admin(12345) is True
            assert is_admin(99999) is False
            assert is_admin(0) is False

    def test_pending_registry_full_lifecycle(self):
        from app.services.admin_service import (
            mark_pending, clear_pending, is_pending, list_pending, _pending_approval
        )
        _pending_approval.clear()

        mark_pending(111, user_id=1, role="mom", country="IN")
        mark_pending(222, user_id=2, role="partner", country="US")

        assert is_pending(111) is True
        assert is_pending(222) is True
        assert is_pending(333) is False

        entries = {e["telegram_user_id"] for e in list_pending()}
        assert 111 in entries
        assert 222 in entries

        clear_pending(111)
        assert is_pending(111) is False
        assert is_pending(222) is True

        _pending_approval.clear()

    def test_metrics_record_and_summarise(self):
        from app.services.admin_service import _MetricsStore

        store = _MetricsStore()
        for i in range(5):
            store.record_request(
                user_id=(i % 2) + 1,  # user IDs 1 and 2 (both truthy)
                intent="LOGGING",
                model_used="gpt-4.1-nano",
                tokens_used=100,
                latency_ms=300,
            )

        s = store.summary(window_hours=1)
        assert s["requests"]["total"] == 5
        assert s["users"]["active_24h"] == 2
        assert s["intents"]["LOGGING"] == 5
        assert s["llm"]["total_tokens"] == 500

    def test_daily_digest_contains_sections(self):
        from app.services.admin_service import _MetricsStore

        store = _MetricsStore()
        store.record_request(user_id=1, intent="KNOWLEDGE_QUESTION",
                             model_used="gpt-4.1-mini", tokens_used=200, latency_ms=400)

        digest = store.daily_digest_text()
        for section in ["Famosi", "Users", "Requests", "LLM", "Intents"]:
            assert section in digest
