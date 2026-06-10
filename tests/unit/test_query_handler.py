"""
Unit tests for app/bot/handlers/query_handler.py

Tests PERSONAL_DATA_QUERY with realistic human questions.
Verifies user lookup uses "current_user" key, visibility enforcement,
date-range extraction, graceful fallbacks.

Requirements: 14.3, 6.3
"""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers.query_handler import (
    _extract_query_params,
    _serialize_records,
    _default_date_range,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(user_id: int = 55) -> MagicMock:
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context(db_user_id: int | None = 10, role: str = "mom") -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    if db_user_id is not None:
        user_obj = MagicMock()
        user_obj.id = db_user_id
        user_obj.role = MagicMock()
        user_obj.role.value = role
        ctx.bot_data = {"current_user": user_obj}
    else:
        ctx.bot_data = {"current_user": None}
    return ctx


def _make_llm_client(response_json: dict) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = json.dumps(response_json)
    response.model = "gpt-4.1-nano"
    response.tokens_used = 40
    client.complete = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# _extract_query_params
# ---------------------------------------------------------------------------

class TestExtractQueryParams:

    @pytest.mark.asyncio
    async def test_extracts_meal_type(self):
        client = _make_llm_client({
            "record_type": "meal",
            "date_range": {"start": "2025-06-01", "end": "2025-06-07"}
        })
        rt, start, end, model = await _extract_query_params("What did I eat this week?", client)
        assert rt == "meal"
        assert start is not None
        assert end is not None

    @pytest.mark.asyncio
    async def test_extracts_symptom_type(self):
        client = _make_llm_client({
            "record_type": "symptom",
            "date_range": {"start": "2025-06-09", "end": "2025-06-09"}
        })
        rt, _, _, _ = await _extract_query_params("Show me my symptoms from yesterday", client)
        assert rt == "symptom"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record_type", [
        "meal", "symptom", "exercise", "medication",
        "weight_log", "water_log", "doctor_question", "preference",
    ])
    async def test_all_valid_record_types_accepted(self, record_type: str):
        client = _make_llm_client({"record_type": record_type, "date_range": {}})
        rt, _, _, _ = await _extract_query_params("show my data", client)
        assert rt == record_type

    @pytest.mark.asyncio
    async def test_invalid_record_type_returns_none(self):
        client = _make_llm_client({"record_type": "totally_unknown", "date_range": {}})
        rt, _, _, _ = await _extract_query_params("show me stuff", client)
        assert rt is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self):
        client = MagicMock()
        response = MagicMock()
        response.content = "not json"
        response.model = "gpt-4.1-nano"
        client.complete = AsyncMock(return_value=response)
        rt, _, _, _ = await _extract_query_params("show me data", client)
        assert rt is None

    @pytest.mark.asyncio
    async def test_no_date_range_returns_none_dates(self):
        client = _make_llm_client({"record_type": "meal", "date_range": {}})
        rt, start, end, _ = await _extract_query_params("show my meals", client)
        assert rt == "meal"
        assert start is None
        assert end is None


# ---------------------------------------------------------------------------
# _default_date_range
# ---------------------------------------------------------------------------

class TestDefaultDateRange:

    def test_returns_7_day_window(self):
        start, end = _default_date_range()
        delta = end - start
        assert 6 <= delta.days <= 8  # ~7 days

    def test_end_is_after_start(self):
        start, end = _default_date_range()
        assert end > start

    def test_both_timezone_aware(self):
        start, end = _default_date_range()
        assert start.tzinfo is not None
        assert end.tzinfo is not None


# ---------------------------------------------------------------------------
# _serialize_records
# ---------------------------------------------------------------------------

class TestSerializeRecords:

    def test_empty_records_returns_no_records_found(self):
        result = _serialize_records("meal", [])
        assert "no records found" in result.lower()

    def test_symptom_includes_name_and_severity(self):
        rec = MagicMock()
        rec.logged_at = datetime(2025, 6, 9, 8, 0, tzinfo=timezone.utc)
        rec.symptom_name = "nausea"
        rec.severity = 4
        rec.frequency = 2
        result = _serialize_records("symptom", [rec])
        assert "nausea" in result
        assert "4" in result

    def test_exercise_includes_type_and_duration(self):
        rec = MagicMock()
        rec.logged_at = datetime(2025, 6, 9, tzinfo=timezone.utc)
        rec.activity_type = "yoga"
        rec.duration_minutes = 30
        result = _serialize_records("exercise", [rec])
        assert "yoga" in result
        assert "30" in result

    def test_medication_includes_name_and_dose(self):
        rec = MagicMock()
        rec.logged_at = datetime(2025, 6, 9, tzinfo=timezone.utc)
        rec.medication_name = "iron"
        rec.dose = "65mg"
        result = _serialize_records("medication", [rec])
        assert "iron" in result
        assert "65mg" in result

    def test_weight_log_includes_value_and_unit(self):
        rec = MagicMock()
        rec.logged_at = datetime(2025, 6, 9, tzinfo=timezone.utc)
        rec.value = 68.5
        rec.unit = "kg"
        result = _serialize_records("weight_log", [rec])
        assert "68.5" in result
        assert "kg" in result

    def test_water_log_includes_volume_and_unit(self):
        rec = MagicMock()
        rec.logged_at = datetime(2025, 6, 9, tzinfo=timezone.utc)
        rec.volume = 500
        rec.unit = "ml"
        result = _serialize_records("water_log", [rec])
        assert "500" in result
        assert "ml" in result

    def test_multiple_records_all_included(self):
        recs = []
        for i in range(3):
            rec = MagicMock()
            rec.logged_at = datetime(2025, 6, i + 1, tzinfo=timezone.utc)
            rec.symptom_name = f"headache_{i}"
            rec.severity = i + 1
            rec.frequency = 1
            recs.append(rec)
        result = _serialize_records("symptom", recs)
        assert "headache_0" in result
        assert "headache_1" in result
        assert "headache_2" in result


# ---------------------------------------------------------------------------
# handle_query_intent: full pipeline with human messages
# ---------------------------------------------------------------------------

class TestHandleQueryIntent:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message, record_type", [
        ("What did I eat yesterday?", "meal"),
        ("Show me my symptoms from this week", "symptom"),
        ("How much water have I logged today?", "water_log"),
        ("What medications did I take this month?", "medication"),
        ("What was my weight last week?", "weight_log"),
        ("Did I exercise this week?", "exercise"),
        ("What questions did I save for my doctor?", "doctor_question"),
    ])
    async def test_human_queries_return_response(self, message: str, record_type: str):
        from app.bot.handlers.query_handler import handle_query_intent

        update = _make_update()
        context = _make_context(db_user_id=10)
        route_result = MagicMock()

        # LLM returns the record type
        nano_client = _make_llm_client({"record_type": record_type, "date_range": {}})
        # Mini formats the response
        mini_response = MagicMock()
        mini_response.content = f"Here are your {record_type} records from this week."
        mini_response.model = "gpt-4.1-mini"
        mini_response.tokens_used = 80
        nano_client.complete = AsyncMock(side_effect=[
            MagicMock(content=json.dumps({"record_type": record_type, "date_range": {}}),
                      model="gpt-4.1-nano", tokens_used=40),
            mini_response
        ])

        with patch("app.bot.handlers.query_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.query_handler.personal_memory") as mock_pm:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_pm.get_records = AsyncMock(return_value=[])  # empty is fine, tests full pipeline

            text, meta = await handle_query_intent(
                update, context, nano_client, route_result, message
            )

        assert text is not None
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_missing_user_returns_error_message(self):
        from app.bot.handlers.query_handler import handle_query_intent

        update = _make_update()
        context = _make_context(db_user_id=None)  # no user in bot_data
        route_result = MagicMock()
        client = MagicMock()

        text, meta = await handle_query_intent(
            update, context, client, route_result, "What did I eat?"
        )

        assert text is not None
        assert "account" in text.lower() or "authenticate" in text.lower()

    @pytest.mark.asyncio
    async def test_partner_visibility_applied(self):
        """Partner role should have visibility filtering applied in the DB query."""
        from app.bot.handlers.query_handler import handle_query_intent

        update = _make_update()
        context = _make_context(db_user_id=20, role="partner")
        route_result = MagicMock()

        client = _make_llm_client({"record_type": "meal", "date_range": {}})
        mini_resp = MagicMock()
        mini_resp.content = "No shared meals found."
        mini_resp.model = "gpt-4.1-mini"
        mini_resp.tokens_used = 30
        client.complete = AsyncMock(side_effect=[
            MagicMock(content=json.dumps({"record_type": "meal", "date_range": {}}),
                      model="gpt-4.1-nano", tokens_used=20),
            mini_resp,
        ])

        with patch("app.bot.handlers.query_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.query_handler.personal_memory") as mock_pm:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_pm.get_records = AsyncMock(return_value=[])

            await handle_query_intent(update, context, client, route_result, "Show shared meals")

        # Verify requesting_role was "partner"
        call_kwargs = mock_pm.get_records.call_args.kwargs
        assert call_kwargs.get("requesting_role") == "partner"
