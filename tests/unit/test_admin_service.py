"""
Unit tests for app/services/admin_service.py

Tests admin identity check, pending registry, in-memory metrics,
and DB helpers (approve_user, reject_user, get_db_stats).

Requirements: admin panel feature
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.admin_service import (
    is_admin,
    mark_pending,
    clear_pending,
    is_pending,
    list_pending,
    metrics,
    _MetricsStore,
)


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------

class TestIsAdmin:

    def test_configured_admin_id_returns_true(self):
        with patch("app.services.admin_service.settings") as mock_settings:
            mock_settings.admin_telegram_user_id = 12345
            assert is_admin(12345) is True

    def test_different_user_returns_false(self):
        with patch("app.services.admin_service.settings") as mock_settings:
            mock_settings.admin_telegram_user_id = 12345
            assert is_admin(99999) is False

    def test_zero_admin_id_always_false(self):
        with patch("app.services.admin_service.settings") as mock_settings:
            mock_settings.admin_telegram_user_id = 0
            assert is_admin(0) is False
            assert is_admin(12345) is False


# ---------------------------------------------------------------------------
# Pending registry
# ---------------------------------------------------------------------------

class TestPendingRegistry:

    def setup_method(self):
        # Ensure clean state before each test
        from app.services.admin_service import _pending_approval
        _pending_approval.clear()

    def test_mark_and_check_pending(self):
        mark_pending(111, user_id=1, role="mom", country="IN")
        assert is_pending(111) is True

    def test_clear_removes_from_pending(self):
        mark_pending(222, user_id=2, role="partner", country="US")
        clear_pending(222)
        assert is_pending(222) is False

    def test_not_pending_by_default(self):
        assert is_pending(999) is False

    def test_list_pending_returns_all(self):
        mark_pending(333, user_id=3, role="mom", country="IN")
        mark_pending(444, user_id=4, role="partner", country="US")
        entries = list_pending()
        tids = {e["telegram_user_id"] for e in entries}
        assert 333 in tids
        assert 444 in tids

    def test_pending_entry_has_required_fields(self):
        mark_pending(555, user_id=5, role="mom", country="GB")
        entries = list_pending()
        entry = next(e for e in entries if e["telegram_user_id"] == 555)
        assert "role" in entry
        assert "country" in entry
        assert "user_id" in entry
        assert "requested_at" in entry

    def test_clear_nonexistent_is_safe(self):
        clear_pending(9999)  # should not raise


# ---------------------------------------------------------------------------
# In-memory metrics
# ---------------------------------------------------------------------------

class TestMetricsStore:

    def setup_method(self):
        self.store = _MetricsStore()

    def test_record_request_increments_count(self):
        self.store.record_request(
            user_id=1, intent="LOGGING", model_used="gpt-4.1-nano",
            tokens_used=50, latency_ms=300
        )
        summary = self.store.summary(window_hours=1)
        assert summary["requests"]["total"] == 1

    def test_multiple_requests_counted(self):
        for i in range(5):
            self.store.record_request(
                user_id=i, intent="LOGGING", model_used="gpt-4.1-nano",
                tokens_used=50, latency_ms=200 + i * 10
            )
        summary = self.store.summary(window_hours=1)
        assert summary["requests"]["total"] == 5

    def test_active_users_counted_uniquely(self):
        self.store.record_request(user_id=1, intent="LOGGING", model_used="nano", tokens_used=50, latency_ms=200)
        self.store.record_request(user_id=1, intent="LOGGING", model_used="nano", tokens_used=50, latency_ms=200)
        self.store.record_request(user_id=2, intent="LOGGING", model_used="nano", tokens_used=50, latency_ms=200)
        summary = self.store.summary(window_hours=1)
        assert summary["users"]["active_24h"] == 2  # unique user IDs

    def test_token_cost_estimated(self):
        self.store.record_request(
            user_id=1, intent="KNOWLEDGE_QUESTION",
            model_used="gpt-4.1-mini", tokens_used=1000, latency_ms=500
        )
        summary = self.store.summary(window_hours=1)
        assert summary["llm"]["total_tokens"] == 1000
        assert summary["llm"]["total_cost_usd"] > 0

    def test_intent_counts_tracked(self):
        for intent in ["LOGGING", "LOGGING", "KNOWLEDGE_QUESTION", "PERSONAL_DATA_QUERY"]:
            self.store.record_request(
                user_id=1, intent=intent, model_used="nano", tokens_used=50, latency_ms=100
            )
        summary = self.store.summary(window_hours=1)
        assert summary["intents"]["LOGGING"] == 2
        assert summary["intents"]["KNOWLEDGE_QUESTION"] == 1

    def test_error_recording(self):
        self.store.record_error(user_id=1, error_type="llm_timeout")
        self.store.record_error(user_id=2, error_type="db_error")
        summary = self.store.summary(window_hours=1)
        assert summary["errors"]["total"] == 2
        assert summary["errors"]["by_type"]["llm_timeout"] == 1

    def test_latency_percentiles(self):
        latencies = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        for ms in latencies:
            self.store.record_request(
                user_id=1, intent="LOGGING", model_used="nano", tokens_used=50, latency_ms=ms
            )
        summary = self.store.summary(window_hours=1)
        assert summary["requests"]["latency_p50_ms"] > 0
        assert summary["requests"]["latency_p95_ms"] >= summary["requests"]["latency_p50_ms"]

    def test_daily_digest_text_contains_key_sections(self):
        self.store.record_request(
            user_id=1, intent="LOGGING", model_used="gpt-4.1-mini", tokens_used=200, latency_ms=400
        )
        digest = self.store.daily_digest_text()
        assert "Famosi" in digest
        assert "Users" in digest
        assert "Requests" in digest
        assert "LLM" in digest
        assert "Intents" in digest

    def test_empty_metrics_returns_zeros(self):
        summary = self.store.summary(window_hours=1)
        assert summary["requests"]["total"] == 0
        assert summary["llm"]["total_calls"] == 0
        assert summary["errors"]["total"] == 0
        assert summary["users"]["active_24h"] == 0

    def test_old_requests_excluded_from_window(self):
        """Requests older than the window should not appear in the summary."""
        # Manually inject an old entry
        old_time = time.time() - 25 * 3600  # 25 hours ago
        self.store._requests.append({
            "ts": old_time, "user_id": 1, "intent": "LOGGING",
            "model": "nano", "tokens": 50, "latency_ms": 200, "cost_usd": 0.001
        })
        # And a recent one
        self.store.record_request(user_id=2, intent="LOGGING", model_used="nano", tokens_used=50, latency_ms=200)

        summary = self.store.summary(window_hours=24)
        # Only the recent one should be in the 24h window
        assert summary["requests"]["total"] == 1
        assert summary["users"]["active_24h"] == 1


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

class TestAdminDBHelpers:

    @pytest.mark.asyncio
    async def test_approve_user_activates_trial(self):
        from app.services.admin_service import approve_user_db
        from app.models.user import User
        from app.models.subscription import Subscription, SubscriptionStatus

        mock_user = MagicMock(spec=User)
        mock_user.id = 10

        mock_sub = MagicMock(spec=Subscription)
        mock_sub.subscription_status = SubscriptionStatus.trial
        mock_sub.trial_end = MagicMock()
        mock_sub.trial_end.isoformat.return_value = "2025-07-01"
        mock_sub.trial_end.strftime.return_value = "01 Jul 2025"

        db = AsyncMock()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = mock_user
        db.execute = AsyncMock(return_value=user_result)
        db.commit = AsyncMock()

        with patch("app.services.admin_service.PaymentStateMachine") as mock_sm_cls, \
             patch("app.services.admin_service.clear_pending") as mock_clear:
            mock_sm = MagicMock()
            mock_sm.activate_trial = AsyncMock(return_value=mock_sub)
            mock_sm_cls.return_value = mock_sm

            result_text = await approve_user_db(db, 77777)

        assert "approved" in result_text.lower() or "✅" in result_text
        mock_sm.activate_trial.assert_awaited_once_with(10, db)
        mock_clear.assert_called_once_with(77777)

    @pytest.mark.asyncio
    async def test_approve_nonexistent_user_raises(self):
        from app.services.admin_service import approve_user_db

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        with pytest.raises(LookupError):
            await approve_user_db(db, 99999)

    @pytest.mark.asyncio
    async def test_reject_user_deletes_record(self):
        from app.services.admin_service import reject_user_db
        from app.models.user import User

        mock_user = MagicMock(spec=User)
        mock_user.id = 5

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = mock_user
        db.execute = AsyncMock(return_value=result)
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        with patch("app.services.admin_service.clear_pending") as mock_clear:
            result_text = await reject_user_db(db, 88888)

        db.delete.assert_awaited_once_with(mock_user)
        assert "rejected" in result_text.lower() or "🚫" in result_text
        mock_clear.assert_called_once_with(88888)

    @pytest.mark.asyncio
    async def test_reject_nonexistent_user_raises(self):
        from app.services.admin_service import reject_user_db

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        with pytest.raises(LookupError):
            await reject_user_db(db, 99999)
