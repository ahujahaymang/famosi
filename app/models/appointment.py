"""ORM model for the `appointments` table."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.meal import VisibilityLevel, visibility_level_enum


class AppointmentType(str, enum.Enum):
    ob_visit = "ob_visit"
    ultrasound = "ultrasound"
    bloodwork = "bloodwork"


# SQL-level enum object
appointment_type_enum = PG_ENUM(
    "ob_visit", "ultrasound", "bloodwork",
    name="appointment_type",
    create_type=False,
)


class Appointment(Base):
    """
    A scheduled medical appointment.

    Default `visibility_level` is `partner_shared` (appointments are usually
    shared with the partner by default).
    No `updated_at` per design schema.
    """

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    visibility_level: Mapped[VisibilityLevel] = mapped_column(
        visibility_level_enum,
        nullable=False,
        server_default="partner_shared",
    )
    appointment_type: Mapped[AppointmentType] = mapped_column(
        appointment_type_enum,
        nullable=False,
    )
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_appointments_user_id_at", "user_id", "appointment_at"),
    )
