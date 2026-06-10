"""ORM model for the `subscriptions` table."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SubscriptionStatus(str, enum.Enum):
    trial = "trial"
    active = "active"
    inactive = "inactive"
    grace = "grace"


# SQL-level enum object
subscription_status_enum = PG_ENUM(
    "trial", "active", "inactive", "grace",
    name="subscription_status",
    create_type=False,
)


class Subscription(Base, TimestampMixin):
    """
    Payment and subscription state for a user.

    One row per user (enforced by UNIQUE on `user_id`).
    Inherits `created_at` / `updated_at` from `TimestampMixin` since the
    design doc explicitly includes `updated_at` on this table.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        subscription_status_enum,
        nullable=False,
        server_default="trial",
    )
    payment_status: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="none"
    )
    payment_provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    provider_customer_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_sub_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    trial_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trial_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payment_retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_subscriptions_user_id"),
    )
