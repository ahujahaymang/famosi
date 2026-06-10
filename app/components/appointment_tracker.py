"""
Appointment tracker component for Famosi.

Provides CRUD operations for medical appointments:

  - :func:`create_appointment` — validate, persist, and auto-schedule reminders
  - :func:`list_upcoming`      — return future non-cancelled appointments in
                                 ascending chronological order (Req 12.3)
  - :func:`cancel_appointment` — mark cancelled and deactivate reminders (Req 12.4)
  - :func:`reschedule_appointment` — update date/time and recreate reminders
                                     (Req 12.5)

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6

Privacy contract
----------------
NEVER log appointment location, notes, or any health-identifying content.
Log only structural fields: user_id, appointment_id, appointment_type.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment, AppointmentType
from app.models.reminder import Reminder

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy import helper for reminder_system (task 25 — may not exist yet)
# ---------------------------------------------------------------------------


def _get_reminder_system():
    """
    Lazily import reminder_system to gracefully handle the case where the
    module hasn't been written yet (task 25 comes after task 24).

    Returns the reminder_system module, or None if it is not yet available.
    """
    try:
        from app.components import reminder_system  # noqa: PLC0415
        return reminder_system
    except ImportError:
        logger.warning("reminder_system_not_yet_available")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_appointment(
    user_id: int,
    appointment_type: AppointmentType | str,
    appointment_at: datetime,
    location: str | None,
    notes: str | None,
    db: AsyncSession,
) -> Appointment:
    """
    Create and persist a new appointment record.

    Validates that ``appointment_at`` is in the future (UTC).  If it is in
    the past a :class:`ValueError` is raised so the caller can re-prompt the
    user (Req 12.6).

    After persisting the appointment, auto-schedules a 24-hour and 1-hour
    reminder by calling ``reminder_system.create_appointment_reminders`` if
    that module is available (Req 12.2).

    Parameters
    ----------
    user_id:
        Internal database id of the owning user.
    appointment_type:
        An :class:`~app.models.appointment.AppointmentType` value (or its
        string representation).
    appointment_at:
        Timezone-aware UTC datetime for the appointment.
    location:
        Optional location string (max 200 chars).
    notes:
        Optional free-text notes (max 1000 chars).
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    :class:`~app.models.appointment.Appointment`
        The newly created and persisted ORM object.

    Raises
    ------
    ValueError
        When ``appointment_at`` is not in the future (Req 12.6).
    """
    log = logger.bind(user_id=user_id)

    # --- Validate appointment is in the future (Req 12.6) ---
    now_utc = datetime.now(timezone.utc)

    # Normalise to timezone-aware UTC if necessary
    if appointment_at.tzinfo is None:
        appointment_at = appointment_at.replace(tzinfo=timezone.utc)

    if appointment_at <= now_utc:
        log.info("create_appointment_rejected_past_datetime")
        raise ValueError(
            "Appointment date/time must be in the future. "
            f"Provided: {appointment_at.isoformat()}, "
            f"Current UTC: {now_utc.isoformat()}"
        )

    # --- Normalise appointment_type ---
    if isinstance(appointment_type, str):
        appointment_type = AppointmentType(appointment_type)

    # --- Persist the appointment ---
    appointment = Appointment(
        user_id=user_id,
        appointment_type=appointment_type,
        appointment_at=appointment_at,
        location=location,
        notes=notes,
        cancelled=False,
    )
    db.add(appointment)
    await db.flush()  # populate appointment.id before calling reminder_system
    await db.refresh(appointment)

    log.info(
        "appointment_created",
        appointment_id=appointment.id,
        appointment_type=appointment_type.value,
    )

    # --- Auto-schedule 24h and 1h reminders (Req 12.2) ---
    reminder_system = _get_reminder_system()
    if reminder_system is not None:
        try:
            await reminder_system.create_appointment_reminders(appointment, db)
            log.info(
                "appointment_reminders_scheduled",
                appointment_id=appointment.id,
            )
        except Exception:  # noqa: BLE001
            # Reminder scheduling is best-effort; don't fail the appointment
            # creation if reminders can't be set.
            log.exception(
                "appointment_reminder_scheduling_failed",
                appointment_id=appointment.id,
            )

    return appointment


async def list_upcoming(
    user_id: int,
    db: AsyncSession,
) -> list[Appointment]:
    """
    Return all future, non-cancelled appointments for the user in ascending
    chronological order (Req 12.3).

    Parameters
    ----------
    user_id:
        Internal database id of the owning user.
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    list[Appointment]
        Appointments where ``appointment_at > now()`` and ``cancelled = False``,
        ordered by ``appointment_at ASC``.
    """
    now_utc = datetime.now(timezone.utc)

    result = await db.execute(
        select(Appointment)
        .where(
            Appointment.user_id == user_id,
            Appointment.appointment_at > now_utc,
            Appointment.cancelled.is_(False),
        )
        .order_by(Appointment.appointment_at.asc())
    )
    appointments = list(result.scalars().all())

    logger.bind(user_id=user_id).info(
        "list_upcoming_appointments",
        count=len(appointments),
    )
    return appointments


async def cancel_appointment(
    appointment_id: int,
    user_id: int,
    db: AsyncSession,
) -> Appointment:
    """
    Cancel an appointment and deactivate all associated active reminders
    (Req 12.4).

    Sets ``Appointment.cancelled = True`` and ``Reminder.active = False``
    for all reminders linked to this appointment.

    Parameters
    ----------
    appointment_id:
        Primary key of the appointment to cancel.
    user_id:
        Must match the appointment's owner (ownership check).
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    :class:`~app.models.appointment.Appointment`
        The updated appointment ORM object.

    Raises
    ------
    ValueError
        When the appointment is not found or doesn't belong to the user.
    """
    log = logger.bind(user_id=user_id, appointment_id=appointment_id)

    # --- Load and authorise ---
    result = await db.execute(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.user_id == user_id,
        )
    )
    appointment: Appointment | None = result.scalar_one_or_none()
    if appointment is None:
        log.warning("cancel_appointment_not_found")
        raise ValueError(
            f"Appointment {appointment_id} not found or does not belong to user {user_id}."
        )

    # Idempotent: already cancelled
    if appointment.cancelled:
        log.info("cancel_appointment_already_cancelled")
        return appointment

    # --- Mark as cancelled ---
    appointment.cancelled = True

    # --- Deactivate associated reminders ---
    reminders_result = await db.execute(
        select(Reminder).where(
            Reminder.appointment_id == appointment_id,
            Reminder.active.is_(True),
        )
    )
    reminders = list(reminders_result.scalars().all())

    for reminder in reminders:
        reminder.active = False

    log.info(
        "appointment_cancelled",
        reminders_deactivated=len(reminders),
    )

    return appointment


async def reschedule_appointment(
    appointment_id: int,
    new_dt: datetime,
    user_id: int,
    db: AsyncSession,
) -> Appointment:
    """
    Reschedule an appointment to a new date/time and recreate reminders
    (Req 12.5).

    Workflow:
      1. Validate ``new_dt`` is in the future.
      2. Load and authorise the appointment.
      3. Update ``appointment_at``.
      4. Cancel all existing active reminders for this appointment.
      5. Call ``reminder_system.create_appointment_reminders`` to schedule new
         24h and 1h reminders.

    Parameters
    ----------
    appointment_id:
        Primary key of the appointment to reschedule.
    new_dt:
        New timezone-aware UTC datetime.
    user_id:
        Must match the appointment's owner (ownership check).
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    :class:`~app.models.appointment.Appointment`
        The updated appointment ORM object.

    Raises
    ------
    ValueError
        When ``new_dt`` is not in the future, or the appointment is not found /
        doesn't belong to the user.
    """
    log = logger.bind(user_id=user_id, appointment_id=appointment_id)

    # --- Validate new datetime is in the future ---
    now_utc = datetime.now(timezone.utc)

    if new_dt.tzinfo is None:
        new_dt = new_dt.replace(tzinfo=timezone.utc)

    if new_dt <= now_utc:
        log.info("reschedule_appointment_rejected_past_datetime")
        raise ValueError(
            "New appointment date/time must be in the future. "
            f"Provided: {new_dt.isoformat()}, "
            f"Current UTC: {now_utc.isoformat()}"
        )

    # --- Load and authorise ---
    result = await db.execute(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.user_id == user_id,
        )
    )
    appointment: Appointment | None = result.scalar_one_or_none()
    if appointment is None:
        log.warning("reschedule_appointment_not_found")
        raise ValueError(
            f"Appointment {appointment_id} not found or does not belong to user {user_id}."
        )

    if appointment.cancelled:
        log.warning("reschedule_cancelled_appointment")
        raise ValueError(
            f"Cannot reschedule a cancelled appointment (id={appointment_id})."
        )

    # --- Update appointment_at ---
    old_dt = appointment.appointment_at
    appointment.appointment_at = new_dt

    # --- Cancel existing active reminders ---
    reminders_result = await db.execute(
        select(Reminder).where(
            Reminder.appointment_id == appointment_id,
            Reminder.active.is_(True),
        )
    )
    existing_reminders = list(reminders_result.scalars().all())
    for reminder in existing_reminders:
        reminder.active = False

    await db.flush()
    await db.refresh(appointment)

    log.info(
        "appointment_rescheduled",
        old_dt=old_dt.isoformat() if old_dt else None,
        new_dt=new_dt.isoformat(),
        reminders_cancelled=len(existing_reminders),
    )

    # --- Recreate reminders for the new datetime ---
    reminder_system = _get_reminder_system()
    if reminder_system is not None:
        try:
            await reminder_system.create_appointment_reminders(appointment, db)
            log.info(
                "appointment_reminders_rescheduled",
                appointment_id=appointment.id,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "appointment_reminder_rescheduling_failed",
                appointment_id=appointment.id,
            )

    return appointment


__all__ = [
    "create_appointment",
    "list_upcoming",
    "cancel_appointment",
    "reschedule_appointment",
]
