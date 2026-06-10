"""
Message dispatcher — routes classified intent to the correct handler pipeline.

This module is the central hub between the Telegram update stream and all
downstream processing components.  Every text message (or callback query that
carries a user message) passes through ``dispatch()``.

Intent routing table
--------------------
  LOGGING            → extraction pipeline → confirmation loop
                        (delegated to ``logging_handler.handle_logging_intent``)
  PERSONAL_DATA_QUERY → personal memory query path
                        (delegated to ``query_handler.handle_query_intent``)
  KNOWLEDGE_QUESTION  → RAG pipeline
                        (delegated to ``knowledge_handler.handle_knowledge_intent``)
  MIXED_QUERY         → both PERSONAL_DATA_QUERY and KNOWLEDGE_QUESTION paths run
                        concurrently; results are synthesised via the "reasoning"
                        LLM tier.  If one path fails, the available result is used
                        and the user is notified of the missing source (Req 14.5).
  UNCLASSIFIED        → error message sent; NO downstream pipeline is invoked
                        (Req 14.8).

Request logging
---------------
A ``request_log`` row is written to the database on every call to ``dispatch()``,
capturing: request_id, user_id, telegram_user_id, intent, model_used,
tokens_used, and latency_ms (Req 14.7).

Circular-import avoidance
--------------------------
``logging_handler``, ``query_handler``, and ``knowledge_handler`` will import
the dispatcher indirectly (via shared utilities).  To prevent circular imports,
these three modules are imported lazily inside the handler branches — never at
module level.  The ``TYPE_CHECKING`` guard is used for type annotations only.

Privacy contract
-----------------
  - NEVER log message text, food items, symptom names, or any health content.
  - Structlog fields: request_id, user_id, intent, tier, latency_ms only.

Requirements: 14.1, 14.3, 14.4, 14.5, 14.7, 14.8
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog
import structlog.contextvars
from telegram import Update
from telegram.ext import ContextTypes

from app.core.intent_router import IntentRouter, RouteResult
from app.core.llm_client import LLMClient
from app.core.request_logger import log_request
from app.dependencies import _AsyncSessionFactory
from app.models.request_log import IntentType

if TYPE_CHECKING:
    pass  # forward references for future handler modules live here

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Error / informational messages
# ---------------------------------------------------------------------------

_MSG_UNCLASSIFIED = (
    "I'm sorry, I wasn't able to understand what you meant. "
    "Could you rephrase your message?\n\n"
    "You can:\n"
    "• Log health data (e.g. 'I ate rice and dal for lunch')\n"
    "• Ask about your records (e.g. 'what did I log yesterday?')\n"
    "• Ask a pregnancy question (e.g. 'is it safe to eat sushi?')"
)

_MSG_MIXED_PARTIAL_KNOWLEDGE_MISSING = (
    "\n\n⚠️ _Note: I could not retrieve knowledge-base guidance for this "
    "response. The information above is based on your personal records only._"
)

_MSG_MIXED_PARTIAL_QUERY_MISSING = (
    "\n\n⚠️ _Note: I could not retrieve your personal records for this "
    "response. The information above is based on general knowledge only._"
)

_MSG_MIXED_BOTH_FAILED = (
    "I'm sorry, I encountered an error retrieving both your personal records "
    "and knowledge-base information. Please try again in a moment."
)

_MSG_GENERIC_ERROR = (
    "I encountered an unexpected error processing your request. "
    "Please try again in a moment."
)


# ---------------------------------------------------------------------------
# Intent → IntentType mapper
# ---------------------------------------------------------------------------

def _intent_label_to_db_type(label: str) -> IntentType | None:
    """Convert a router intent label to the ORM IntentType enum value."""
    mapping: dict[str, IntentType] = {
        "LOGGING": IntentType.logging,
        "PERSONAL_DATA_QUERY": IntentType.personal_data_query,
        "KNOWLEDGE_QUESTION": IntentType.knowledge_question,
        "MIXED_QUERY": IntentType.mixed_query,
        "UNCLASSIFIED": IntentType.unclassified,
    }
    return mapping.get(label)


# ---------------------------------------------------------------------------
# Handler stubs — lazy imports to avoid circular references
# ---------------------------------------------------------------------------

async def _invoke_logging_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    llm_client: LLMClient,
    route_result: RouteResult,
) -> dict[str, Any]:
    """
    Delegate to logging_handler.handle_logging_intent.

    Returns a dict with keys: model_used (str | None), tokens_used (int | None).

    This lazy import pattern keeps the dispatcher compilable before
    logging_handler.py exists — Python only resolves the import when this
    branch is actually executed.
    """
    try:
        from app.bot.handlers.logging_handler import handle_logging_intent  # noqa: PLC0415
        return await handle_logging_intent(update, context, llm_client, route_result)
    except ImportError:
        logger.warning("logging_handler_not_yet_implemented")
        if update.message:
            await update.message.reply_text(
                "Logging is not yet available. Please try again later."
            )
        return {"model_used": None, "tokens_used": None}


async def _invoke_query_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    llm_client: LLMClient,
    route_result: RouteResult,
    user_message: str,
) -> tuple[str | None, dict[str, Any]]:
    """
    Delegate to query_handler.handle_query_intent.

    Returns (response_text | None, metadata_dict).
    response_text is None on failure.
    metadata_dict contains: model_used, tokens_used.
    """
    try:
        from app.bot.handlers.query_handler import handle_query_intent  # noqa: PLC0415
        return await handle_query_intent(update, context, llm_client, route_result, user_message)
    except ImportError:
        logger.warning("query_handler_not_yet_implemented")
        return None, {"model_used": None, "tokens_used": None}


async def _invoke_knowledge_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    llm_client: LLMClient,
    route_result: RouteResult,
    user_message: str,
) -> tuple[str | None, dict[str, Any]]:
    """
    Delegate to knowledge_handler.handle_knowledge_intent.

    Returns (response_text | None, metadata_dict).
    response_text is None on failure.
    metadata_dict contains: model_used, tokens_used.
    """
    try:
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent  # noqa: PLC0415
        return await handle_knowledge_intent(update, context, llm_client, route_result, user_message)
    except ImportError:
        logger.warning("knowledge_handler_not_yet_implemented")
        return None, {"model_used": None, "tokens_used": None}


# ---------------------------------------------------------------------------
# MIXED_QUERY synthesis
# ---------------------------------------------------------------------------

async def _handle_mixed_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    llm_client: LLMClient,
    route_result: RouteResult,
    user_message: str,
) -> dict[str, Any]:
    """
    Handle MIXED_QUERY intent: run personal-data and knowledge paths
    concurrently, then synthesise the results via the reasoning LLM tier.

    Partial failure policy (Req 14.5):
      - If the knowledge path fails: use the query result and append a
        note that knowledge-base guidance is unavailable.
      - If the query path fails: use the knowledge result and append a
        note that personal records are unavailable.
      - If both fail: send a generic error message; do not synthesise.

    Returns a metadata dict with: model_used, tokens_used.
    """
    log = logger.bind(intent="MIXED_QUERY")

    # Run both paths concurrently; capture exceptions rather than raising
    query_task = asyncio.create_task(
        _invoke_query_handler(update, context, llm_client, route_result, user_message)
    )
    knowledge_task = asyncio.create_task(
        _invoke_knowledge_handler(update, context, llm_client, route_result, user_message)
    )

    results = await asyncio.gather(query_task, knowledge_task, return_exceptions=True)

    # Unpack results — each is either (text, metadata) or an Exception
    query_result = results[0]
    knowledge_result = results[1]

    query_text: str | None = None
    knowledge_text: str | None = None
    query_meta: dict[str, Any] = {"model_used": None, "tokens_used": None}
    knowledge_meta: dict[str, Any] = {"model_used": None, "tokens_used": None}

    if isinstance(query_result, Exception):
        log.warning("mixed_query_query_path_failed", error=str(query_result))
    else:
        query_text, query_meta = query_result

    if isinstance(knowledge_result, Exception):
        log.warning("mixed_query_knowledge_path_failed", error=str(knowledge_result))
    else:
        knowledge_text, knowledge_meta = knowledge_result

    # Both paths failed
    if query_text is None and knowledge_text is None:
        if update.message:
            await update.message.reply_text(_MSG_MIXED_BOTH_FAILED)
        return {"model_used": None, "tokens_used": None}

    # Only one path succeeded — send the available result with a caveat note
    if query_text is None:
        disclaimer = _MSG_MIXED_PARTIAL_QUERY_MISSING
        if update.message:
            await update.message.reply_text(
                f"{knowledge_text}{disclaimer}",
                parse_mode="Markdown",
            )
        return knowledge_meta

    if knowledge_text is None:
        disclaimer = _MSG_MIXED_PARTIAL_KNOWLEDGE_MISSING
        if update.message:
            await update.message.reply_text(
                f"{query_text}{disclaimer}",
                parse_mode="Markdown",
            )
        return query_meta

    # Both paths succeeded — synthesise via reasoning tier (Req 14.4)
    synthesis_messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful pregnancy assistant. "
                "You have been given two pieces of information to combine into a "
                "single, coherent, and accurate response for the user:\n\n"
                "1. PERSONAL RECORDS SUMMARY: information extracted from the user's "
                "own logged health data.\n"
                "2. KNOWLEDGE BASE GUIDANCE: medically-grounded information from "
                "trusted sources (ACOG, WHO, CDC, NHS).\n\n"
                "Synthesise both into one unified, helpful response. "
                "Do not reveal internal labels like 'PERSONAL RECORDS SUMMARY'. "
                "Be concise, warm, and clear. "
                "If the two sources are inconsistent, prioritise the knowledge-base "
                "guidance and note any discrepancy gently."
            ),
        },
        {
            "role": "user",
            "content": (
                f"User question: {user_message}\n\n"
                f"Personal records summary:\n{query_text}\n\n"
                f"Knowledge base guidance:\n{knowledge_text}"
            ),
        },
    ]

    try:
        synthesis_response = await llm_client.complete("reasoning", synthesis_messages)
        synthesised_text = synthesis_response.content
        model_used = synthesis_response.model
        # Accumulate tokens from all three calls
        tokens_used = (
            (query_meta.get("tokens_used") or 0)
            + (knowledge_meta.get("tokens_used") or 0)
            + synthesis_response.tokens_used
        )
    except Exception:  # noqa: BLE001
        log.exception("mixed_query_synthesis_failed")
        # Fall back: send both results separately with a join separator
        synthesised_text = (
            f"{query_text}\n\n---\n\n{knowledge_text}"
        )
        model_used = knowledge_meta.get("model_used")
        tokens_used = (
            (query_meta.get("tokens_used") or 0)
            + (knowledge_meta.get("tokens_used") or 0)
        )

    if update.message:
        await update.message.reply_text(synthesised_text)

    return {"model_used": model_used, "tokens_used": tokens_used}


# ---------------------------------------------------------------------------
# Main dispatch entry point
# ---------------------------------------------------------------------------

async def dispatch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    llm_client: LLMClient | None = None,
) -> None:
    """
    Central dispatcher for all incoming Telegram messages.

    This coroutine is registered as the PTB message handler in ``app/main.py``.
    It:

    1. Extracts the user's text from the ``Update``.
    2. Runs intent classification via :class:`~app.core.intent_router.IntentRouter`.
    3. Routes to the appropriate handler (or returns an error for UNCLASSIFIED).
    4. Writes a ``request_log`` row on completion, regardless of outcome.

    Parameters
    ----------
    update:
        The incoming Telegram ``Update`` object.
    context:
        The PTB ``ContextTypes.DEFAULT_TYPE`` carrying bot data and user data.
    llm_client:
        Optional pre-instantiated :class:`~app.core.llm_client.LLMClient`.
        When ``None`` a new instance is created per request.  Pass a shared
        instance from application startup for connection re-use.
    """
    start_time = time.monotonic()

    # ------------------------------------------------------------------
    # Extract structural fields — never log message content (Req 16.2)
    # ------------------------------------------------------------------
    effective_user = update.effective_user
    telegram_user_id: int | None = effective_user.id if effective_user else None

    # Retrieve the request_id from structlog context (set by RequestIdMiddleware)
    bound_ctx = structlog.contextvars.get_contextvars()
    request_id: str = bound_ctx.get("request_id") or str(uuid.uuid4())

    log = logger.bind(request_id=request_id, telegram_user_id=telegram_user_id)

    # Extract user_id from context bot_data if set by auth middleware
    user_id: int | None = None
    if context.bot_data and isinstance(context.bot_data.get("current_user"), object):
        user_obj = context.bot_data.get("current_user")
        user_id = getattr(user_obj, "id", None)

    # ------------------------------------------------------------------
    # Extract message text
    # ------------------------------------------------------------------
    user_message: str = ""
    if update.message and update.message.text:
        user_message = update.message.text.strip()
    elif update.edited_message and update.edited_message.text:
        user_message = update.edited_message.text.strip()

    if not user_message:
        # Non-text update (photo, sticker, etc.) — ignore silently
        log.debug("dispatch_skipped_non_text_update")
        return

    # ------------------------------------------------------------------
    # Initialise LLM client
    # ------------------------------------------------------------------
    if llm_client is None:
        llm_client = LLMClient()

    # ------------------------------------------------------------------
    # Approval pending gate — block pending users before any processing
    # ------------------------------------------------------------------
    if context.bot_data and context.bot_data.get("approval_pending"):
        if update.message:
            await update.message.reply_text(
                "⏳ Your account is awaiting admin approval.\n"
                "You'll receive a message as soon as you're approved — "
                "usually within a few hours. Thanks for your patience!"
            )
        return

    # ------------------------------------------------------------------
    # Intent classification
    # ------------------------------------------------------------------
    router = IntentRouter(llm_client)
    route_result: RouteResult | None = None

    # Telemetry accumulators — updated by each branch
    intent_type_db: IntentType | None = None
    model_used: str | None = None
    tokens_used: int | None = None

    try:
        route_result = await router.route(user_message)
        intent_label = route_result.intent
        intent_type_db = _intent_label_to_db_type(intent_label)

        log.info(
            "dispatch_intent_classified",
            intent=intent_label,
            confidence=route_result.confidence,
            tier=route_result.tier,
            escalated=route_result.escalated,
        )

        # ------------------------------------------------------------------
        # Route by intent
        # ------------------------------------------------------------------

        if intent_label == "UNCLASSIFIED":
            # Req 14.8 — return error; invoke NO downstream pipeline
            log.info("dispatch_unclassified_intent")
            if update.message:
                await update.message.reply_text(_MSG_UNCLASSIFIED)

        elif intent_label == "LOGGING":
            # Req 14.1 → extraction pipeline → confirmation loop
            meta = await _invoke_logging_handler(update, context, llm_client, route_result)
            model_used = meta.get("model_used")
            tokens_used = meta.get("tokens_used")

        elif intent_label == "PERSONAL_DATA_QUERY":
            # Req 14.3 — personal memory query path only (NOT knowledge base)
            text, meta = await _invoke_query_handler(
                update, context, llm_client, route_result, user_message
            )
            model_used = meta.get("model_used")
            tokens_used = meta.get("tokens_used")
            if text and update.message:
                await update.message.reply_text(text)

        elif intent_label == "KNOWLEDGE_QUESTION":
            # Req 14.4 — RAG pipeline
            text, meta = await _invoke_knowledge_handler(
                update, context, llm_client, route_result, user_message
            )
            model_used = meta.get("model_used")
            tokens_used = meta.get("tokens_used")
            if text and update.message:
                await update.message.reply_text(text)

        elif intent_label == "MIXED_QUERY":
            # Req 14.5 — both paths concurrently, synthesise via reasoning tier
            meta = await _handle_mixed_query(
                update, context, llm_client, route_result, user_message
            )
            model_used = meta.get("model_used")
            tokens_used = meta.get("tokens_used")

        else:
            # Defensive fallback — treat unknown labels as unclassified
            log.warning("dispatch_unknown_intent_label", label=intent_label)
            if update.message:
                await update.message.reply_text(_MSG_UNCLASSIFIED)

    except Exception:  # noqa: BLE001
        log.exception("dispatch_unhandled_exception")
        if update.message:
            await update.message.reply_text(_MSG_GENERIC_ERROR)

    finally:
        # ------------------------------------------------------------------
        # Write request_log — unconditionally, on every dispatch call
        # (Req 14.7)
        # ------------------------------------------------------------------
        latency_ms = int((time.monotonic() - start_time) * 1000)

        async with _AsyncSessionFactory() as db:
            await log_request(
                request_id=request_id,
                user_id=user_id,
                intent=intent_type_db,
                model_used=model_used,
                tokens_used=tokens_used,
                latency_ms=latency_ms,
                is_rag=False,
                rag_chunks_count=None,
                rag_empty=None,
                db=db,
                telegram_user_id=telegram_user_id,
            )

        # Feed in-memory metrics for the admin /metrics command
        try:
            from app.services.admin_service import metrics as _metrics  # noqa: PLC0415
            _metrics.record_request(
                user_id=user_id,
                intent=intent_type_db.value if intent_type_db else None,
                model_used=model_used,
                tokens_used=tokens_used,
                latency_ms=latency_ms,
            )
        except Exception:  # noqa: BLE001
            pass  # metrics must never crash the request path

        log.info(
            "dispatch_complete",
            intent=intent_type_db.value if intent_type_db else None,
            latency_ms=latency_ms,
        )
