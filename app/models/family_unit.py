"""ORM model for the `family_units` table."""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FamilyUnit(Base):
    """
    Grouping entity that links a mom and her partner.

    `users.family_unit_id` references this table.

    Invite flow
    -----------
    1. Mom runs /invite → a FamilyUnit row is created with a random 6-char
       ``invite_code`` and ``mom_user_id`` set to her DB user id.
    2. The code is valid until ``invite_used`` becomes True.
    3. During partner onboarding the partner enters the code → the bot looks
       up the FamilyUnit by ``invite_code``, sets ``invite_used = True``,
       and writes ``partner.family_unit_id = family_unit.id``.
       Mom's ``family_unit_id`` is also set to this row if not already.
    """

    __tablename__ = "family_units"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # 6-char uppercase alphanumeric invite code (unique, nullable until generated)
    invite_code: Mapped[Optional[str]] = mapped_column(String(6), nullable=True, unique=True)
    # True once a partner has consumed the code
    invite_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # FK back to the mom user row for quick lookup
    mom_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_family_units_invite_code", "invite_code", unique=True),
    )
