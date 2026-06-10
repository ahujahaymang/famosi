"""
Unit tests for app/components/pregnancy_engine.py

Covers:
  - calculate_gestational_age formula and boundary values
  - get_current_week helper
  - validate_due_date (valid, too near, too far, exact boundaries)
  - validate_lmp (valid, too near, too far, exact boundaries)
  - lmp_to_due_date conversion

Requirements: 3.1, 1.2, 1.3
"""

import pytest
from datetime import date, timedelta

from app.components.pregnancy_engine import (
    DueDateValidationError,
    LMPValidationError,
    PREGNANCY_DAYS,
    calculate_gestational_age,
    get_current_week,
    lmp_to_due_date,
    validate_due_date,
    validate_lmp,
)


# ── calculate_gestational_age ────────────────────────────────────────────────


class TestCalculateGestationalAge:
    def test_due_date_equals_today_is_40_weeks(self):
        today = date(2025, 6, 1)
        due = today  # 0 days until due → 280 days pregnant → week 40, day 0
        weeks, days = calculate_gestational_age(due, today)
        assert weeks == 40
        assert days == 0

    def test_280_days_until_due_is_week_0_day_0(self):
        today = date(2025, 1, 1)
        due = today + timedelta(days=280)  # 280 days until due → 0 days pregnant
        weeks, days = calculate_gestational_age(due, today)
        assert weeks == 0
        assert days == 0

    def test_typical_mid_pregnancy(self):
        # 140 days until due → 280-140 = 140 total days → 20 weeks, 0 days
        today = date(2025, 4, 1)
        due = today + timedelta(days=140)
        weeks, days = calculate_gestational_age(due, today)
        assert weeks == 20
        assert days == 0

    def test_partial_week(self):
        # 138 days until due → 280-138 = 142 total days → 20 weeks, 2 days
        today = date(2025, 4, 1)
        due = today + timedelta(days=138)
        weeks, days = calculate_gestational_age(due, today)
        assert weeks == 20
        assert days == 2

    def test_formula_invariant_weeks_times_7_plus_days_equals_total(self):
        """weeks * 7 + days must always equal 280 - days_until_due."""
        today = date(2025, 3, 15)
        for days_until_due in range(0, 281):
            due = today + timedelta(days=days_until_due)
            weeks, extra_days = calculate_gestational_age(due, today)
            expected_total = PREGNANCY_DAYS - days_until_due
            assert weeks * 7 + extra_days == expected_total, (
                f"Failed for days_until_due={days_until_due}"
            )

    def test_days_component_is_never_negative(self):
        today = date(2025, 5, 1)
        for n in range(0, 281):
            due = today + timedelta(days=n)
            _weeks, days = calculate_gestational_age(due, today)
            assert days >= 0

    def test_days_component_is_less_than_7(self):
        today = date(2025, 5, 1)
        for n in range(0, 281):
            due = today + timedelta(days=n)
            _weeks, days = calculate_gestational_age(due, today)
            assert days < 7

    def test_past_due_date_returns_weeks_above_40(self):
        today = date(2025, 6, 15)
        due = today - timedelta(days=7)  # 1 week overdue
        weeks, days = calculate_gestational_age(due, today)
        assert weeks == 41
        assert days == 0


# ── get_current_week ─────────────────────────────────────────────────────────


class TestGetCurrentWeek:
    def test_returns_weeks_component(self):
        today = date(2025, 4, 1)
        due = today + timedelta(days=100)  # 280-100 = 180 days → 25 weeks, 5 days
        assert get_current_week(due, today) == 25

    def test_matches_calculate_gestational_age_weeks(self):
        today = date(2025, 3, 20)
        due = today + timedelta(days=60)
        weeks, _ = calculate_gestational_age(due, today)
        assert get_current_week(due, today) == weeks


# ── validate_due_date ────────────────────────────────────────────────────────


class TestValidateDueDate:
    def _today(self) -> date:
        return date(2025, 6, 1)

    def test_valid_due_date_does_not_raise(self):
        today = self._today()
        validate_due_date(today + timedelta(days=100), today)  # no exception

    def test_minimum_boundary_1_day_does_not_raise(self):
        today = self._today()
        validate_due_date(today + timedelta(days=1), today)

    def test_maximum_boundary_280_days_does_not_raise(self):
        today = self._today()
        validate_due_date(today + timedelta(days=280), today)

    def test_same_day_raises(self):
        today = self._today()
        with pytest.raises(DueDateValidationError):
            validate_due_date(today, today)

    def test_past_date_raises(self):
        today = self._today()
        with pytest.raises(DueDateValidationError):
            validate_due_date(today - timedelta(days=1), today)

    def test_too_far_future_raises(self):
        today = self._today()
        with pytest.raises(DueDateValidationError):
            validate_due_date(today + timedelta(days=281), today)

    def test_error_message_is_descriptive(self):
        today = self._today()
        with pytest.raises(DueDateValidationError, match="Due date must be between"):
            validate_due_date(today, today)


# ── validate_lmp ─────────────────────────────────────────────────────────────


class TestValidateLMP:
    def _today(self) -> date:
        return date(2025, 6, 1)

    def test_valid_lmp_does_not_raise(self):
        today = self._today()
        validate_lmp(today - timedelta(days=100), today)

    def test_minimum_boundary_1_day_past_does_not_raise(self):
        today = self._today()
        validate_lmp(today - timedelta(days=1), today)

    def test_maximum_boundary_280_days_past_does_not_raise(self):
        today = self._today()
        validate_lmp(today - timedelta(days=280), today)

    def test_same_day_raises(self):
        today = self._today()
        with pytest.raises(LMPValidationError):
            validate_lmp(today, today)

    def test_future_date_raises(self):
        today = self._today()
        with pytest.raises(LMPValidationError):
            validate_lmp(today + timedelta(days=1), today)

    def test_too_far_past_raises(self):
        today = self._today()
        with pytest.raises(LMPValidationError):
            validate_lmp(today - timedelta(days=281), today)

    def test_error_message_is_descriptive(self):
        today = self._today()
        with pytest.raises(LMPValidationError, match="LMP must be between"):
            validate_lmp(today, today)


# ── lmp_to_due_date ──────────────────────────────────────────────────────────


class TestLMPToDueDate:
    def test_adds_280_days(self):
        lmp = date(2025, 1, 1)
        expected = date(2025, 10, 8)  # 280 days after Jan 1 2025
        result = lmp_to_due_date(lmp)
        assert result == lmp + timedelta(days=280)
        assert result == expected

    def test_round_trip_gestational_age_at_conception(self):
        """On the LMP date, gestational age should be 0 weeks, 0 days."""
        lmp = date(2025, 2, 15)
        due = lmp_to_due_date(lmp)
        weeks, days = calculate_gestational_age(due, lmp)
        assert weeks == 0
        assert days == 0
