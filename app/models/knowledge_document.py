"""ORM model for the `knowledge_documents` table."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeCategory(str, enum.Enum):
    nutrition = "nutrition"
    symptoms = "symptoms"
    exercise = "exercise"
    medications = "medications"
    baby_development = "baby_development"
    labor = "labor"
    postpartum = "postpartum"
    mental_health = "mental_health"
    dad_support = "dad_support"


# SQL-level enum object
knowledge_category_enum = PG_ENUM(
    "nutrition", "symptoms", "exercise", "medications",
    "baby_development", "labor", "postpartum", "mental_health", "dad_support",
    name="knowledge_category",
    create_type=False,
)


class KnowledgeDocument(Base):
    """
    A source document ingested into the knowledge base.

    `source` identifies the publisher (ACOG, WHO, CDC, NHS).
    `active` is flipped to FALSE when a document is superseded.
    No `updated_at` per design schema.
    """

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[KnowledgeCategory] = mapped_column(
        knowledge_category_enum,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
