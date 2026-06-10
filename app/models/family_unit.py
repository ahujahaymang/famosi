"""ORM model for the `family_units` table."""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FamilyUnit(Base):
    """
    Grouping entity that links a mom and her partner.

    `users.family_unit_id` references this table.
    The design schema defines only `id` and `created_at` — no `updated_at`.
    """

    __tablename__ = "family_units"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
