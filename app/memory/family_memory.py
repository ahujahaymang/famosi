"""
Family-scoped memory queries.

Provides read access to shared health records and appointments for all members
of a family unit.  All queries apply partner-level visibility filtering so that
only `partner_shared` and `doctor_shared` records are returned — `private`
records are never exposed through this module.

Requirements: 6.3, 18.3
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.doctor_question import DoctorQuestion
from app.models.exercise import Exercise
from app.models.meal import Meal, VisibilityLevel
from app.models.medication import Medication
from app.models.preference import Preference
from app.models.symptom import Symptom
from app.models.user import User
from app.models.water_log import WaterLog
from app.models.weight_log import WeightLog

# Visibility levels that are permitted for partner-role queries (Req 6.3)
_PARTNER_VISIBLE: tuple[str, ...] = (
    VisibilityLevel.partner_shared.value,
    VisibilityLevel.doctor_shared.value,
)

# Map of record_type string → ORM model class.
# Matches the same record types supported by personal_memory.
_RECORD_TYPE_MAP: dict[str, type[Any]] = {
    "meal": Meal,
    "symptom": Symptom,
    "exercise": Exercise,
    "medication": Medication,
    "weight_log": WeightLog,
    "water_log": WaterLog,
    "doctor_question": DoctorQuestion,
    "preference": Preference,
}


def _partner_visibility_filter(model: type[Any], query: Any) -> Any:
    """
    Restrict a SQLAlchemy select query to rows that are visible at the
    partner role level (partner_shared or doctor_shared).

    Args:
        model:  The ORM model class being queried.  Must have a
                `visibility_level` column.
        query:  An existing SQLAlchemy ``Select`` statement to augment.

    Returns:
        The query with an additional WHERE clause applied.
    """
    return query.where(
        model.visibility_level.in_(_PARTNER_VISIBLE)
    )


async def _get_family_user_ids(
    db: AsyncSession,
    family_unit_id: int,
) -> list[int]:
    """
    Return the internal user IDs for every member of the family unit.

    Args:
        db:             Async database session.
        family_unit_id: Primary key of the `family_units` row.

    Returns:
        List of `users.id` values belonging to the family unit.
        Returns an empty list when no users are linked to the unit.
    """
    result = await db.execute(
        select(User.id).where(User.family_unit_id == family_unit_id)
    )
    return list(result.scalars().all())


async def get_shared_records(
    db: AsyncSession,
    family_unit_id: int,
    record_type: str,
    start: datetime,
    end: datetime,
) -> Sequence[Any]:
    """
    Retrieve all health records of a given type for every member of a family
    unit, filtered to partner-visible entries only (Req 6.3).

    The function joins through the `users` table to collect all user IDs that
    belong to the family unit and then queries the target record table,
    restricting results to rows with ``visibility_level`` of
    ``partner_shared`` or ``doctor_shared``.

    Args:
        db:             Async database session.
        family_unit_id: Primary key of the family unit whose records to fetch.
        record_type:    One of ``meal``, ``symptom``, ``exercise``,
                        ``medication``, ``weight_log``, ``water_log``,
                        ``doctor_question``, or ``preference``.
        start:          Inclusive start of the ``logged_at`` (or
                        ``confirmed_at`` for types without ``logged_at``)
                        date-range filter.  Timezone-aware datetime expected.
        end:            Inclusive end of the date-range filter.

    Returns:
        Sequence of ORM model instances matching the query, ordered by
        ``logged_at`` ascending (or ``confirmed_at`` when the model lacks a
        ``logged_at`` column).

    Raises:
        ValueError: When ``record_type`` is not one of the supported values.
    """
    model = _RECORD_TYPE_MAP.get(record_type)
    if model is None:
        supported = ", ".join(sorted(_RECORD_TYPE_MAP.keys()))
        raise ValueError(
            f"Unsupported record_type '{record_type}'. "
            f"Supported values: {supported}"
        )

    # Collect all user IDs in the family unit first.
    user_ids = await _get_family_user_ids(db, family_unit_id)
    if not user_ids:
        return []

    # Build the base query filtered by user IDs and date range.
    # Use `logged_at` when available; fall back to `confirmed_at` for models
    # that do not expose a `logged_at` column (currently all supported models
    # have `logged_at`, but the guard keeps this future-proof).
    timestamp_col = getattr(model, "logged_at", None) or model.confirmed_at  # type: ignore[attr-defined]

    query = (
        select(model)
        .where(model.user_id.in_(user_ids))  # type: ignore[attr-defined]
        .where(timestamp_col >= start)
        .where(timestamp_col <= end)
        .order_by(timestamp_col.asc())
    )

    # Apply partner-level visibility filter (Req 6.3).
    query = _partner_visibility_filter(model, query)

    result = await db.execute(query)
    return result.scalars().all()


async def get_shared_appointments(
    db: AsyncSession,
    family_unit_id: int,
) -> Sequence[Appointment]:
    """
    Retrieve all non-cancelled appointments for every member of a family unit
    where the appointment is partner- or doctor-visible (Req 18.3).

    Appointments default to ``partner_shared`` visibility, so most upcoming
    appointments will be returned unless the user has explicitly set them to
    ``private``.

    Args:
        db:             Async database session.
        family_unit_id: Primary key of the family unit whose appointments to
                        fetch.

    Returns:
        Sequence of :class:`~app.models.appointment.Appointment` instances,
        ordered by ``appointment_at`` ascending (soonest first).
    """
    user_ids = await _get_family_user_ids(db, family_unit_id)
    if not user_ids:
        return []

    query = (
        select(Appointment)
        .where(Appointment.user_id.in_(user_ids))
        .where(
            Appointment.visibility_level.in_(_PARTNER_VISIBLE)
        )
        .where(Appointment.cancelled.is_(False))
        .order_by(Appointment.appointment_at.asc())
    )

    result = await db.execute(query)
    return result.scalars().all()
