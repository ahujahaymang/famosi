"""ORM model for the `doctor_questions` table."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.meal import VisibilityLevel, visibility_level_enum


class DoctorQuestion(Base):
    """
    A question the user wants to raise at their next doctor visit (Req 4.7).

    `doctor_visit_tagged` defaults TRUE — all logged questions are flagged for
    the next appointment summary unless the user explicitly removes the flag.
    `used_in_summary` is flipped to TRUE when the question is included in a
    generated doctor-visit summary.
    No `updated_at` per design schema.
    """

    __tablename__ = "doctor_questions"

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
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    doctor_visit_tagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    used_in_summary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
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
        Index("idx_doctor_questions_user_id", "user_id", "doctor_visit_tagged"),
    )
