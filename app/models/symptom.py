"""ORM model for the `symptoms` table."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.meal import VisibilityLevel, visibility_level_enum


class Symptom(Base):
    """
    Logged symptom entry.

    `severity` is 1–10 and `frequency` is 1–99 (occurrences per day).
    Both are enforced via CHECK constraints.
    No `updated_at` per design schema.
    """

    __tablename__ = "symptoms"

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
    symptom_name: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    frequency: Mapped[int] = mapped_column(SmallInteger, nullable=False)
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
        CheckConstraint("severity BETWEEN 1 AND 10", name="ck_symptoms_severity"),
        CheckConstraint("frequency BETWEEN 1 AND 99", name="ck_symptoms_frequency"),
        Index("idx_symptoms_user_id_logged_at", "user_id", "logged_at"),
    )
