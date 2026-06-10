"""Personal memory — async CRUD with visibility-level enforcement.

Implements Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 19.4, 19.5, 19.6.

Design notes
------------
- Every ``create_*`` helper maps the Pydantic extraction schema to the
  matching ORM model and persists it in a single DB round-trip.
- ``logged_at`` is always taken from the caller (the confirmation timestamp),
  never from the message timestamp, so all records reflect when the user
  explicitly confirmed the entry (Req 19.4 / 19.5).
- ``visibility_filter`` enforces the partner/mom access rules at the query
  layer — never in Python — so the database does the heavy lifting and no
  filtered rows are ever transmitted over the wire (Req 6.3, 6.5).
- ``update_visibility`` writes nothing to a separate audit table; it emits a
  structured log entry via structlog instead (Req 6.4 — "log the change").
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.doctor_question import DoctorQuestion
from app.models.exercise import Exercise
from app.models.meal import Meal, MealItem, VisibilityLevel
from app.models.medication import Medication
from app.models.preference import Preference, PreferenceType
from app.models.symptom import Symptom
from app.models.water_log import WaterLog
from app.models.weight_log import WeightLog
from app.schemas.exercise import ExerciseExtraction
from app.schemas.meal import MealExtraction
from app.schemas.medication import MedicationExtraction
from app.schemas.preference import PreferenceExtraction
from app.schemas.question import DoctorQuestionExtraction
from app.schemas.symptom import SymptomExtraction
from app.schemas.water import WaterExtraction
from app.schemas.weight import WeightExtraction

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Record-type registry
# ---------------------------------------------------------------------------

# Map record_type strings to their ORM model class (used by get_records and
# update_visibility so callers don't need to import every model).
_RECORD_TYPE_MAP: dict[str, Any] = {
    "meal": Meal,
    "symptom": Symptom,
    "exercise": Exercise,
    "medication": Medication,
    "weight_log": WeightLog,
    "water_log": WaterLog,
    "doctor_question": DoctorQuestion,
    "preference": Preference,
}

# Enum-like set of record types that carry a visibility_level column.
# Preferences intentionally do NOT have a visibility column.
_VISIBILITY_BEARING_TYPES = frozenset(
    {"meal", "symptom", "exercise", "medication", "weight_log", "water_log", "doctor_question"}
)

# ---------------------------------------------------------------------------
# Role enum (mirrors the SQL user_role enum values)
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    """Role values that match the ``user_role`` PostgreSQL enum."""

    mom = "mom"
    partner = "partner"


# ---------------------------------------------------------------------------
# Visibility filter helper
# ---------------------------------------------------------------------------


def visibility_filter(query: Any, requesting_role: str) -> Any:
    """Apply a visibility predicate to *query* based on the requester's role.

    Rules (Req 6.3, 6.5):
    - ``partner`` role → restrict to ``partner_shared`` or ``doctor_shared``
      records only.  Private records are never returned.
    - ``mom`` role    → all own records are returned (no additional filter
      beyond the ``user_id`` clause the caller already applied).

    Parameters
    ----------
    query:
        A SQLAlchemy ``Select`` statement that already filters on ``user_id``.
    requesting_role:
        The string role of the requesting user (``"mom"`` or ``"partner"``).

    Returns
    -------
    The query with visibility predicate applied when the role is ``partner``;
    the original query unchanged when the role is ``mom``.
    """
    if requesting_role == UserRole.partner:
        # Partner may only see records explicitly shared with them.
        query = query.where(
            Meal.visibility_level.in_(  # type: ignore[attr-defined]
                [VisibilityLevel.partner_shared, VisibilityLevel.doctor_shared]
            )
        )
    # mom role: no additional filter — all own records are accessible.
    return query


def _visibility_filter_for_model(
    query: Any,
    model: Any,
    requesting_role: str,
) -> Any:
    """Apply visibility filtering against *model*'s ``visibility_level`` column.

    This is a model-aware variant of ``visibility_filter`` used internally by
    ``get_records`` so the correct column reference is used for each model.
    """
    if requesting_role == UserRole.partner:
        query = query.where(
            model.visibility_level.in_(
                [VisibilityLevel.partner_shared, VisibilityLevel.doctor_shared]
            )
        )
    return query


# ---------------------------------------------------------------------------
# create_* helpers
# ---------------------------------------------------------------------------


async def create_meal(
    db: AsyncSession,
    extraction: MealExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
    raw_text: str | None = None,
) -> Meal:
    """Persist a confirmed meal and its constituent items.

    A ``MealNutrient`` row is intentionally *not* created here — that is the
    responsibility of the ``Nutrition_Assistant`` (Req 7.1).

    Parameters
    ----------
    db:
        Active async SQLAlchemy session.
    extraction:
        Validated ``MealExtraction`` from the extraction pipeline.
    user_id:
        Internal DB id of the owning user.
    logged_at:
        Timestamp of the user's confirmation (UTC).  Used as ``confirmed_at``
        and also stored as ``logged_at`` so callers control the anchor time.
    visibility_level:
        Defaults to ``private`` (Req 6.2).
    raw_text:
        The original user message, stored for audit/debugging purposes.

    Returns
    -------
    The persisted ``Meal`` ORM object (id populated after flush).
    """
    meal = Meal(
        user_id=user_id,
        visibility_level=visibility_level,
        logged_at=logged_at,
        confirmed_at=logged_at,
        raw_text=raw_text,
    )
    db.add(meal)
    # Flush to obtain meal.id before creating child rows.
    await db.flush()

    for item in extraction.items:
        db.add(
            MealItem(
                meal_id=meal.id,
                food_name=item.food_name,
                quantity=item.quantity,
                unit=item.unit,
            )
        )

    await db.flush()
    logger.info(
        "meal_created",
        user_id=user_id,
        meal_id=meal.id,
        item_count=len(extraction.items),
        visibility_level=visibility_level.value,
    )
    return meal


async def create_symptom(
    db: AsyncSession,
    extraction: SymptomExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
) -> Symptom:
    """Persist a confirmed symptom record.

    Parameters
    ----------
    db:
        Active async SQLAlchemy session.
    extraction:
        Validated ``SymptomExtraction``.
    user_id:
        Internal DB id of the owning user.
    logged_at:
        Confirmation timestamp (UTC).
    visibility_level:
        Defaults to ``private`` (Req 6.2).
    """
    symptom = Symptom(
        user_id=user_id,
        visibility_level=visibility_level,
        symptom_name=extraction.symptom_name,
        severity=extraction.severity,
        frequency=extraction.frequency,
        logged_at=logged_at,
        confirmed_at=logged_at,
    )
    db.add(symptom)
    await db.flush()
    logger.info(
        "symptom_created",
        user_id=user_id,
        symptom_id=symptom.id,
        severity=extraction.severity,
        visibility_level=visibility_level.value,
    )
    return symptom


async def create_exercise(
    db: AsyncSession,
    extraction: ExerciseExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
) -> Exercise:
    """Persist a confirmed exercise record."""
    exercise = Exercise(
        user_id=user_id,
        visibility_level=visibility_level,
        activity_type=extraction.activity_type,
        duration_minutes=extraction.duration_minutes,
        logged_at=logged_at,
        confirmed_at=logged_at,
    )
    db.add(exercise)
    await db.flush()
    logger.info(
        "exercise_created",
        user_id=user_id,
        exercise_id=exercise.id,
        visibility_level=visibility_level.value,
    )
    return exercise


async def create_medication(
    db: AsyncSession,
    extraction: MedicationExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
) -> Medication:
    """Persist a confirmed medication record."""
    medication = Medication(
        user_id=user_id,
        visibility_level=visibility_level,
        medication_name=extraction.medication_name,
        dose=extraction.dose,
        logged_at=logged_at,
        confirmed_at=logged_at,
    )
    db.add(medication)
    await db.flush()
    logger.info(
        "medication_created",
        user_id=user_id,
        medication_id=medication.id,
        visibility_level=visibility_level.value,
    )
    return medication


async def create_weight_log(
    db: AsyncSession,
    extraction: WeightExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
) -> WeightLog:
    """Persist a confirmed weight measurement."""
    weight_log = WeightLog(
        user_id=user_id,
        visibility_level=visibility_level,
        value=extraction.value,
        unit=extraction.unit,
        logged_at=logged_at,
        confirmed_at=logged_at,
    )
    db.add(weight_log)
    await db.flush()
    logger.info(
        "weight_log_created",
        user_id=user_id,
        weight_log_id=weight_log.id,
        visibility_level=visibility_level.value,
    )
    return weight_log


async def create_water_log(
    db: AsyncSession,
    extraction: WaterExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
) -> WaterLog:
    """Persist a confirmed water-intake entry."""
    water_log = WaterLog(
        user_id=user_id,
        visibility_level=visibility_level,
        volume=extraction.volume,
        unit=extraction.unit,
        logged_at=logged_at,
        confirmed_at=logged_at,
    )
    db.add(water_log)
    await db.flush()
    logger.info(
        "water_log_created",
        user_id=user_id,
        water_log_id=water_log.id,
        visibility_level=visibility_level.value,
    )
    return water_log


async def create_doctor_question(
    db: AsyncSession,
    extraction: DoctorQuestionExtraction,
    user_id: int,
    logged_at: datetime,
    visibility_level: VisibilityLevel = VisibilityLevel.private,
    doctor_visit_tagged: bool = True,
) -> DoctorQuestion:
    """Persist a confirmed doctor question.

    Questions are tagged with ``doctor_visit_tagged=True`` by default so that
    the ``Doctor_Visit_Assistant`` picks them up automatically (Req 4.7).
    """
    question = DoctorQuestion(
        user_id=user_id,
        visibility_level=visibility_level,
        question_text=extraction.question_text,
        doctor_visit_tagged=doctor_visit_tagged,
        used_in_summary=False,
        logged_at=logged_at,
        confirmed_at=logged_at,
    )
    db.add(question)
    await db.flush()
    logger.info(
        "doctor_question_created",
        user_id=user_id,
        question_id=question.id,
        doctor_visit_tagged=doctor_visit_tagged,
        visibility_level=visibility_level.value,
    )
    return question


async def create_preference(
    db: AsyncSession,
    extraction: PreferenceExtraction,
    user_id: int,
    confirmed_at: datetime | None = None,
) -> Preference:
    """Persist a confirmed food preference.

    Preferences do **not** carry a ``visibility_level`` column — they are
    always considered private to the owning user.

    If a row with the same ``(user_id, preference_type, food_item)`` already
    exists and is active, the caller is responsible for deactivating it before
    calling this method (Req 5.5 — conflict resolution lives in
    ``preference_engine.py``).

    Parameters
    ----------
    confirmed_at:
        Timestamp of user confirmation.  Defaults to ``datetime.now(UTC)``
        if ``None``.
    """
    ts = confirmed_at or datetime.now(timezone.utc)
    preference = Preference(
        user_id=user_id,
        preference_type=PreferenceType(extraction.preference_type),
        food_item=extraction.food_item,
        active=True,
        confirmed_at=ts,
    )
    db.add(preference)
    await db.flush()
    logger.info(
        "preference_created",
        user_id=user_id,
        preference_id=preference.id,
        preference_type=extraction.preference_type,
    )
    return preference


# ---------------------------------------------------------------------------
# Generic read helper
# ---------------------------------------------------------------------------


async def get_records(
    db: AsyncSession,
    user_id: int,
    record_type: str,
    start: datetime,
    end: datetime,
    requesting_user_id: int,
    requesting_role: str,
) -> list[Any]:
    """Fetch records of *record_type* within [start, end] with visibility enforcement.

    Visibility is applied at the query layer (Req 6.3, 6.5, 19.6):
    - A ``partner`` requester only receives ``partner_shared`` or
      ``doctor_shared`` records.
    - A ``mom`` requester (typically ``requesting_user_id == user_id``)
      receives all their own records regardless of visibility level.

    Parameters
    ----------
    db:
        Active async SQLAlchemy session.
    user_id:
        The user whose records are being fetched.
    record_type:
        One of the keys in ``_RECORD_TYPE_MAP``: ``"meal"``, ``"symptom"``,
        ``"exercise"``, ``"medication"``, ``"weight_log"``, ``"water_log"``,
        ``"doctor_question"``, ``"preference"``.
    start:
        Inclusive start of the time window (UTC).
    end:
        Inclusive end of the time window (UTC).
    requesting_user_id:
        The user making the request (may differ from *user_id* for partner
        queries).
    requesting_role:
        ``"mom"`` or ``"partner"``.

    Returns
    -------
    A list of ORM objects matching the query.

    Raises
    ------
    ValueError
        If *record_type* is not a recognised key.
    """
    model = _RECORD_TYPE_MAP.get(record_type)
    if model is None:
        raise ValueError(
            f"Unknown record_type '{record_type}'. "
            f"Valid values: {sorted(_RECORD_TYPE_MAP)}"
        )

    # Preferences have no logged_at or visibility_level column; handle separately.
    if record_type == "preference":
        stmt = (
            select(model)
            .where(model.user_id == user_id)
            .where(model.active.is_(True))
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # All other record types have logged_at and visibility_level.
    stmt = (
        select(model)
        .where(model.user_id == user_id)
        .where(model.logged_at >= start)
        .where(model.logged_at <= end)
    )

    # Apply visibility enforcement only for records that carry the column.
    if record_type in _VISIBILITY_BEARING_TYPES:
        stmt = _visibility_filter_for_model(stmt, model, requesting_role)

    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    logger.debug(
        "get_records",
        user_id=user_id,
        record_type=record_type,
        requesting_role=requesting_role,
        count=len(rows),
    )
    return rows


# ---------------------------------------------------------------------------
# Visibility update
# ---------------------------------------------------------------------------


async def update_visibility(
    db: AsyncSession,
    record_id: int,
    record_type: str,
    new_visibility: VisibilityLevel,
    user_id: int,
) -> None:
    """Update the ``visibility_level`` of a single record and log the change.

    The audit trail is a structured log entry (not a DB table) containing
    ``user_id``, ``record_id``, ``record_type``, ``previous_visibility``,
    ``new_visibility``, and ``timestamp`` (Req 6.4).

    Parameters
    ----------
    db:
        Active async SQLAlchemy session.
    record_id:
        Primary key of the record to update.
    record_type:
        String key identifying the model (must be a visibility-bearing type).
    new_visibility:
        The ``VisibilityLevel`` value to apply.
    user_id:
        The user requesting the change (must own the record).

    Raises
    ------
    ValueError
        If *record_type* is not a visibility-bearing type or is not recognised.
    LookupError
        If no matching record is found for (record_id, user_id).
    """
    if record_type not in _VISIBILITY_BEARING_TYPES:
        raise ValueError(
            f"Record type '{record_type}' does not support visibility levels. "
            f"Supported types: {sorted(_VISIBILITY_BEARING_TYPES)}"
        )

    model = _RECORD_TYPE_MAP.get(record_type)
    if model is None:
        raise ValueError(
            f"Unknown record_type '{record_type}'. "
            f"Valid values: {sorted(_RECORD_TYPE_MAP)}"
        )

    # Read the current row so we can log the previous visibility value.
    result = await db.execute(
        select(model).where(model.id == record_id).where(model.user_id == user_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise LookupError(
            f"No {record_type} record with id={record_id} found for user_id={user_id}."
        )

    previous_visibility: VisibilityLevel = record.visibility_level

    # Skip the write when the value hasn't changed.
    if previous_visibility == new_visibility:
        logger.info(
            "update_visibility_noop",
            user_id=user_id,
            record_id=record_id,
            record_type=record_type,
            visibility_level=new_visibility.value,
        )
        return

    # Apply the update.
    await db.execute(
        update(model)
        .where(model.id == record_id)
        .where(model.user_id == user_id)
        .values(visibility_level=new_visibility)
    )
    await db.flush()

    # Emit the structured audit log (Req 6.4, 19.5).
    logger.info(
        "visibility_updated",
        user_id=user_id,
        record_id=record_id,
        record_type=record_type,
        previous_visibility=previous_visibility.value,
        new_visibility=new_visibility.value,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
