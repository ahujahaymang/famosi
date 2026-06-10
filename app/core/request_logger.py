"""
Request log persistence.

Provides a single coroutine — ``log_request`` — that inserts one row into
``request_logs`` after every handled Telegram message.

Privacy contract
-----------------
NEVER pass message text, food items, symptom names, or any health content to
this function.  Only structural telemetry fields are stored and logged via
structlog (request_id, intent, latency_ms).

Requirements: 14.7, 16.1, 16.2
"""

from __future__ import annotations

import uuid
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.request_log import IntentType, RequestLog

logger = structlog.get_logger(__name__)


async def log_request(
    request_id: str,
    user_id: Optional[int],
    intent: Optional[IntentType],
    model_used: Optional[str],
    tokens_used: Optional[int],
    latency_ms: int,
    is_rag: bool,
    rag_chunks_count: Optional[int],
    rag_empty: Optional[bool],
    db: AsyncSession,
    *,
    telegram_user_id: Optional[int] = None,
) -> None:
    """
    Persist a ``RequestLog`` row to the database.

    This function is called exactly once per ``dispatch()`` invocation,
    unconditionally, in a ``finally`` block so it executes even when an
    exception propagates (Req 14.7).

    Errors during log writing are swallowed and reported to structlog only —
    they must never surface to the user.

    Parameters
    ----------
    request_id:
        The UUID string assigned by the request-ID middleware (Req 14.6).
    user_id:
        Internal database ``users.id`` for the authenticated user, or ``None``
        when the user record is not yet resolved (e.g. during onboarding).
    intent:
        The classified ``IntentType`` enum value, or ``None`` when
        classification did not complete.
    model_used:
        The model name string returned by the LLM client, or ``None``.
    tokens_used:
        Total tokens consumed across all LLM calls for this request, or
        ``None`` when unavailable.
    latency_ms:
        End-to-end processing time in milliseconds from message receipt to
        handler completion.
    is_rag:
        ``True`` when the RAG retrieval pipeline was invoked for this request.
    rag_chunks_count:
        Number of knowledge chunks retrieved by the RAG pipeline, or ``None``
        when RAG was not used.
    rag_empty:
        ``True`` when the RAG pipeline returned zero chunks, ``False`` when
        chunks were returned, ``None`` when RAG was not used.
    db:
        An open ``AsyncSession`` used to insert the row and commit.
    telegram_user_id:
        The raw Telegram numeric user ID (``update.effective_user.id``).
        Optional; stored for cross-referencing but not required.
    """
    try:
        # Parse request_id string to UUID for the PG UUID column
        try:
            rid = uuid.UUID(request_id)
        except (ValueError, AttributeError):
            rid = uuid.uuid4()

        log_row = RequestLog(
            request_id=rid,
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            intent=intent,
            model_used=model_used,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            is_rag=is_rag,
            rag_chunks_count=rag_chunks_count,
            rag_empty=rag_empty,
        )
        db.add(log_row)
        await db.commit()

        logger.debug(
            "request_log_written",
            request_id=request_id,
            intent=intent.value if intent else None,
            is_rag=is_rag,
            latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001
        logger.exception("request_log_write_failed", request_id=request_id)
