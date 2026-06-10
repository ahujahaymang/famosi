"""ORM model for the `reminders` table."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReminderType(str, enum.Enum):
    vitamin = "vitamin"
    meal = "meal"
    water = "water"
    exercise = "exercise"
    appointment = "appointment"


# SQL-level enum object
reminder_type_enum = PG_ENUM(
    "vitamin", "meal", "water", "exercise", "appointment",
    name="reminder_type",
    create_type=False,
)


class Reminder(Base):
    """
    A scheduled reminder that the delivery job dispatches every minute.

    The partial index `idx_reminders_scheduled` covers only rows where the
    reminder is still pending (active=TRUE, delivered=FALSE, failed=FALSE),
    keeping the index small and fast for the delivery query.
    No `updated_at` per design schema.
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    reminder_type: Mapped[ReminderType] = mapped_column(
        reminder_type_enum,
        nullable=False,
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    appointment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("appointments.id", ondelete="SET NULL"),
        nullable=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    failed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Partial index — only pending reminders (matches the delivery query filter)
        Index(
            "idx_reminders_scheduled",
            "scheduled_at",
            postgresql_where="active = TRUE AND delivered = FALSE AND failed = FALSE",
        ),
    )
