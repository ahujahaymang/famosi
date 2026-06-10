"""ORM model for the `medications` table."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.meal import VisibilityLevel, visibility_level_enum


class Medication(Base):
    """
    Logged medication entry.
    No `updated_at` per design schema.
    """

    __tablename__ = "medications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    visibility_level: Mapped[VisibilityLevel] = mapped_column(
        visibility_level_enum,
        nullable=False,
        server_default="private",
    )
    medication_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dose: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
        Index("idx_medications_user_id_logged_at", "user_id", "logged_at"),
    )
