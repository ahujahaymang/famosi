"""
Unit tests for app/memory/personal_memory.py

Tests CRUD and visibility enforcement:
  - create_meal / symptom / exercise / medication / weight_log / water_log /
    doctor_question / preference
  - get_records: mom sees all, partner sees only shared
  - update_visibility: logs change, skips no-op
  - Unsupported record types raise ValueError

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 19.4, 19.5, 19.6
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.memory.personal_memory import (
    visibility_filter,
    UserRole,
    _VISIBILITY_BEARING_TYPES,
    _RECORD_TYPE_MAP,
)
from app.models.meal import VisibilityLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_async_session() -> AsyncMock:
    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


# ---------------------------------------------------------------------------
# create_meal
# ---------------------------------------------------------------------------

class TestCreateMeal:

    @pytest.mark.asyncio
    async def test_creates_meal_and_items(self):
        from app.memory.personal_memory import create_meal
        from app.schemas.meal import MealExtraction

        db = _make_async_session()
        extraction = MealExtraction(items=[
            {"food_name": "oatmeal", "quantity": 1, "unit": "bowl"},
            {"food_name": "banana", "quantity": 1, "unit": "piece"},
        ])

        meal = await create_meal(db, extraction, user_id=1, logged_at=_utcnow())

        # Both meal and items added
        assert db.add.call_count == 3  # 1 meal + 2 items
        assert db.flush.call_count == 2

    @pytest.mark.asyncio
    async def test_meal_defaults_to_private_visibility(self):
        from app.memory.personal_memory import create_meal
        from app.schemas.meal import MealExtraction

        db = _make_async_session()
        extraction = MealExtraction(items=[{"food_name": "rice", "quantity": 1, "unit": "cup"}])

        meal = await create_meal(db, extraction, user_id=1, logged_at=_utcnow())

        added_meal = db.add.call_args_list[0].args[0]
        assert added_meal.visibility_level == VisibilityLevel.private


# ---------------------------------------------------------------------------
# create_symptom
# ---------------------------------------------------------------------------

class TestCreateSymptom:

    @pytest.mark.asyncio
    async def test_creates_symptom_record(self):
        from app.memory.personal_memory import create_symptom
        from app.schemas.symptom import SymptomExtraction

        db = _make_async_session()
        extraction = SymptomExtraction(symptom_name="nausea", severity=4, frequency=2)

        symptom = await create_symptom(db, extraction, user_id=1, logged_at=_utcnow())

        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert added.symptom_name == "nausea"
        assert added.severity == 4
        assert added.frequency == 2
        assert added.visibility_level == VisibilityLevel.private

    @pytest.mark.asyncio
    async def test_symptom_can_be_partner_shared(self):
        from app.memory.personal_memory import create_symptom
        from app.schemas.symptom import SymptomExtraction

        db = _make_async_session()
        extraction = SymptomExtraction(symptom_name="backache", severity=3, frequency=1)

        await create_symptom(
            db, extraction, user_id=1, logged_at=_utcnow(),
            visibility_level=VisibilityLevel.partner_shared
        )

        added = db.add.call_args.args[0]
        assert added.visibility_level == VisibilityLevel.partner_shared


# ---------------------------------------------------------------------------
# create_exercise
# ---------------------------------------------------------------------------

class TestCreateExercise:

    @pytest.mark.asyncio
    async def test_creates_exercise_record(self):
        from app.memory.personal_memory import create_exercise
        from app.schemas.exercise import ExerciseExtraction

        db = _make_async_session()
        extraction = ExerciseExtraction(activity_type="prenatal yoga", duration_minutes=30)

        await create_exercise(db, extraction, user_id=2, logged_at=_utcnow())

        added = db.add.call_args.args[0]
        assert added.activity_type == "prenatal yoga"
        assert added.duration_minutes == 30


# ---------------------------------------------------------------------------
# create_medication
# ---------------------------------------------------------------------------

class TestCreateMedication:

    @pytest.mark.asyncio
    async def test_creates_medication_record(self):
        from app.memory.personal_memory import create_medication
        from app.schemas.medication import MedicationExtraction

        db = _make_async_session()
        extraction = MedicationExtraction(medication_name="iron", dose="65mg")

        await create_medication(db, extraction, user_id=1, logged_at=_utcnow())

        added = db.add.call_args.args[0]
        assert added.medication_name == "iron"
        assert added.dose == "65mg"


# ---------------------------------------------------------------------------
# create_weight_log / create_water_log
# ---------------------------------------------------------------------------

class TestCreateBiometricLogs:

    @pytest.mark.asyncio
    async def test_creates_weight_log(self):
        from app.memory.personal_memory import create_weight_log
        from app.schemas.weight import WeightExtraction

        db = _make_async_session()
        extraction = WeightExtraction(value=68.5, unit="kg")

        await create_weight_log(db, extraction, user_id=1, logged_at=_utcnow())

        added = db.add.call_args.args[0]
        assert float(added.value) == 68.5
        assert added.unit == "kg"

    @pytest.mark.asyncio
    async def test_creates_water_log(self):
        from app.memory.personal_memory import create_water_log
        from app.schemas.water import WaterExtraction

        db = _make_async_session()
        extraction = WaterExtraction(volume=500, unit="ml")

        await create_water_log(db, extraction, user_id=1, logged_at=_utcnow())

        added = db.add.call_args.args[0]
        assert float(added.volume) == 500
        assert added.unit == "ml"


# ---------------------------------------------------------------------------
# create_doctor_question
# ---------------------------------------------------------------------------

class TestCreateDoctorQuestion:

    @pytest.mark.asyncio
    async def test_creates_question_tagged_for_visit(self):
        from app.memory.personal_memory import create_doctor_question
        from app.schemas.question import DoctorQuestionExtraction

        db = _make_async_session()
        extraction = DoctorQuestionExtraction(question_text="When does morning sickness end?")

        await create_doctor_question(db, extraction, user_id=1, logged_at=_utcnow())

        added = db.add.call_args.args[0]
        assert added.doctor_visit_tagged is True
        assert added.used_in_summary is False


# ---------------------------------------------------------------------------
# create_preference
# ---------------------------------------------------------------------------

class TestCreatePreference:

    @pytest.mark.asyncio
    async def test_creates_vegetarian_preference(self):
        from app.memory.personal_memory import create_preference
        from app.schemas.preference import PreferenceExtraction

        db = _make_async_session()
        extraction = PreferenceExtraction(preference_type="dietary", food_item="meat")

        await create_preference(db, extraction, user_id=1)

        added = db.add.call_args.args[0]
        assert added.food_item == "meat"
        assert added.active is True


# ---------------------------------------------------------------------------
# get_records: visibility enforcement
# ---------------------------------------------------------------------------

class TestGetRecords:

    @pytest.mark.asyncio
    async def test_unknown_record_type_raises_value_error(self):
        from app.memory.personal_memory import get_records

        db = AsyncMock()
        with pytest.raises(ValueError, match="Unknown record_type"):
            await get_records(
                db=db, user_id=1, record_type="unknown_type",
                start=_utcnow(), end=_utcnow(),
                requesting_user_id=1, requesting_role="mom",
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record_type", list(_RECORD_TYPE_MAP.keys()))
    async def test_all_record_types_accepted(self, record_type: str):
        from app.memory.personal_memory import get_records

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        result = await get_records(
            db=db, user_id=1, record_type=record_type,
            start=_utcnow(), end=_utcnow(),
            requesting_user_id=1, requesting_role="mom",
        )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_mom_role_no_visibility_filter_in_query(self):
        """Mom sees all her own records — no WHERE on visibility."""
        from app.memory.personal_memory import get_records
        from sqlalchemy import select
        from app.models.meal import Meal

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        await get_records(
            db=db, user_id=1, record_type="meal",
            start=_utcnow(), end=_utcnow(),
            requesting_user_id=1, requesting_role="mom",
        )

        executed_stmt = db.execute.call_args.args[0]
        # Compile the statement and check visibility not in WHERE for mom
        compiled = str(executed_stmt.compile(compile_kwargs={"literal_binds": False}))
        # Mom path should not filter by visibility_level in WHERE clause
        # (visibility_level appears in SELECT as a column, but must NOT appear in WHERE)
        lower = compiled.lower()
        if "where" in lower:
            where_part = lower.split("where", 1)[1]
            assert "visibility" not in where_part


# ---------------------------------------------------------------------------
# update_visibility
# ---------------------------------------------------------------------------

class TestUpdateVisibility:

    @pytest.mark.asyncio
    async def test_update_visibility_changes_level(self):
        from app.memory.personal_memory import update_visibility
        from app.models.meal import Meal

        mock_record = MagicMock(spec=Meal)
        mock_record.id = 1
        mock_record.user_id = 5
        mock_record.visibility_level = VisibilityLevel.private

        db = AsyncMock()
        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = mock_record
        update_result = MagicMock()
        db.execute = AsyncMock(side_effect=[select_result, update_result])

        with patch("app.memory.personal_memory.logger") as mock_logger:
            await update_visibility(db, 1, "meal", VisibilityLevel.partner_shared, user_id=5)

        mock_logger.info.assert_called()
        log_call = mock_logger.info.call_args
        assert log_call.args[0] == "visibility_updated"

    @pytest.mark.asyncio
    async def test_noop_when_same_visibility(self):
        from app.memory.personal_memory import update_visibility
        from app.models.meal import Meal

        mock_record = MagicMock(spec=Meal)
        mock_record.id = 1
        mock_record.user_id = 5
        mock_record.visibility_level = VisibilityLevel.private

        db = AsyncMock()
        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = mock_record
        db.execute = AsyncMock(return_value=select_result)

        with patch("app.memory.personal_memory.logger") as mock_logger:
            await update_visibility(db, 1, "meal", VisibilityLevel.private, user_id=5)

        # Should log noop, not updated
        log_event = mock_logger.info.call_args.args[0]
        assert "noop" in log_event.lower()

    @pytest.mark.asyncio
    async def test_unsupported_record_type_raises(self):
        from app.memory.personal_memory import update_visibility

        db = AsyncMock()
        with pytest.raises(ValueError, match="not support"):
            await update_visibility(db, 1, "preference", VisibilityLevel.partner_shared, user_id=1)

    @pytest.mark.asyncio
    async def test_record_not_found_raises_lookup_error(self):
        from app.memory.personal_memory import update_visibility

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        with pytest.raises(LookupError):
            await update_visibility(db, 999, "meal", VisibilityLevel.partner_shared, user_id=1)


# ---------------------------------------------------------------------------
# visibility_filter helper
# ---------------------------------------------------------------------------

class TestVisibilityFilter:

    def test_mom_role_returns_query_unchanged(self):
        from sqlalchemy import select
        from app.models.meal import Meal

        original = select(Meal)
        result = visibility_filter(original, UserRole.mom.value)
        assert result is original

    def test_partner_role_adds_visibility_clause(self):
        from sqlalchemy import select
        from app.models.meal import Meal

        original = select(Meal)
        result = visibility_filter(original, UserRole.partner.value)
        # The query should now differ (visibility clause added)
        assert result is not original
