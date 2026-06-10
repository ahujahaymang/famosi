"""ORM model for the `water_logs` table."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.meal import VisibilityLevel, visibility_level_enum


class WaterLog(Base):
    """
    Logged water-intake entry.

    `unit` must be 'ml' or 'oz' (enforced at the application layer via Pydantic schema).
    No `updated_at` per design schema.
    """

    __tablename__ = "water_logs"

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
    volume: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(10), nullable=False)
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
