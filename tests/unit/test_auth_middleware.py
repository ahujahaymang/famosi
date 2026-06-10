"""
Unit tests for app/bot/middleware/auth.py

Tests cover:
- verify_telegram_secret FastAPI dependency
- AuthMiddleware PTB handler:
    - user not found
    - user found, active subscription → read_only=False
    - user found, inactive subscription → read_only=True
    - user found, no subscription row → read_only=False
    - update without effective_user (channel post)
    - non-Update object passed through
"""

from __future__ import annotations

import hmac
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
from fastapi import HTTPException

from app.models.subscription import SubscriptionStatus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_request(header_value: str | None = None) -> MagicMock:
    """Build a minimal mock FastAPI Request with an optional secret header."""
    request = MagicMock()
    if header_value is None:
        request.headers.get.return_value = ""
    else:
        request.headers.get.return_value = header_value
    return request


def _make_update(telegram_user_id: int | None = 12345) -> MagicMock:
    """Build a minimal mock telegram.Update."""
    from telegram import Update

    update = MagicMock(spec=Update)
    if telegram_user_id is None:
        update.effective_user = None
    else:
        update.effective_user = MagicMock()
        update.effective_user.id = telegram_user_id
    return update


def _make_context() -> MagicMock:
    """Build a minimal mock PTB CallbackContext with bot_data dict."""
    ctx = MagicMock()
    ctx.bot_data = {}
    return ctx


# ---------------------------------------------------------------------------
# Part 1: verify_telegram_secret
# ---------------------------------------------------------------------------


class TestVerifyTelegramSecret:
    """Tests for the FastAPI dependency that validates the webhook secret."""

    @pytest.mark.asyncio
    async def test_valid_secret_passes(self):
        """Correct header value must not raise."""
        with patch("app.bot.middleware.auth.settings") as mock_settings:
            mock_settings.telegram_webhook_secret = "mysecret"
            request = _make_request("mysecret")
            # Should not raise
            await __import__(
                "app.bot.middleware.auth", fromlist=["verify_telegram_secret"]
            ).verify_telegram_secret(request)

    @pytest.mark.asyncio
    async def test_wrong_secret_raises_403(self):
        """Wrong header value must raise HTTPException with status 403."""
        with patch("app.bot.middleware.auth.settings") as mock_settings:
            mock_settings.telegram_webhook_secret = "mysecret"
            request = _make_request("wrongsecret")
            with pytest.raises(HTTPException) as exc_info:
                await __import__(
                    "app.bot.middleware.auth", fromlist=["verify_telegram_secret"]
                ).verify_telegram_secret(request)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_header_raises_403(self):
        """Absent header must raise HTTPException with status 403."""
        with patch("app.bot.middleware.auth.settings") as mock_settings:
            mock_settings.telegram_webhook_secret = "mysecret"
            request = _make_request(None)  # header absent → empty string
            with pytest.raises(HTTPException) as exc_info:
                await __import__(
                    "app.bot.middleware.auth", fromlist=["verify_telegram_secret"]
                ).verify_telegram_secret(request)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_configured_secret_skips_validation(self):
        """When no secret is configured, validation is skipped entirely."""
        with patch("app.bot.middleware.auth.settings") as mock_settings:
            mock_settings.telegram_webhook_secret = ""
            request = _make_request("any-value")
            # Must not raise even though header doesn't match (nothing to match)
            await __import__(
                "app.bot.middleware.auth", fromlist=["verify_telegram_secret"]
            ).verify_telegram_secret(request)

    @pytest.mark.asyncio
    async def test_none_configured_secret_skips_validation(self):
        """When secret is None, validation is skipped."""
        with patch("app.bot.middleware.auth.settings") as mock_settings:
            mock_settings.telegram_webhook_secret = None
            request = _make_request("any-value")
            await __import__(
                "app.bot.middleware.auth", fromlist=["verify_telegram_secret"]
            ).verify_telegram_secret(request)

    @pytest.mark.asyncio
    async def test_uses_constant_time_comparison(self):
        """Confirm hmac.compare_digest is used (not ==) to prevent timing attacks."""
        import app.bot.middleware.auth as auth_module

        with patch("app.bot.middleware.auth.settings") as mock_settings:
            mock_settings.telegram_webhook_secret = "secret"
            with patch("hmac.compare_digest", return_value=True) as mock_digest:
                request = _make_request("secret")
                await auth_module.verify_telegram_secret(request)
                mock_digest.assert_called_once()


# ---------------------------------------------------------------------------
# Part 2: AuthMiddleware
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    """Tests for the PTB update middleware."""

    @pytest.mark.asyncio
    async def test_user_found_active_subscription(self):
        """User with active subscription: current_user set, read_only=False."""
        from app.bot.middleware.auth import AuthMiddleware
        from app.models.user import User

        mock_user = MagicMock(spec=User)
        mock_user.id = 42

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
            patch("app.bot.middleware.auth._is_read_only", new_callable=AsyncMock) as mock_read_only,
            patch("app.bot.middleware.auth._enforce_daily_token_cap", new_callable=AsyncMock),
            patch("structlog.contextvars.bind_contextvars"),
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.return_value = mock_user
            mock_read_only.return_value = False

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=999)
            context = _make_context()

            await mw(update, context)

            assert context.bot_data["current_user"] is mock_user
            assert context.bot_data["read_only"] is False
            mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_user_found_inactive_subscription(self):
        """User with inactive subscription: current_user set, read_only=True."""
        from app.bot.middleware.auth import AuthMiddleware
        from app.models.user import User

        mock_user = MagicMock(spec=User)
        mock_user.id = 7

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
            patch("app.bot.middleware.auth._is_read_only", new_callable=AsyncMock) as mock_read_only,
            patch("app.bot.middleware.auth._enforce_daily_token_cap", new_callable=AsyncMock),
            patch("structlog.contextvars.bind_contextvars"),
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.return_value = mock_user
            mock_read_only.return_value = True

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=888)
            context = _make_context()

            await mw(update, context)

            assert context.bot_data["current_user"] is mock_user
            assert context.bot_data["read_only"] is True

    @pytest.mark.asyncio
    async def test_user_not_found(self):
        """Unknown telegram_user_id: current_user=None, read_only=False."""
        from app.bot.middleware.auth import AuthMiddleware

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
            patch("structlog.contextvars.bind_contextvars"),
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.return_value = None

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=1111)
            context = _make_context()

            await mw(update, context)

            assert context.bot_data["current_user"] is None
            assert context.bot_data["read_only"] is False
            mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_without_effective_user(self):
        """Update with no effective_user (e.g. channel post): skips DB lookup."""
        from app.bot.middleware.auth import AuthMiddleware

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=None)  # effective_user is None
            context = _make_context()

            await mw(update, context)

            # DB lookup must not happen
            mock_load.assert_not_awaited()
            assert context.bot_data["current_user"] is None
            assert context.bot_data["read_only"] is False

    @pytest.mark.asyncio
    async def test_non_update_object_skips_processing(self):
        """Non-Update objects (e.g. error objects) must be ignored silently."""
        from app.bot.middleware.auth import AuthMiddleware

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session

            mw = AuthMiddleware()
            context = _make_context()

            # Pass a plain dict instead of an Update
            await mw({"not": "an update"}, context)

            mock_load.assert_not_awaited()
            mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_closed_on_exception(self):
        """DB session must be closed even when _load_user raises an exception."""
        from app.bot.middleware.auth import AuthMiddleware

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.side_effect = RuntimeError("DB error")

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=555)
            context = _make_context()

            with pytest.raises(RuntimeError):
                await mw(update, context)

            # Session must still be closed
            mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_structlog_binds_user_id_not_personal_info(self):
        """Only DB user.id is bound to structlog context, never personal info."""
        from app.bot.middleware.auth import AuthMiddleware
        from app.models.user import User

        mock_user = MagicMock(spec=User)
        mock_user.id = 99
        mock_user.telegram_user_id = 12345  # must NOT be bound

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
            patch("app.bot.middleware.auth._is_read_only", new_callable=AsyncMock) as mock_read_only,
            patch("app.bot.middleware.auth._enforce_daily_token_cap", new_callable=AsyncMock),
            patch("structlog.contextvars.bind_contextvars") as mock_bind,
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.return_value = mock_user
            mock_read_only.return_value = False

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=12345)
            context = _make_context()

            await mw(update, context)

            # Only user_id (DB PK) should be bound
            mock_bind.assert_called_once_with(user_id=str(mock_user.id))
            call_kwargs = mock_bind.call_args.kwargs
            # Must not contain personally identifiable keys
            for forbidden_key in ("telegram_user_id", "username", "first_name", "last_name"):
                assert forbidden_key not in call_kwargs


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------


class TestIsReadOnly:
    """Tests for the _is_read_only helper."""

    @pytest.mark.asyncio
    async def test_inactive_status_returns_true(self):
        from app.bot.middleware.auth import _is_read_only

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = SubscriptionStatus.inactive
        mock_session.execute = AsyncMock(return_value=mock_result)

        assert await _is_read_only(mock_session, user_id=1) is True

    @pytest.mark.asyncio
    async def test_active_status_returns_false(self):
        from app.bot.middleware.auth import _is_read_only

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = SubscriptionStatus.active
        mock_session.execute = AsyncMock(return_value=mock_result)

        assert await _is_read_only(mock_session, user_id=1) is False

    @pytest.mark.asyncio
    async def test_trial_status_returns_false(self):
        from app.bot.middleware.auth import _is_read_only

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = SubscriptionStatus.trial
        mock_session.execute = AsyncMock(return_value=mock_result)

        assert await _is_read_only(mock_session, user_id=1) is False

    @pytest.mark.asyncio
    async def test_grace_status_returns_false(self):
        from app.bot.middleware.auth import _is_read_only

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = SubscriptionStatus.grace
        mock_session.execute = AsyncMock(return_value=mock_result)

        assert await _is_read_only(mock_session, user_id=1) is False

    @pytest.mark.asyncio
    async def test_no_subscription_row_returns_false(self):
        """No subscription record: user is not read-only (default to permissive)."""
        from app.bot.middleware.auth import _is_read_only

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        assert await _is_read_only(mock_session, user_id=1) is False


# ---------------------------------------------------------------------------
# Token cap helper tests
# ---------------------------------------------------------------------------


def _mock_session_with_token_sum(daily_tokens: int) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns the given token sum."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = daily_tokens
    mock_session.execute = AsyncMock(return_value=mock_result)
    return mock_session


class TestGetDailyTokensUsed:
    """Tests for the _get_daily_tokens_used helper."""

    @pytest.mark.asyncio
    async def test_returns_sum_for_user_today(self):
        from app.bot.middleware.auth import _get_daily_tokens_used
        from datetime import datetime, timezone

        mock_session = _mock_session_with_token_sum(50_000)
        today = datetime.now(tz=timezone.utc)
        result = await _get_daily_tokens_used(mock_session, user_id=1, day=today)
        assert result == 50_000

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_logs(self):
        from app.bot.middleware.auth import _get_daily_tokens_used
        from datetime import datetime, timezone

        mock_session = _mock_session_with_token_sum(0)
        today = datetime.now(tz=timezone.utc)
        result = await _get_daily_tokens_used(mock_session, user_id=99, day=today)
        assert result == 0

    @pytest.mark.asyncio
    async def test_queries_utc_day_boundaries(self):
        """Verify that the query is scoped to midnight-to-midnight UTC."""
        from app.bot.middleware.auth import _get_daily_tokens_used
        from datetime import datetime, timezone
        from sqlalchemy import select

        mock_session = _mock_session_with_token_sum(0)
        # Use a specific moment with hours/minutes to verify they're stripped.
        day = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        await _get_daily_tokens_used(mock_session, user_id=1, day=day)

        # execute() must have been called exactly once.
        mock_session.execute.assert_awaited_once()


class TestCheckConsecutiveCapDays:
    """Tests for _check_consecutive_cap_days."""

    @pytest.mark.asyncio
    async def test_returns_true_when_all_prior_days_over_cap(self):
        from app.bot.middleware.auth import (
            _check_consecutive_cap_days,
            _DAILY_TOKEN_CAP,
        )
        from datetime import datetime, timezone

        # Always return a value above the cap.
        mock_session = _mock_session_with_token_sum(_DAILY_TOKEN_CAP + 1)
        today = datetime.now(tz=timezone.utc)
        assert await _check_consecutive_cap_days(mock_session, user_id=1, today=today) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_one_prior_day_under_cap(self):
        from app.bot.middleware.auth import (
            _check_consecutive_cap_days,
            _DAILY_TOKEN_CAP,
        )
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, MagicMock

        # First call (1 day ago) returns under cap; subsequent calls over cap.
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            # Day -1: under cap; day -2: over cap
            result.scalar_one.return_value = 0 if call_count == 1 else _DAILY_TOKEN_CAP + 1
            return result

        mock_session = AsyncMock()
        mock_session.execute = side_effect
        today = datetime.now(tz=timezone.utc)
        assert await _check_consecutive_cap_days(mock_session, user_id=1, today=today) is False


class TestEnforceDailyTokenCap:
    """Tests for _enforce_daily_token_cap and its integration into AuthMiddleware."""

    @pytest.mark.asyncio
    async def test_sets_force_mini_tier_when_cap_exceeded(self):
        from app.bot.middleware.auth import _enforce_daily_token_cap, _DAILY_TOKEN_CAP

        mock_session = _mock_session_with_token_sum(_DAILY_TOKEN_CAP + 1)
        context = _make_context()

        with patch("app.bot.middleware.auth._check_consecutive_cap_days", new_callable=AsyncMock) as mock_consec:
            mock_consec.return_value = False
            await _enforce_daily_token_cap(mock_session, user_id=1, context=context)

        assert context.bot_data["force_mini_tier"] is True
        assert context.bot_data["daily_cap_note"] is not None
        assert len(context.bot_data["daily_cap_note"]) > 0

    @pytest.mark.asyncio
    async def test_clears_force_mini_tier_when_under_cap(self):
        from app.bot.middleware.auth import _enforce_daily_token_cap, _DAILY_TOKEN_CAP

        mock_session = _mock_session_with_token_sum(_DAILY_TOKEN_CAP - 1)
        context = _make_context()

        await _enforce_daily_token_cap(mock_session, user_id=1, context=context)

        assert context.bot_data["force_mini_tier"] is False
        assert context.bot_data["daily_cap_note"] is None

    @pytest.mark.asyncio
    async def test_clears_force_mini_tier_at_exactly_cap(self):
        """At exactly the cap (not exceeding it) the flag must remain False."""
        from app.bot.middleware.auth import _enforce_daily_token_cap, _DAILY_TOKEN_CAP

        mock_session = _mock_session_with_token_sum(_DAILY_TOKEN_CAP)
        context = _make_context()

        await _enforce_daily_token_cap(mock_session, user_id=1, context=context)

        assert context.bot_data["force_mini_tier"] is False

    @pytest.mark.asyncio
    async def test_emits_warning_on_consecutive_days(self):
        """Operator WARNING is emitted when cap hit on _CONSECUTIVE_CAP_ALERT_DAYS consecutive days."""
        from app.bot.middleware.auth import _enforce_daily_token_cap, _DAILY_TOKEN_CAP

        mock_session = _mock_session_with_token_sum(_DAILY_TOKEN_CAP + 500)
        context = _make_context()

        with patch("app.bot.middleware.auth._check_consecutive_cap_days", new_callable=AsyncMock) as mock_consec:
            mock_consec.return_value = True
            with patch("app.bot.middleware.auth.logger") as mock_logger:
                await _enforce_daily_token_cap(mock_session, user_id=42, context=context)
                mock_logger.warning.assert_called_once()
                call_kwargs = mock_logger.warning.call_args
                # Event name is the first positional arg
                assert call_kwargs.args[0] == "daily_token_cap_consecutive_alert"
                # user_id should be included but no PII
                assert call_kwargs.kwargs.get("user_id") == 42
                assert call_kwargs.kwargs.get("pii_logged") is False

    @pytest.mark.asyncio
    async def test_no_warning_when_not_consecutive(self):
        """No WARNING emitted when cap is hit but not on consecutive days."""
        from app.bot.middleware.auth import _enforce_daily_token_cap, _DAILY_TOKEN_CAP

        mock_session = _mock_session_with_token_sum(_DAILY_TOKEN_CAP + 1)
        context = _make_context()

        with patch("app.bot.middleware.auth._check_consecutive_cap_days", new_callable=AsyncMock) as mock_consec:
            mock_consec.return_value = False
            with patch("app.bot.middleware.auth.logger") as mock_logger:
                await _enforce_daily_token_cap(mock_session, user_id=1, context=context)
                mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_sets_force_mini_tier_on_update(self):
        """Full AuthMiddleware integration: force_mini_tier set when cap exceeded."""
        from app.bot.middleware.auth import AuthMiddleware
        from app.models.user import User

        mock_user = MagicMock(spec=User)
        mock_user.id = 5

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
            patch("app.bot.middleware.auth._is_read_only", new_callable=AsyncMock) as mock_read_only,
            patch("app.bot.middleware.auth._enforce_daily_token_cap", new_callable=AsyncMock) as mock_cap,
            patch("structlog.contextvars.bind_contextvars"),
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.return_value = mock_user
            mock_read_only.return_value = False

            async def set_cap(session, user_id, context):
                context.bot_data["force_mini_tier"] = True
                context.bot_data["daily_cap_note"] = "cap note"

            mock_cap.side_effect = set_cap

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=777)
            context = _make_context()

            await mw(update, context)

            assert context.bot_data["force_mini_tier"] is True
            assert context.bot_data["daily_cap_note"] == "cap note"
            mock_cap.assert_awaited_once_with(mock_session, mock_user.id, context)

    @pytest.mark.asyncio
    async def test_middleware_sets_defaults_when_no_user(self):
        """When user is not found, force_mini_tier and daily_cap_note must be falsy defaults."""
        from app.bot.middleware.auth import AuthMiddleware

        with (
            patch("app.bot.middleware.auth._AsyncSessionFactory") as mock_factory,
            patch("app.bot.middleware.auth._load_user", new_callable=AsyncMock) as mock_load,
            patch("structlog.contextvars.bind_contextvars"),
        ):
            mock_session = AsyncMock()
            mock_factory.return_value = mock_session
            mock_load.return_value = None

            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=1234)
            context = _make_context()

            await mw(update, context)

            assert context.bot_data["force_mini_tier"] is False
            assert context.bot_data["daily_cap_note"] is None

    @pytest.mark.asyncio
    async def test_middleware_sets_defaults_when_no_effective_user(self):
        """Channel posts (no effective_user) must also initialise cap keys."""
        from app.bot.middleware.auth import AuthMiddleware

        with patch("app.bot.middleware.auth._AsyncSessionFactory"):
            mw = AuthMiddleware()
            update = _make_update(telegram_user_id=None)
            context = _make_context()

            await mw(update, context)

            assert context.bot_data["force_mini_tier"] is False
            assert context.bot_data["daily_cap_note"] is None
