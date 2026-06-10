"""
Unit tests for app/bot/handlers/logging_handler.py

Tests the LOGGING pipeline with realistic human messages for every record type:
  - Meals: "I had oatmeal with banana for breakfast"
  - Symptoms: "Feeling nauseous this morning, about a 5 out of 10"
  - Exercise: "Did 30 minutes of prenatal yoga today"
  - Medication: "Took my iron tablet 65mg after dinner"
  - Weight: "I weigh 67.2 kg this morning"
  - Water: "Drank about 1.5 litres of water today"
  - Doctor questions: "I want to ask my doctor about the anatomy scan"
  - Preferences: "I don't eat meat, I'm vegetarian"
  - Confirmation: save, edit, cancel flows
  - Visibility callbacks: private, partner_shared, doctor_shared
  - Record type determination heuristic

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 6.2
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers.logging_handler import _determine_record_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str, telegram_user_id: int = 42) -> MagicMock:
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(data: str, telegram_user_id: int = 42) -> MagicMock:
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def _make_context(user_id: int = 99) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {"current_user": MagicMock(id=user_id)}
    return ctx


def _make_route_result() -> MagicMock:
    rr = MagicMock()
    rr.record_type = None
    return rr


def _make_llm_client_with_extraction(record_type: str, payload: dict) -> MagicMock:
    """Return a mock LLMClient that returns valid extraction JSON for the given type."""
    import json
    client = MagicMock()
    response = MagicMock()
    response.content = json.dumps(payload)
    response.model = "gpt-4.1-nano"
    response.tokens_used = 60
    client.complete = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# Record type determination
# ---------------------------------------------------------------------------

class TestDetermineRecordType:

    @pytest.mark.parametrize("message, expected", [
        ("I had oatmeal with banana for breakfast", "meal"),
        ("Just ate rice and dal", "meal"),
        ("Feeling nauseous this morning", "symptom"),
        ("Had a mild headache after lunch", "symptom"),
        ("Did 30 minutes of prenatal yoga", "exercise"),
        ("Went for a 20-minute walk in the park", "exercise"),
        ("Took my iron tablet 65mg", "medication"),
        ("Folic acid supplement taken this morning", "medication"),
        ("I weigh 67.2 kg", "weight"),
        ("My weight is 68 pounds today", "weight"),
        ("Drank 2 litres of water today", "water"),
        ("Had 3 glasses of water", "water"),
        ("I want to ask my doctor about the scan", "question"),
        ("Question for appointment: when does nausea stop?", "question"),
        ("I don't eat meat", "preference"),
        ("I'm allergic to shellfish", "preference"),
    ])
    def test_keyword_heuristic(self, message: str, expected: str):
        route_result = MagicMock()
        route_result.record_type = None
        assert _determine_record_type(message, route_result) == expected

    def test_router_hint_takes_priority(self):
        route_result = MagicMock()
        route_result.record_type = "symptom"
        # Even though message says "ate", the hint wins
        assert _determine_record_type("I ate something", route_result) == "symptom"

    def test_no_keyword_match_defaults_to_meal(self):
        route_result = MagicMock()
        route_result.record_type = None
        assert _determine_record_type("something random that doesn't match", route_result) == "meal"


# ---------------------------------------------------------------------------
# handle_logging_intent: extraction success → confirmation presented
# ---------------------------------------------------------------------------

class TestHandleLoggingIntent:

    @pytest.mark.asyncio
    async def test_meal_extraction_shows_confirmation(self):
        from app.bot.handlers.logging_handler import handle_logging_intent

        update = _make_update("I had oatmeal with banana for breakfast")
        context = _make_context()
        route_result = _make_route_result()

        meal_payload = {"items": [
            {"food_name": "oatmeal", "quantity": 1, "unit": "bowl"},
            {"food_name": "banana", "quantity": 1, "unit": "piece"},
        ]}
        client = _make_llm_client_with_extraction("meal", meal_payload)

        with patch("app.bot.handlers.logging_handler.extract", new_callable=AsyncMock) as mock_extract, \
             patch("app.bot.handlers.logging_handler._store") as mock_store:
            from app.schemas.meal import MealExtraction
            mock_extract.return_value = MealExtraction(**meal_payload)
            mock_store.put = AsyncMock()

            result = await handle_logging_intent(update, context, client, route_result)

        update.message.reply_text.assert_awaited_once()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "oatmeal" in reply_text.lower() or "meal" in reply_text.lower()
        assert "Looks right" in reply_text or "confirm" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_symptom_extraction_shows_confirmation(self):
        from app.bot.handlers.logging_handler import handle_logging_intent

        update = _make_update("Feeling nauseous this morning, about a 5 out of 10")
        context = _make_context()
        route_result = _make_route_result()
        route_result.record_type = "symptom"

        symptom_payload = {"symptom_name": "nausea", "severity": 5, "frequency": 2}
        client = _make_llm_client_with_extraction("symptom", symptom_payload)

        with patch("app.bot.handlers.logging_handler.extract", new_callable=AsyncMock) as mock_extract, \
             patch("app.bot.handlers.logging_handler._store") as mock_store:
            from app.schemas.symptom import SymptomExtraction
            mock_extract.return_value = SymptomExtraction(**symptom_payload)
            mock_store.put = AsyncMock()

            result = await handle_logging_intent(update, context, client, route_result)

        reply_text = update.message.reply_text.call_args.args[0]
        assert "nausea" in reply_text.lower() or "symptom" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_extraction_failure_sends_clarification(self):
        from app.bot.handlers.logging_handler import handle_logging_intent

        update = _make_update("hmm")
        context = _make_context()
        route_result = _make_route_result()
        client = MagicMock()
        client.complete = AsyncMock()

        with patch("app.bot.handlers.logging_handler.extract", new_callable=AsyncMock) as mock_extract:
            mock_extract.return_value = None  # hard failure

            result = await handle_logging_intent(update, context, client, route_result)

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args.args[0]
        assert "understand" in text.lower() or "rephrase" in text.lower()

    @pytest.mark.asyncio
    async def test_missing_fields_prompts_user(self):
        from app.bot.handlers.logging_handler import handle_logging_intent
        from app.core.extractor import MissingFields

        update = _make_update("I had some food")
        context = _make_context()
        route_result = _make_route_result()
        client = MagicMock()
        client.complete = AsyncMock()

        with patch("app.bot.handlers.logging_handler.extract", new_callable=AsyncMock) as mock_extract:
            mock_extract.return_value = MissingFields(record_type="meal", missing=["food_name", "quantity"])

            result = await handle_logging_intent(update, context, client, route_result)

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args.args[0]
        assert "food name" in text.lower() or "quantity" in text.lower() or "information" in text.lower()


# ---------------------------------------------------------------------------
# Confirm callback: save, edit, cancel
# ---------------------------------------------------------------------------

class TestHandleConfirmCallback:

    @pytest.mark.asyncio
    async def test_save_persists_record_and_shows_visibility(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_SAVE
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_callback_update(CONFIRM_SAVE)
        context = _make_context(user_id=5)
        context.user_data[f"session_meta:log:42"] = {"record_type": "meal"}

        mock_record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])
        mock_session = ConfirmationSession(record=mock_record)

        with patch("app.bot.handlers.logging_handler._store") as mock_store, \
             patch("app.bot.handlers.logging_handler._persist_record", new_callable=AsyncMock) as mock_persist:
            mock_store.get = AsyncMock(return_value=mock_session)
            mock_store.delete = AsyncMock()
            mock_persist.return_value = 101  # newly created record ID

            await handle_confirm_callback(update, context)

        update.callback_query.edit_message_text.assert_awaited()
        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "✅" in edit_text or "saved" in edit_text.lower()
        # Should offer visibility choice
        update.callback_query.message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_discards_record(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_CANCEL
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_callback_update(CONFIRM_CANCEL)
        context = _make_context()
        context.user_data["session_meta:log:42"] = {"record_type": "meal"}

        mock_record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])
        mock_session = ConfirmationSession(record=mock_record)

        with patch("app.bot.handlers.logging_handler._store") as mock_store, \
             patch("app.bot.handlers.logging_handler._persist_record", new_callable=AsyncMock) as mock_persist:
            mock_store.get = AsyncMock(return_value=mock_session)
            mock_store.delete = AsyncMock()

            await handle_confirm_callback(update, context)

        mock_persist.assert_not_awaited()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "❌" in text or "discarded" in text.lower()

    @pytest.mark.asyncio
    async def test_edit_prompts_for_correction(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_EDIT
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_callback_update(CONFIRM_EDIT)
        context = _make_context()
        context.user_data["session_meta:log:42"] = {"record_type": "meal"}

        mock_record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])
        mock_session = ConfirmationSession(record=mock_record)

        with patch("app.bot.handlers.logging_handler._store") as mock_store:
            mock_store.get = AsyncMock(return_value=mock_session)
            mock_store.put = AsyncMock()

            await handle_confirm_callback(update, context)

        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "corrected" in text.lower() or "send" in text.lower()

    @pytest.mark.asyncio
    async def test_expired_session_sends_error(self):
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_SAVE

        update = _make_callback_update(CONFIRM_SAVE)
        context = _make_context()

        with patch("app.bot.handlers.logging_handler._store") as mock_store:
            mock_store.get = AsyncMock(return_value=None)  # session expired

            await handle_confirm_callback(update, context)

        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "expired" in text.lower() or "try again" in text.lower()

    @pytest.mark.asyncio
    async def test_save_without_user_id_shows_error(self):
        """Save should gracefully handle missing user_id from bot_data."""
        from app.bot.handlers.logging_handler import handle_confirm_callback
        from app.bot.keyboards.confirm import CONFIRM_SAVE
        from app.core.confirmation import ConfirmationSession
        from app.schemas.meal import MealExtraction

        update = _make_callback_update(CONFIRM_SAVE)
        context = _make_context()
        context.bot_data = {"current_user": None}  # no user
        context.user_data["session_meta:log:42"] = {"record_type": "meal"}

        mock_record = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "bowl"}])

        with patch("app.bot.handlers.logging_handler._store") as mock_store:
            mock_store.get = AsyncMock(return_value=ConfirmationSession(record=mock_record))

            await handle_confirm_callback(update, context)

        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "could not" in text.lower() or "⚠️" in text


# ---------------------------------------------------------------------------
# Visibility callbacks
# ---------------------------------------------------------------------------

class TestHandleVisibilityCallback:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cb_data, expected_enum", [
        ("visibility:private", "private"),
        ("visibility:partner_shared", "partner_shared"),
        ("visibility:doctor_shared", "doctor_shared"),
    ])
    async def test_valid_visibility_updates_record(self, cb_data: str, expected_enum: str):
        from app.bot.handlers.logging_handler import handle_visibility_callback

        update = _make_callback_update(cb_data)
        context = _make_context(user_id=7)
        context.user_data["pending_visibility"] = {
            "record_id": 1, "record_type": "meal", "user_id": 7
        }

        with patch("app.dependencies._AsyncSessionFactory") as mock_factory, \
             patch("app.memory.personal_memory.update_visibility", new_callable=AsyncMock) as mock_pm_update:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await handle_visibility_callback(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "✅" in text or "updated" in text.lower()
