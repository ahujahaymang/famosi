"""ORM model for the `request_logs` table."""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IntentType(str, enum.Enum):
    logging = "logging"
    personal_data_query = "personal_data_query"
    knowledge_question = "knowledge_question"
    mixed_query = "mixed_query"
    unclassified = "unclassified"


# SQL-level enum object
intent_type_enum = PG_ENUM(
    "logging", "personal_data_query", "knowledge_question",
    "mixed_query", "unclassified",
    name="intent_type",
    create_type=False,
)


class RequestLog(Base):
    """
    Audit / telemetry record for every processed Telegram request.

    Written after handler completion.  `request_id` is a UUID assigned by the
    request-ID middleware before any handler runs (Req 14.6).
    No `updated_at` per design schema.
    """

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    intent: Mapped[Optional[IntentType]] = mapped_column(
        intent_type_enum,
        nullable=True,
    )
    model_used: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_rag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    rag_chunks_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rag_empty: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_request_logs_user_id", "user_id", "created_at"),
        Index("idx_request_logs_created_at", "created_at"),
    )
