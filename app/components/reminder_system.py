"""
Reminder system component for Famosi.

Provides CRUD operations for user reminders:

  - :func:`create_reminder`              — validate timezone, convert local time to UTC, persist
  - :func:`cancel_reminder`              — deactivate a reminder by id (Req 10.6)
  - :func:`reschedule_reminder`          — update scheduled_at UTC from a new local time
  - :func:`create_appointment_reminders` — auto-schedule 24h and 1h reminders (Req 10.7, 12.2)
  - :func:`list_reminders`               — return active reminders for a user
  - :func:`get_reminder`                 — load a single reminder with ownership check

Requirements: 10.1, 10.2, 10.4, 10.5, 10.6, 10.7

Privacy contract
----------------
NEVER log reminder message text or any health-identifying content.
Log only structural fields: user_id, reminder_id, reminder_type, scheduled_at_utc.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytz
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reminder import Reminder, ReminderType

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class TimezoneNotSetError(ValueError):
    """Raised when a user has no stored timezone and a reminder cannot be created."""


class ReminderNotFoundError(LookupError):
    """Raised when a reminder is not found or does not belong to the user."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _localtime_to_utc(local_dt: datetime, tz_name: str) -> datetime:
    """
    Convert a naive or tz-aware local datetime to a UTC-aware datetime.

    Parameters
    ----------
    local_dt:
        The datetime in the user's local timezone.  If already tz-aware its
        tzinfo is replaced with *tz_name* (the stored IANA timezone wins).
    tz_name:
        IANA timezone string, e.g. ``"Asia/Kolkata"`` or ``"America/New_York"``.

    Returns
    -------
    A UTC-aware :class:`datetime`.

    Raises
    ------
    pytz.UnknownTimeZoneError
        If *tz_name* is not a valid IANA timezone identifier.
    """
    tz = pytz.timezone(tz_name)
    if local_dt.tzinfo is None:
        # Treat as local time in the given timezone
        local_aware = tz.localize(local_dt)
    else:
        # Replace tzinfo with the stored IANA timezone (user timezone wins)
        local_aware = local_dt.replace(tzinfo=None)
        local_aware = tz.localize(local_aware)
    return local_aware.astimezone(pytz.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_reminder(
    user_id: int,
    reminder_type: ReminderType | str,
    local_time: datetime,
    message_text: str,
    db: AsyncSession,
    timezone_name: str | None,
) -> Reminder:
    """
    Validate the user's timezone, convert local_time to UTC, and persist a
    new :class:`~app.models.reminder.Reminder` record.

    Parameters
    ----------
    user_id:
        Internal database id of the owning user.
    reminder_type:
        A :class:`~app.models.reminder.ReminderType` value (or its string
        representation).
    local_time:
        The desired reminder time expressed in the user's local timezone.
    message_text:
        The message delivered when the reminder fires.
    db:
        Active async SQLAlchemy session.
    timezone_name:
        IANA timezone string stored on the user record (e.g. ``"Asia/Kolkata"``).
        Pass ``None`` when the user has no timezone stored — the function will
        raise :class:`TimezoneNotSetError` (Req 10.2).

    Returns
    -------
    :class:`~app.models.reminder.Reminder`
        The newly created and flushed ORM object.

    Raises
    ------
    TimezoneNotSetError
        When *timezone_name* is ``None`` or empty (Req 10.2).
    pytz.UnknownTimeZoneError
        When *timezone_name* is not a valid IANA timezone identifier.
    ValueError
        When *reminder_type* is not a valid :class:`ReminderType`.
    """
    log = logger.bind(user_id=user_id)

    # --- Req 10.2: reject if no timezone is stored ---
    if not timezone_name:
        log.info("create_reminder_rejected_no_timezone")
        raise TimezoneNotSetError(
            "No timezone is set for this user. "
            "Please set your timezone before creating a reminder."
        )

    # --- Normalise reminder_type ---
    if isinstance(reminder_type, str):
        reminder_type = ReminderType(reminder_type)

    # --- Convert local time to UTC ---
    scheduled_at_utc = _localtime_to_utc(local_time, timezone_name)

    # --- Persist the reminder ---
    reminder = Reminder(
        user_id=user_id,
        reminder_type=reminder_type,
        scheduled_at=scheduled_at_utc,
        message_text=message_text,
        active=True,
        delivered=False,
        failed=False,
    )
    db.add(reminder)
    await db.flush()
    await db.refresh(reminder)

    log.info(
        "reminder_created",
        reminder_id=reminder.id,
        reminder_type=reminder_type.value,
        scheduled_at_utc=scheduled_at_utc.isoformat(),
    )
    return reminder


async def cancel_reminder(
    reminder_id: int,
    user_id: int,
    db: AsyncSession,
) -> Reminder:
    """
    Deactivate a reminder by setting ``active = False`` (Req 10.6).

    Parameters
    ----------
    reminder_id:
        Primary key of the reminder to cancel.
    user_id:
        Must match the reminder's owner (ownership check).
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    :class:`~app.models.reminder.Reminder`
        The updated reminder ORM object.

    Raises
    ------
    ReminderNotFoundError
        When the reminder is not found or doesn't belong to the user (Req 10.6).
    """
    log = logger.bind(user_id=user_id, reminder_id=reminder_id)

    result = await db.execute(
        select(Reminder).where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
        )
    )
    reminder: Reminder | None = result.scalar_one_or_none()
    if reminder is None:
        log.warning("cancel_reminder_not_found")
        raise ReminderNotFoundError(
            f"Reminder {reminder_id} not found or does not belong to user {user_id}."
        )

    # Idempotent: already inactive
    if not reminder.active:
        log.info("cancel_reminder_already_inactive")
        return reminder

    reminder.active = False

    log.info(
        "reminder_cancelled",
        reminder_type=reminder.reminder_type.value,
        scheduled_at_utc=reminder.scheduled_at.isoformat() if reminder.scheduled_at else None,
    )
    return reminder


async def reschedule_reminder(
    reminder_id: int,
    new_local_time: datetime,
    user_id: int,
    db: AsyncSession,
    timezone_name: str | None,
) -> Reminder:
    """
    Update a reminder's ``scheduled_at`` (UTC) from a new local datetime.

    Parameters
    ----------
    reminder_id:
        Primary key of the reminder to reschedule.
    new_local_time:
        The new desired local time in the user's stored timezone.
    user_id:
        Must match the reminder's owner (ownership check).
    db:
        Active async SQLAlchemy session.
    timezone_name:
        IANA timezone string from the user record.  Raises
        :class:`TimezoneNotSetError` when absent (Req 10.2).

    Returns
    -------
    :class:`~app.models.reminder.Reminder`
        The updated reminder ORM object.

    Raises
    ------
    TimezoneNotSetError
        When *timezone_name* is ``None`` or empty.
    ReminderNotFoundError
        When the reminder is not found or doesn't belong to the user.
    pytz.UnknownTimeZoneError
        When *timezone_name* is not a valid IANA timezone identifier.
    """
    log = logger.bind(user_id=user_id, reminder_id=reminder_id)

    # --- Req 10.2: reject if no timezone is stored ---
    if not timezone_name:
        log.info("reschedule_reminder_rejected_no_timezone")
        raise TimezoneNotSetError(
            "No timezone is set for this user. "
            "Please set your timezone before rescheduling a reminder."
        )

    result = await db.execute(
        select(Reminder).where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
        )
    )
    reminder: Reminder | None = result.scalar_one_or_none()
    if reminder is None:
        log.warning("reschedule_reminder_not_found")
        raise ReminderNotFoundError(
            f"Reminder {reminder_id} not found or does not belong to user {user_id}."
        )

    new_utc = _localtime_to_utc(new_local_time, timezone_name)
    old_utc = reminder.scheduled_at

    reminder.scheduled_at = new_utc
    # Re-activate the reminder in case it was deactivated
    reminder.active = True

    log.info(
        "reminder_rescheduled",
        reminder_type=reminder.reminder_type.value,
        old_scheduled_at_utc=old_utc.isoformat() if old_utc else None,
        new_scheduled_at_utc=new_utc.isoformat(),
    )
    return reminder


async def create_appointment_reminders(
    appointment: object,
    db: AsyncSession,
) -> list[Reminder]:
    """
    Auto-schedule a 24-hour and a 1-hour reminder for an appointment (Req 10.7, 12.2).

    The appointment's ``appointment_at`` is assumed to be a UTC-aware
    :class:`datetime`.  Both reminders are persisted as
    :class:`~app.models.reminder.Reminder` rows with
    ``reminder_type = appointment`` and linked via ``appointment_id``.

    Parameters
    ----------
    appointment:
        An :class:`~app.models.appointment.Appointment` ORM object that has
        already been flushed (so ``appointment.id`` is populated).
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    list[Reminder]
        The two newly created reminder ORM objects ``[24h_reminder, 1h_reminder]``.
    """
    log = logger.bind(
        user_id=appointment.user_id,  # type: ignore[attr-defined]
        appointment_id=appointment.id,  # type: ignore[attr-defined]
    )

    appointment_at: datetime = appointment.appointment_at  # type: ignore[attr-defined]

    # Normalise to UTC-aware if necessary
    if appointment_at.tzinfo is None:
        appointment_at = appointment_at.replace(tzinfo=timezone.utc)

    remind_24h = appointment_at - timedelta(hours=24)
    remind_1h = appointment_at - timedelta(hours=1)

    reminders: list[Reminder] = []
    for scheduled_at, label in [
        (remind_24h, "24 hours"),
        (remind_1h, "1 hour"),
    ]:
        reminder = Reminder(
            user_id=appointment.user_id,  # type: ignore[attr-defined]
            reminder_type=ReminderType.appointment,
            scheduled_at=scheduled_at,
            message_text=(
                f"Reminder: your appointment is in {label}."
            ),
            appointment_id=appointment.id,  # type: ignore[attr-defined]
            active=True,
            delivered=False,
            failed=False,
        )
        db.add(reminder)
        reminders.append(reminder)

    await db.flush()
    for r in reminders:
        await db.refresh(r)

    log.info(
        "appointment_reminders_created",
        reminder_ids=[r.id for r in reminders],
        scheduled_at_utc=[r.scheduled_at.isoformat() for r in reminders],
    )
    return reminders


async def list_reminders(
    user_id: int,
    db: AsyncSession,
    active_only: bool = True,
) -> list[Reminder]:
    """
    Return reminders for the user ordered by ``scheduled_at`` ascending.

    Parameters
    ----------
    user_id:
        Internal database id of the owning user.
    db:
        Active async SQLAlchemy session.
    active_only:
        When ``True`` (default), only return reminders where ``active = True``.

    Returns
    -------
    list[Reminder]
        Matching reminders in ascending chronological order.
    """
    stmt = select(Reminder).where(Reminder.user_id == user_id)
    if active_only:
        stmt = stmt.where(Reminder.active.is_(True))
    stmt = stmt.order_by(Reminder.scheduled_at.asc())

    result = await db.execute(stmt)
    reminders = list(result.scalars().all())

    logger.bind(user_id=user_id).info(
        "list_reminders",
        count=len(reminders),
        active_only=active_only,
    )
    return reminders


async def get_reminder(
    reminder_id: int,
    user_id: int,
    db: AsyncSession,
) -> Reminder:
    """
    Load a single reminder with an ownership check.

    Parameters
    ----------
    reminder_id:
        Primary key of the reminder to load.
    user_id:
        Must match the reminder's owner.
    db:
        Active async SQLAlchemy session.

    Returns
    -------
    :class:`~app.models.reminder.Reminder`

    Raises
    ------
    ReminderNotFoundError
        When the reminder is not found or doesn't belong to the user.
    """
    result = await db.execute(
        select(Reminder).where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
        )
    )
    reminder: Reminder | None = result.scalar_one_or_none()
    if reminder is None:
        raise ReminderNotFoundError(
            f"Reminder {reminder_id} not found or does not belong to user {user_id}."
        )
    return reminder


__all__ = [
    "TimezoneNotSetError",
    "ReminderNotFoundError",
    "create_reminder",
    "cancel_reminder",
    "reschedule_reminder",
    "create_appointment_reminders",
    "list_reminders",
    "get_reminder",
]
