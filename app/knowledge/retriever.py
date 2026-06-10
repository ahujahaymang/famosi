"""
RAG retriever — embed a query and fetch the top-k knowledge chunks via pgvector.

Responsibilities
----------------
- ``embed(text_input, llm_client)``: call OpenAI embedding API with the
  configured EMBEDDING_MODEL and return a list of floats.
- ``retrieve(query_text, db, llm_client, top_k, category)``: embed the query,
  run a cosine-similarity search on ``knowledge_chunks.embedding``, log the
  retrieval event, and return the matching ``KnowledgeChunk`` ORM objects.

Logging contract (Req 15.5)
----------------------------
Logs a single ``rag_retrieval`` event per call that includes:
  - ``chunks_count``— number of chunks returned
  - ``top_doc_ids`` — document IDs of the returned chunks (structural only)
  - ``rag_empty``   — True when no chunks were found
  - ``category``    — category filter applied (or None)

No message content, health data, or chunk text is ever written to logs.

Requirements: 15.4, 15.5, 15.6
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeCategory, KnowledgeDocument

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient

logger = structlog.get_logger(__name__)


async def embed(text_input: str, llm_client: "LLMClient") -> list[float]:
    """
    Generate an embedding vector for ``text_input`` using the OpenAI
    embeddings endpoint and the model configured in ``EMBEDDING_MODEL``.

    Parameters
    ----------
    text_input:
        The text to embed.
    llm_client:
        An ``LLMClient`` instance whose OpenAI client is already initialised.
        We reuse its lazy ``_get_openai_client()`` to avoid creating a second
        client and to respect the same API key.

    Returns
    -------
    A list of 1536 floats (text-embedding-3-small dimensionality).

    Raises
    ------
    openai.OpenAIError on API failure.
    """
    openai_client = llm_client._get_openai_client()
    model_name = settings.llm.embedding_model

    response = await openai_client.embeddings.create(
        model=model_name,
        input=text_input,
    )

    return response.data[0].embedding


async def retrieve(
    query_text: str,
    db: AsyncSession,
    llm_client: "LLMClient",
    top_k: int = 5,
    category: KnowledgeCategory | None = None,
) -> list[KnowledgeChunk]:
    """
    Retrieve the ``top_k`` most relevant knowledge chunks for ``query_text``.

    Steps
    -----
    1. Embed ``query_text`` via :func:`embed`.
    2. Query ``knowledge_chunks`` using pgvector's cosine distance operator
       ``<=>`` with an optional ``category`` filter joined through
       ``knowledge_documents``.
    3. Log the retrieval event (Req 15.5).
    4. Return the list (may be empty — callers must handle the no-guidance
       case, Req 15.6).

    Parameters
    ----------
    query_text:
        The user's question to retrieve context for.
    db:
        Async SQLAlchemy session.
    llm_client:
        Initialised ``LLMClient`` (embedding reuses its OpenAI client).
    top_k:
        Maximum number of chunks to return.  Default 5 (Req 15.4).
    category:
        Optional ``KnowledgeCategory`` to filter by.  Used by the Partner role
        to bias toward ``dad_support`` content (Req 18.1).

    Returns
    -------
    List of ``KnowledgeChunk`` ORM objects, ordered by cosine similarity
    (closest first).  Empty list if no matching documents exist.
    """
    # Step 1 — embed query
    query_embedding: list[float] = await embed(query_text, llm_client)

    # Serialise the embedding vector as a pgvector literal for the ORDER BY clause.
    # The <=> operator computes cosine distance (lower value = more similar).
    embedding_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"
    cosine_distance_expr = text(
        f"knowledge_chunks.embedding <=> '{embedding_literal}'::vector"
    )

    # Step 2 — build query
    if category is not None:
        # Join with knowledge_documents to filter by category
        stmt = (
            select(KnowledgeChunk)
            .join(
                KnowledgeDocument,
                KnowledgeChunk.document_id == KnowledgeDocument.id,
            )
            .where(
                KnowledgeDocument.category == category,
                KnowledgeDocument.active.is_(True),
            )
            .order_by(cosine_distance_expr)
            .limit(top_k)
        )
    else:
        stmt = (
            select(KnowledgeChunk)
            .order_by(cosine_distance_expr)
            .limit(top_k)
        )

    result = await db.execute(stmt)
    chunks: list[KnowledgeChunk] = list(result.scalars().all())

    # Step 3 — log retrieval event (Req 15.5)
    top_doc_ids: list[int] = [c.document_id for c in chunks]
    rag_empty: bool = len(chunks) == 0

    logger.info(
        "rag_retrieval",
        chunks_count=len(chunks),
        top_doc_ids=top_doc_ids,
        rag_empty=rag_empty,
        category=category.value if category else None,
    )

    # Step 4 — return list (empty is valid; caller handles no-guidance per Req 15.6)
    return chunks
