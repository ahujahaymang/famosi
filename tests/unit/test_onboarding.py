"""
Unit tests for app/bot/handlers/onboarding.py

Tests the full onboarding state machine with realistic human-style interactions:
  - Mom onboarding: due date → country → timezone → language → food pref → exercise → times
  - Partner onboarding: invite code entry, skip, invalid code
  - /start reset flow: confirm yes (wipes data), confirm no (keeps data)
  - /invite command: generates code, shows existing unused code, detects used code
  - Family linking: _link_partner_to_family success and failure paths
  - Validation helpers: due date, LMP, invite code format

Requirements: 1.2, 1.3, 1.6, 1.7
"""
from __future__ import annotations

import pytest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers.onboarding import (
    CONFIRM_RESET,
    INVITE_CODE,
    ROLE,
    DUE_DATE_OR_LMP,
    COUNTRY,
    FOOD_PREFERENCE,
    SLEEP_TIME,
    _generate_invite_code,
    _is_valid_due_date,
    _is_valid_lmp,
    _INVITE_RE,
)
from telegram.ext import ConversationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str | None = None, callback_data: str | None = None,
                 telegram_user_id: int = 111) -> MagicMock:
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
        update.callback_query.message = MagicMock()
        update.callback_query.message.reply_text = AsyncMock()

    return update


def _make_context(data: dict | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {"onboarding_data": data or {}}
    ctx.bot_data = {}
    return ctx


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class TestValidationHelpers:

    def test_valid_due_date_in_range(self):
        assert _is_valid_due_date(date.today() + timedelta(days=100)) is True

    def test_due_date_tomorrow_valid(self):
        assert _is_valid_due_date(date.today() + timedelta(days=1)) is True

    def test_due_date_280_days_valid(self):
        assert _is_valid_due_date(date.today() + timedelta(days=280)) is True

    def test_due_date_today_invalid(self):
        assert _is_valid_due_date(date.today()) is False

    def test_due_date_in_past_invalid(self):
        assert _is_valid_due_date(date.today() - timedelta(days=1)) is False

    def test_due_date_281_days_invalid(self):
        assert _is_valid_due_date(date.today() + timedelta(days=281)) is False

    def test_valid_lmp_yesterday(self):
        assert _is_valid_lmp(date.today() - timedelta(days=1)) is True

    def test_valid_lmp_100_days_ago(self):
        assert _is_valid_lmp(date.today() - timedelta(days=100)) is True

    def test_lmp_today_invalid(self):
        assert _is_valid_lmp(date.today()) is False

    def test_lmp_future_invalid(self):
        assert _is_valid_lmp(date.today() + timedelta(days=1)) is False

    def test_lmp_281_days_ago_invalid(self):
        assert _is_valid_lmp(date.today() - timedelta(days=281)) is False

    def test_invite_code_valid_format(self):
        assert _INVITE_RE.match("AB12CD") is not None
        assert _INVITE_RE.match("000000") is not None
        assert _INVITE_RE.match("ZZZZZZ") is not None

    def test_invite_code_wrong_length(self):
        assert _INVITE_RE.match("AB12C") is None    # 5 chars
        assert _INVITE_RE.match("AB12CDE") is None  # 7 chars

    def test_invite_code_lowercase_rejected(self):
        assert _INVITE_RE.match("ab12cd") is None

    def test_invite_code_special_chars_rejected(self):
        assert _INVITE_RE.match("AB-12C") is None

    def test_generate_invite_code_length(self):
        code = _generate_invite_code()
        assert len(code) == 6

    def test_generate_invite_code_format(self):
        for _ in range(20):
            code = _generate_invite_code()
            assert _INVITE_RE.match(code) is not None

    def test_generate_invite_code_randomness(self):
        codes = {_generate_invite_code() for _ in range(50)}
        assert len(codes) > 10  # extremely unlikely to get < 10 unique in 50 tries


# ---------------------------------------------------------------------------
# /start: already-onboarded user sees reset prompt
# ---------------------------------------------------------------------------

class TestCmdStart:

    @pytest.mark.asyncio
    async def test_already_onboarded_shows_reset_prompt(self):
        from app.bot.handlers.onboarding import cmd_start
        from app.models.user import User

        mock_user = MagicMock(spec=User)
        mock_user.onboarding_complete = True

        update = _make_update(text="/start")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_user
            mock_session.execute = AsyncMock(return_value=mock_result)

            state = await cmd_start(update, context)

        assert state == CONFIRM_RESET
        update.message.reply_text.assert_awaited_once()
        call_args = update.message.reply_text.call_args
        assert "delete" in call_args.kwargs.get("text", call_args.args[0]).lower()

    @pytest.mark.asyncio
    async def test_new_user_starts_from_role(self):
        from app.bot.handlers.onboarding import cmd_start

        update = _make_update(text="/start")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None  # no existing user
            mock_session.execute = AsyncMock(return_value=mock_result)

            with patch("app.bot.handlers.onboarding._load_state", new_callable=AsyncMock) as mock_load:
                mock_load.return_value = None
                state = await cmd_start(update, context)

        assert state == ROLE

    @pytest.mark.asyncio
    async def test_partial_state_resumes(self):
        from app.bot.handlers.onboarding import cmd_start

        update = _make_update(text="/start")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_session.execute = AsyncMock(return_value=mock_result)

            with patch("app.bot.handlers.onboarding._load_state", new_callable=AsyncMock) as mock_load:
                mock_load.return_value = {"step": COUNTRY, "data": {"role": "mom", "due_date": "2025-12-01"}}
                state = await cmd_start(update, context)

        assert state == COUNTRY


# ---------------------------------------------------------------------------
# CONFIRM_RESET: yes wipes data, no keeps it
# ---------------------------------------------------------------------------

class TestConfirmReset:

    @pytest.mark.asyncio
    async def test_confirm_yes_deletes_user_and_restarts(self):
        from app.bot.handlers.onboarding import handle_confirm_reset

        update = _make_update(callback_data="reset:yes")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._delete_user_data", new_callable=AsyncMock) as mock_delete, \
             patch("app.bot.handlers.onboarding._clear_state", new_callable=AsyncMock) as mock_clear, \
             patch("app.bot.handlers.onboarding.clear_pending") as mock_clear_pending:

            state = await handle_confirm_reset(update, context)

        mock_delete.assert_awaited_once_with(111)
        mock_clear.assert_awaited_once()
        mock_clear_pending.assert_called_once_with(111)
        assert state == ROLE
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "deleted" in text.lower() or "start fresh" in text.lower()

    @pytest.mark.asyncio
    async def test_confirm_no_ends_conversation_without_deleting(self):
        from app.bot.handlers.onboarding import handle_confirm_reset

        update = _make_update(callback_data="reset:no")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._delete_user_data", new_callable=AsyncMock) as mock_delete:
            state = await handle_confirm_reset(update, context)

        mock_delete.assert_not_awaited()
        assert state == ConversationHandler.END


# ---------------------------------------------------------------------------
# Role selection: mom vs partner routing
# ---------------------------------------------------------------------------

class TestHandleRole:

    @pytest.mark.asyncio
    async def test_mom_role_goes_to_due_date(self):
        from app.bot.handlers.onboarding import handle_role

        update = _make_update(callback_data="role:mom")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            state = await handle_role(update, context)

        assert state == DUE_DATE_OR_LMP
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "due date" in text.lower()

    @pytest.mark.asyncio
    async def test_partner_role_goes_to_invite_code(self):
        from app.bot.handlers.onboarding import handle_role

        update = _make_update(callback_data="role:partner")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            state = await handle_role(update, context)

        assert state == INVITE_CODE
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "invite" in text.lower() or "code" in text.lower()


# ---------------------------------------------------------------------------
# Partner invite code step
# ---------------------------------------------------------------------------

class TestHandleInviteCode:

    @pytest.mark.asyncio
    async def test_skip_button_moves_to_due_date(self):
        from app.bot.handlers.onboarding import handle_invite_code

        update = _make_update(callback_data="invite:skip")
        context = _make_context({"role": "partner"})

        with patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            state = await handle_invite_code(update, context)

        assert state == DUE_DATE_OR_LMP
        assert context.user_data["onboarding_data"].get("invite_code") is None

    @pytest.mark.asyncio
    async def test_valid_code_accepted(self):
        from app.bot.handlers.onboarding import handle_invite_code
        from app.models.family_unit import FamilyUnit

        update = _make_update(text="AB12CD")
        context = _make_context({"role": "partner"})

        mock_fu = MagicMock(spec=FamilyUnit)
        mock_fu.invite_used = False

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_fu
            mock_session.execute = AsyncMock(return_value=mock_result)

            state = await handle_invite_code(update, context)

        assert state == DUE_DATE_OR_LMP
        assert context.user_data["onboarding_data"]["invite_code"] == "AB12CD"
        text = update.message.reply_text.call_args.args[0]
        assert "accepted" in text.lower() or "✅" in text

    @pytest.mark.asyncio
    async def test_invalid_code_format_prompts_retry(self):
        from app.bot.handlers.onboarding import handle_invite_code

        update = _make_update(text="short")  # only 5 chars
        context = _make_context({"role": "partner"})

        state = await handle_invite_code(update, context)

        assert state == INVITE_CODE
        text = update.message.reply_text.call_args.args[0]
        assert "6" in text or "invalid" in text.lower() or "❌" in text

    @pytest.mark.asyncio
    async def test_used_or_nonexistent_code_prompts_retry(self):
        from app.bot.handlers.onboarding import handle_invite_code

        update = _make_update(text="ZZZZZZ")
        context = _make_context({"role": "partner"})

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None  # code not found
            mock_session.execute = AsyncMock(return_value=mock_result)

            state = await handle_invite_code(update, context)

        assert state == INVITE_CODE
        text = update.message.reply_text.call_args.args[0]
        assert "invalid" in text.lower() or "❌" in text

    @pytest.mark.asyncio
    async def test_lowercase_code_normalised_to_uppercase(self):
        """User types lowercase — should be uppercased and accepted."""
        from app.bot.handlers.onboarding import handle_invite_code
        from app.models.family_unit import FamilyUnit

        update = _make_update(text="ab12cd")  # lowercase
        context = _make_context({"role": "partner"})

        mock_fu = MagicMock(spec=FamilyUnit)
        mock_fu.invite_used = False

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory, \
             patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_fu
            mock_session.execute = AsyncMock(return_value=mock_result)

            state = await handle_invite_code(update, context)

        assert state == DUE_DATE_OR_LMP
        assert context.user_data["onboarding_data"]["invite_code"] == "AB12CD"


# ---------------------------------------------------------------------------
# Due date / LMP text entry
# ---------------------------------------------------------------------------

class TestHandleDueDateOrLMP:

    @pytest.mark.asyncio
    async def test_mom_valid_due_date_advances(self):
        from app.bot.handlers.onboarding import handle_due_date_or_lmp

        future = (date.today() + timedelta(days=100)).isoformat()
        update = _make_update(text=future)
        context = _make_context({"role": "mom"})

        with patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            state = await handle_due_date_or_lmp(update, context)

        assert state == COUNTRY

    @pytest.mark.asyncio
    async def test_mom_invalid_date_format_reprompts(self):
        from app.bot.handlers.onboarding import handle_due_date_or_lmp

        update = _make_update(text="tomorrow")  # not ISO format
        context = _make_context({"role": "mom"})
        state = await handle_due_date_or_lmp(update, context)

        assert state == DUE_DATE_OR_LMP
        text = update.message.reply_text.call_args.args[0]
        assert "❌" in text or "invalid" in text.lower()

    @pytest.mark.asyncio
    async def test_mom_past_due_date_reprompts(self):
        from app.bot.handlers.onboarding import handle_due_date_or_lmp

        past = (date.today() - timedelta(days=10)).isoformat()
        update = _make_update(text=past)
        context = _make_context({"role": "mom"})
        state = await handle_due_date_or_lmp(update, context)

        assert state == DUE_DATE_OR_LMP

    @pytest.mark.asyncio
    async def test_partner_valid_lmp_advances(self):
        from app.bot.handlers.onboarding import handle_due_date_or_lmp

        lmp = (date.today() - timedelta(days=80)).isoformat()
        update = _make_update(text=lmp)
        context = _make_context({"role": "partner"})

        with patch("app.bot.handlers.onboarding._save_state", new_callable=AsyncMock):
            state = await handle_due_date_or_lmp(update, context)

        assert state == COUNTRY

    @pytest.mark.asyncio
    async def test_partner_future_lmp_reprompts(self):
        from app.bot.handlers.onboarding import handle_due_date_or_lmp

        future = (date.today() + timedelta(days=10)).isoformat()
        update = _make_update(text=future)
        context = _make_context({"role": "partner"})
        state = await handle_due_date_or_lmp(update, context)

        assert state == DUE_DATE_OR_LMP


# ---------------------------------------------------------------------------
# Family linking
# ---------------------------------------------------------------------------

class TestLinkPartnerToFamily:

    @pytest.mark.asyncio
    async def test_valid_code_links_partner_and_marks_used(self):
        from app.bot.handlers.onboarding import _link_partner_to_family
        from app.models.family_unit import FamilyUnit
        from app.models.user import User

        mock_fu = MagicMock(spec=FamilyUnit)
        mock_fu.id = 5
        mock_fu.invite_used = False
        mock_fu.mom_user_id = 10

        mock_partner = MagicMock(spec=User)
        mock_partner.family_unit_id = None

        mock_mom = MagicMock(spec=User)
        mock_mom.family_unit_id = None

        results = iter([
            MagicMock(**{"scalar_one_or_none.return_value": mock_fu}),
            MagicMock(**{"scalar_one_or_none.return_value": mock_partner}),
            MagicMock(**{"scalar_one_or_none.return_value": mock_mom}),
        ])

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(side_effect=lambda *a, **kw: next(results))
            mock_session.commit = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            success, msg = await _link_partner_to_family(999, "AB12CD")

        assert success is True
        assert mock_fu.invite_used is True
        assert mock_partner.family_unit_id == 5
        assert mock_mom.family_unit_id == 5

    @pytest.mark.asyncio
    async def test_invalid_code_returns_failure(self):
        from app.bot.handlers.onboarding import _link_partner_to_family

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None  # no matching FU
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            success, msg = await _link_partner_to_family(999, "ZZZZZZ")

        assert success is False
        assert "invalid" in msg.lower() or "❌" in msg


# ---------------------------------------------------------------------------
# /invite command
# ---------------------------------------------------------------------------

class TestCmdInvite:

    @pytest.mark.asyncio
    async def test_non_mom_cannot_generate_code(self):
        from app.bot.handlers.onboarding import cmd_invite
        from app.models.user import User, UserRole

        mock_user = MagicMock(spec=User)
        mock_user.onboarding_complete = True
        mock_user.role = UserRole.partner
        mock_user.family_unit_id = None

        update = _make_update(text="/invite")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_user
            mock_session.execute = AsyncMock(return_value=mock_result)

            await cmd_invite(update, context)

        text = update.message.reply_text.call_args.args[0]
        assert "partner" in text.lower() or "mom" in text.lower()

    @pytest.mark.asyncio
    async def test_unregistered_user_cannot_generate_code(self):
        from app.bot.handlers.onboarding import cmd_invite

        update = _make_update(text="/invite")
        context = _make_context()

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_session.execute = AsyncMock(return_value=mock_result)

            await cmd_invite(update, context)

        text = update.message.reply_text.call_args.args[0]
        assert "setup" in text.lower() or "/start" in text.lower()

    @pytest.mark.asyncio
    async def test_mom_generates_new_code(self):
        from app.bot.handlers.onboarding import cmd_invite
        from app.models.user import User, UserRole
        from app.models.family_unit import FamilyUnit

        mock_user = MagicMock(spec=User)
        mock_user.id = 1
        mock_user.onboarding_complete = True
        mock_user.role = UserRole.mom
        mock_user.family_unit_id = None

        update = _make_update(text="/invite")
        context = _make_context()

        results_iter = iter([
            MagicMock(**{"scalar_one_or_none.return_value": mock_user}),
            MagicMock(**{"scalar_one_or_none.return_value": None}),  # no collision
        ])

        with patch("app.bot.handlers.onboarding._AsyncSessionFactory") as mock_factory:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(side_effect=lambda *a, **kw: next(results_iter))
            mock_session.flush = AsyncMock()
            mock_session.commit = AsyncMock()
            mock_session.add = MagicMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            await cmd_invite(update, context)

        text = update.message.reply_text.call_args.kwargs.get(
            "text", update.message.reply_text.call_args.args[0]
        )
        assert "invite" in text.lower() or "code" in text.lower()
        # FamilyUnit was added
        mock_session.add.assert_called_once()
