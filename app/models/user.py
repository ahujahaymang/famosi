"""ORM model for the `users` table."""

import enum
from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserRole(str, enum.Enum):
    mom = "mom"
    partner = "partner"
    admin = "admin"


class FoodPreference(str, enum.Enum):
    vegetarian = "vegetarian"
    vegan = "vegan"
    jain = "jain"
    eggitarian = "eggitarian"
    non_vegetarian = "non_vegetarian"


# SQL-level enum objects — reused by Alembic and other models
user_role_enum = PG_ENUM(
    "mom", "partner", "admin",
    name="user_role",
    create_type=False,
)

food_preference_enum = PG_ENUM(
    "vegetarian", "vegan", "jain", "eggitarian", "non_vegetarian",
    name="food_preference",
    create_type=False,
)


class User(Base, TimestampMixin):
    """
    Core user record.  One row per Telegram user.

    `telegram_user_id` is the primary identity for all lookups.
    `family_unit_id` is nullable — set when user joins a family unit.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    role: Mapped[UserRole] = mapped_column(
        user_role_enum,
        nullable=False,
    )
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    lmp_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    first_pregnancy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    food_preference: Mapped[Optional[FoodPreference]] = mapped_column(
        food_preference_enum,
        nullable=True,
    )
    exercise_habit: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    wake_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    sleep_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    onboarding_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    last_daily_fact_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_milestone_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_appointment_anchor: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    family_unit_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("family_units.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_users_telegram_user_id", "telegram_user_id"),
        Index("idx_users_family_unit_id", "family_unit_id"),
    )
