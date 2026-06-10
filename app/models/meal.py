"""ORM models for the `meals`, `meal_items`, and `meal_nutrients` tables."""

import enum
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class VisibilityLevel(str, enum.Enum):
    private = "private"
    partner_shared = "partner_shared"
    doctor_shared = "doctor_shared"


# SQL-level enum — shared across all models that use visibility_level
visibility_level_enum = PG_ENUM(
    "private", "partner_shared", "doctor_shared",
    name="visibility_level",
    create_type=False,
)


class Meal(Base):
    """
    Top-level meal entry.  Child rows: MealItem and MealNutrient.
    No `updated_at` per design schema.
    """

    __tablename__ = "meals"

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
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_meals_user_id_logged_at", "user_id", "logged_at"),
        Index("idx_meals_visibility", "user_id", "visibility_level"),
    )


class MealItem(Base):
    """Individual food item within a meal."""

    __tablename__ = "meal_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("meals.id", ondelete="CASCADE"),
        nullable=False,
    )
    food_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)


class MealNutrient(Base):
    """Estimated nutrient breakdown for a meal (LLM-generated)."""

    __tablename__ = "meal_nutrients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("meals.id", ondelete="CASCADE"),
        nullable=False,
    )
    protein_g: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    iron_mg: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    calcium_mg: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    folate_mcg: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    fiber_g: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)

    __table_args__ = (Index("idx_meal_nutrients_meal_id", "meal_id"),)
