"""
Knowledge ingestion pipeline.

``ingest_document`` is the single entry point for adding a source document
into the knowledge base.  It is designed to be invoked:
  - Manually by an admin via a CLI command or management endpoint.
  - Automatically by a weekly AWS EventBridge job.

Workflow (Req 15.2)
-------------------
1. Chunk ``raw_text`` into 512-token windows with 50-token overlap via
   ``chunker.chunk_text()``.
2. Embed each chunk via ``retriever.embed()``.
3. Upsert a ``KnowledgeDocument`` record (identified by ``source`` + ``title``
   + ``source_url``).  If the document already exists, its ``ingested_at``
   timestamp and ``active`` flag are refreshed.
4. Upsert ``KnowledgeChunk`` records for each chunk window.  Chunks are
   matched by ``(document_id, chunk_index)``; existing rows are updated with
   fresh content and embeddings.

Requirements: 15.2
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.ingestion.chunker import chunk_text
from app.knowledge.retriever import embed
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeCategory, KnowledgeDocument

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient

logger = structlog.get_logger(__name__)

# tiktoken encoding used — kept in sync with chunker.py and retriever.py
_MAX_TOKENS = 512
_OVERLAP = 50


async def ingest_document(
    source: str,
    category: KnowledgeCategory,
    title: str,
    url: str | None,
    raw_text: str,
    db: AsyncSession,
    llm_client: "LLMClient",
) -> tuple[KnowledgeDocument, list[KnowledgeChunk]]:
    """
    Chunk, embed, and upsert a source document into the knowledge base.

    Parameters
    ----------
    source:
        Short publisher identifier, e.g. ``"ACOG"``, ``"WHO"``, ``"CDC"``,
        ``"NHS"``.  Stored as ``knowledge_documents.source`` (VARCHAR(20)).
    category:
        ``KnowledgeCategory`` enum value for the document.
    title:
        Human-readable document title.
    url:
        Source URL; stored in ``knowledge_documents.source_url``.  May be
        ``None`` for documents without a canonical URL.
    raw_text:
        Full plain-text content of the document to chunk and embed.
    db:
        Async SQLAlchemy session.  The caller is responsible for committing
        the transaction.
    llm_client:
        Initialised ``LLMClient`` used for embedding (OpenAI embeddings API).

    Returns
    -------
    ``(document, chunks)`` — the upserted ``KnowledgeDocument`` ORM object and
    the list of upserted ``KnowledgeChunk`` ORM objects.

    Raises
    ------
    openai.OpenAIError:
        If any embedding call fails.
    sqlalchemy.exc.SQLAlchemyError:
        On database errors.
    """
    log = logger.bind(source=source, category=category.value, title=title)

    # ------------------------------------------------------------------
    # Step 1 — chunk the raw text
    # ------------------------------------------------------------------
    text_chunks: list[str] = chunk_text(raw_text, max_tokens=_MAX_TOKENS, overlap=_OVERLAP)
    log.info("ingest_document_chunked", chunk_count=len(text_chunks))

    if not text_chunks:
        log.warning("ingest_document_no_chunks", url=url)
        text_chunks = []

    # ------------------------------------------------------------------
    # Step 2 — embed each chunk (one API call per chunk)
    # ------------------------------------------------------------------
    embeddings: list[list[float]] = []
    for i, chunk in enumerate(text_chunks):
        vector = await embed(chunk, llm_client)
        embeddings.append(vector)
        if (i + 1) % 10 == 0:
            log.debug("ingest_document_embedding_progress", done=i + 1, total=len(text_chunks))

    log.info("ingest_document_embeddings_ready", count=len(embeddings))

    # ------------------------------------------------------------------
    # Step 3 — upsert KnowledgeDocument
    # ------------------------------------------------------------------
    # Identify an existing document by (source, title, source_url).
    # If one exists, update ingested_at and ensure active=True.
    # If none exists, insert a new row.
    existing_doc_stmt = select(KnowledgeDocument).where(
        KnowledgeDocument.source == source,
        KnowledgeDocument.title == title,
        KnowledgeDocument.source_url == url,
    )
    existing_result = await db.execute(existing_doc_stmt)
    document: KnowledgeDocument | None = existing_result.scalar_one_or_none()

    if document is None:
        document = KnowledgeDocument(
            source=source,
            category=category,
            title=title,
            source_url=url,
            active=True,
        )
        db.add(document)
        await db.flush()  # populate document.id before inserting chunks
        log.info("ingest_document_new_document", document_id=document.id)
    else:
        # Refresh metadata — re-run the import, category may have been corrected
        document.category = category
        document.active = True
        db.add(document)
        await db.flush()
        log.info("ingest_document_existing_document", document_id=document.id)

    # ------------------------------------------------------------------
    # Step 4 — upsert KnowledgeChunk records
    # ------------------------------------------------------------------
    # Use PostgreSQL ON CONFLICT (document_id, chunk_index) DO UPDATE so
    # re-ingestion refreshes content and embeddings without duplicates.
    # SQLAlchemy's ``pg_insert`` supports this natively.
    upserted_chunks: list[KnowledgeChunk] = []

    for chunk_index, (chunk_content, embedding) in enumerate(
        zip(text_chunks, embeddings)
    ):
        import tiktoken  # noqa: PLC0415 — deferred to keep startup lean

        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(chunk_content))

        # Build an upsert statement
        insert_stmt = (
            pg_insert(KnowledgeChunk)
            .values(
                document_id=document.id,
                chunk_index=chunk_index,
                content=chunk_content,
                embedding=embedding,
                token_count=token_count,
            )
            .on_conflict_do_update(
                index_elements=["document_id", "chunk_index"],
                set_={
                    "content": chunk_content,
                    "embedding": embedding,
                    "token_count": token_count,
                },
            )
            .returning(KnowledgeChunk)
        )

        result = await db.execute(insert_stmt)
        chunk_row = result.scalar_one()
        upserted_chunks.append(chunk_row)

    log.info(
        "ingest_document_complete",
        document_id=document.id,
        chunks_upserted=len(upserted_chunks),
    )

    return document, upserted_chunks
