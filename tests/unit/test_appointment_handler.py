"""
Unit tests for app/bot/handlers/appointment_handler.py

Tests the appointment CRUD ConversationHandler with realistic user interactions:
  - "I have my first scan scheduled tomorrow" → create flow
  - List upcoming appointments
  - Cancel an appointment
  - Reschedule an appointment
  - Date/time parsing and validation
  - user_id resolved from "current_user" (not the buggy "user" key)

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers.appointment_handler import (
    _parse_datetime,
    _format_appointment_list,
    _get_user_id,
    CREATE_DATETIME,
    CREATE_LOCATION,
    CREATE_NOTES,
    CREATE_CONFIRM,
    RESCHEDULE_DATETIME,
)
from telegram.ext import ConversationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str | None = None, callback_data: str | None = None,
                 telegram_user_id: int = 55) -> MagicMock:
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id

    if text is not None:
        update.message = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock()
        update.callback_query = None
    else:
        update.message = None
        update.callback_query = MagicMock()
        update.callback_query.data = callback_data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

    return update


def _make_context(db_user_id: int | None = 10) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    if db_user_id is not None:
        user_obj = MagicMock()
        user_obj.id = db_user_id
        ctx.bot_data = {"current_user": user_obj}
    else:
        ctx.bot_data = {"current_user": None}
    return ctx


# ---------------------------------------------------------------------------
# _parse_datetime
# ---------------------------------------------------------------------------

class TestParseDatetime:

    def test_iso_format_with_space(self):
        dt = _parse_datetime("2025-09-15 14:30")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 9
        assert dt.day == 15
        assert dt.hour == 14
        assert dt.minute == 30

    def test_iso_format_with_T(self):
        dt = _parse_datetime("2025-10-20T09:00")
        assert dt is not None
        assert dt.hour == 9

    def test_european_date_format(self):
        dt = _parse_datetime("20/11/2025 10:30")
        assert dt is not None
        assert dt.month == 11

    def test_returns_utc_aware(self):
        dt = _parse_datetime("2025-09-15 14:30")
        assert dt.tzinfo is not None

    def test_invalid_format_returns_none(self):
        assert _parse_datetime("tomorrow at 3pm") is None
        assert _parse_datetime("next Monday") is None
        assert _parse_datetime("") is None

    def test_natural_language_tomorrow_returns_none(self):
        # This is an important user-facing scenario — natural language not supported
        assert _parse_datetime("tomorrow") is None


# ---------------------------------------------------------------------------
# _get_user_id: uses "current_user" key
# ---------------------------------------------------------------------------

class TestGetUserId:

    @pytest.mark.asyncio
    async def test_returns_id_from_current_user(self):
        """Critical regression test: must use 'current_user', not 'user'."""
        context = _make_context(db_user_id=42)
        result = await _get_user_id(context)
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_none_when_no_user(self):
        context = _make_context(db_user_id=None)
        result = await _get_user_id(context)
        assert result is None

    @pytest.mark.asyncio
    async def test_old_user_key_not_used(self):
        """Ensure 'user' key (old buggy key) is not picked up."""
        ctx = MagicMock()
        ctx.bot_data = {"user": MagicMock(id=999)}  # OLD incorrect key
        # current_user is not set → should return None
        ctx.bot_data["current_user"] = None
        result = await _get_user_id(ctx)
        assert result is None


# ---------------------------------------------------------------------------
# _format_appointment_list
# ---------------------------------------------------------------------------

class TestFormatAppointmentList:

    def test_empty_list_returns_no_appointments_message(self):
        result = _format_appointment_list([])
        assert "no upcoming" in result.lower() or "🗓️" in result

    def test_single_appointment_includes_type_and_date(self):
        from app.models.appointment import AppointmentType
        appt = MagicMock()
        appt.appointment_type = AppointmentType.ultrasound
        appt.appointment_at = datetime(2025, 9, 15, 10, 30, tzinfo=timezone.utc)
        appt.location = "City Hospital"
        appt.notes = None

        result = _format_appointment_list([appt])
        assert "ultrasound" in result.lower() or "Ultrasound" in result
        assert "September" in result or "2025" in result

    def test_multiple_appointments_all_listed(self):
        from app.models.appointment import AppointmentType
        appointments = []
        for i, apt in enumerate([AppointmentType.ob_visit, AppointmentType.bloodwork]):
            appt = MagicMock()
            appt.appointment_type = apt
            appt.appointment_at = datetime(2025, 9, i + 10, 10, tzinfo=timezone.utc)
            appt.location = None
            appt.notes = None
            appointments.append(appt)

        result = _format_appointment_list(appointments)
        assert "OB" in result or "ob" in result.lower()
        assert "bloodwork" in result.lower() or "Bloodwork" in result


# ---------------------------------------------------------------------------
# Create flow: date/time entry
# ---------------------------------------------------------------------------

class TestCreateDatetime:

    @pytest.mark.asyncio
    async def test_valid_future_datetime_advances(self):
        from app.bot.handlers.appointment_handler import handle_create_datetime

        future = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        update = _make_update(text=future)
        context = _make_context()
        context.user_data["appt_data"] = {"type": "ultrasound"}

        state = await handle_create_datetime(update, context)
        assert state == CREATE_LOCATION

    @pytest.mark.asyncio
    async def test_invalid_format_reprompts(self):
        from app.bot.handlers.appointment_handler import handle_create_datetime

        update = _make_update(text="I have my first scan scheduled tomorrow")
        context = _make_context()
        context.user_data["appt_data"] = {"type": "ultrasound"}

        state = await handle_create_datetime(update, context)
        assert state == CREATE_DATETIME
        text = update.message.reply_text.call_args.args[0]
        assert "❌" in text or "couldn't" in text.lower()

    @pytest.mark.asyncio
    async def test_past_datetime_reprompts(self):
        from app.bot.handlers.appointment_handler import handle_create_datetime

        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        update = _make_update(text=past)
        context = _make_context()
        context.user_data["appt_data"] = {"type": "ob_visit"}

        state = await handle_create_datetime(update, context)
        assert state == CREATE_DATETIME
        text = update.message.reply_text.call_args.args[0]
        assert "future" in text.lower() or "❌" in text


# ---------------------------------------------------------------------------
# Create confirm: save persists the appointment
# ---------------------------------------------------------------------------

class TestCreateConfirm:

    @pytest.mark.asyncio
    async def test_save_creates_appointment_in_db(self):
        from app.bot.handlers.appointment_handler import handle_create_confirm
        from app.bot.keyboards.confirm import CONFIRM_SAVE

        future_dt = datetime.now(timezone.utc) + timedelta(days=7)

        update = _make_update(callback_data=CONFIRM_SAVE)
        context = _make_context(db_user_id=5)
        context.user_data["appt_data"] = {
            "type": "ultrasound",
            "datetime": future_dt.isoformat(),
            "datetime_display": "2025-09-15 10:30 UTC",
            "location": "City Hospital",
            "notes": "Bring blood test results",
        }

        mock_appt = MagicMock()
        mock_appt.id = 1

        with patch("app.bot.handlers.appointment_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.appointment_handler.appointment_tracker") as mock_tracker:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session.commit = AsyncMock()
            mock_tracker.create_appointment = AsyncMock(return_value=mock_appt)

            state = await handle_create_confirm(update, context)

        assert state == ConversationHandler.END
        mock_tracker.create_appointment.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "✅" in text or "saved" in text.lower()

    @pytest.mark.asyncio
    async def test_cancel_ends_conversation(self):
        from app.bot.handlers.appointment_handler import handle_create_confirm
        from app.bot.keyboards.confirm import CONFIRM_CANCEL

        update = _make_update(callback_data=CONFIRM_CANCEL)
        context = _make_context()
        context.user_data["appt_data"] = {}

        state = await handle_create_confirm(update, context)
        assert state == ConversationHandler.END
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "cancelled" in text.lower() or "❌" in text

    @pytest.mark.asyncio
    async def test_save_without_user_id_shows_error(self):
        from app.bot.handlers.appointment_handler import handle_create_confirm
        from app.bot.keyboards.confirm import CONFIRM_SAVE

        future_dt = datetime.now(timezone.utc) + timedelta(days=7)
        update = _make_update(callback_data=CONFIRM_SAVE)
        context = _make_context(db_user_id=None)  # no user
        context.user_data["appt_data"] = {
            "type": "ob_visit",
            "datetime": future_dt.isoformat(),
        }

        state = await handle_create_confirm(update, context)
        assert state == ConversationHandler.END
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "⚠️" in text or "session" in text.lower()


# ---------------------------------------------------------------------------
# List flow
# ---------------------------------------------------------------------------

class TestListAppointments:

    @pytest.mark.asyncio
    async def test_list_shows_upcoming_appointments(self):
        from app.bot.handlers.appointment_handler import handle_main_menu

        update = _make_update(callback_data="appt:action:list")
        context = _make_context(db_user_id=10)
        context.user_data["appt_data"] = {}

        from app.models.appointment import AppointmentType
        mock_appt = MagicMock()
        mock_appt.appointment_type = AppointmentType.ultrasound
        mock_appt.appointment_at = datetime(2025, 9, 15, 10, 30, tzinfo=timezone.utc)
        mock_appt.location = "City Hospital"
        mock_appt.notes = None
        mock_appt.cancelled = False

        with patch("app.bot.handlers.appointment_handler._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.appointment_handler.appointment_tracker") as mock_tracker:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tracker.list_upcoming = AsyncMock(return_value=[mock_appt])

            state = await handle_main_menu(update, context)

        assert state == ConversationHandler.END
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "ultrasound" in text.lower() or "Ultrasound" in text
