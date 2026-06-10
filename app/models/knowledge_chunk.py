"""ORM model for the `knowledge_chunks` table."""

from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeChunk(Base):
    """
    A 512-token chunk of a knowledge document, with its 1536-dim embedding.

    The HNSW index on `embedding` is created via `op.execute` in the Alembic
    migration because Alembic cannot autogenerate HNSW indexes.
    No `updated_at` per design schema.
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 1536 dimensions — text-embedding-3-small
    embedding: Mapped[Optional[list]] = mapped_column(Vector(1536), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Regular B-tree index on document_id
        Index("idx_knowledge_chunks_document_id", "document_id"),
        # NOTE: The HNSW index on `embedding` is created in the Alembic migration
        # via op.execute because SQLAlchemy/Alembic cannot autogenerate HNSW indexes.
    )
