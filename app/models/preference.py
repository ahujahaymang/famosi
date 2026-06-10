"""ORM model for the `preferences` table."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PreferenceType(str, enum.Enum):
    like = "like"
    dislike = "dislike"
    allergy = "allergy"
    dietary = "dietary"


# SQL-level enum object
preference_type_enum = PG_ENUM(
    "like", "dislike", "allergy", "dietary",
    name="preference_type",
    create_type=False,
)


class Preference(Base):
    """
    A user's food preference, allergy, or dietary restriction.

    The UNIQUE constraint on `(user_id, preference_type, food_item)` ensures
    only one active row per combination.  When a preference is overwritten,
    the old row has `active` set to FALSE and a new row is inserted.
    No `updated_at` per design schema.
    """

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    preference_type: Mapped[PreferenceType] = mapped_column(
        preference_type_enum,
        nullable=False,
    )
    food_item: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
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
        UniqueConstraint("user_id", "preference_type", "food_item", name="uq_preferences_user_type_item"),
        Index("idx_preferences_user_id", "user_id", "active"),
    )
