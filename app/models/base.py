"""
SQLAlchemy declarative base and shared mixins.

All ORM models should inherit from `Base`.
Models that need `created_at` / `updated_at` columns should also inherit
from `TimestampMixin`.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base."""
    pass


class TimestampMixin:
    """
    Adds `created_at` and `updated_at` columns to any model.

    Both columns are timezone-aware TIMESTAMPTZ with a server-side DEFAULT of
    `now()`.  `updated_at` is refreshed on every UPDATE via `onupdate`.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
