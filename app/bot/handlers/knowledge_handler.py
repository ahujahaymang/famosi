"""
Knowledge question handler — entry point for KNOWLEDGE_QUESTION intents.

Implements the RAG-based question-answering pipeline described in the design
(Section 5.2 and 6.3):

  1. Call ``retriever.retrieve()`` to fetch the top-5 most relevant knowledge
     chunks for the user's query.
  2. If no chunks are found (RAG miss), return a no-guidance message
     recommending the user consult a healthcare provider (Req 15.6).
  3. Compose a grounded response using ``LLMClient.complete("reasoning", ...)``
     with the retrieved chunks injected as context in the system prompt.
  4. For Partner-role users, bias retrieval toward the ``dad_support``
     category by passing a category filter to the retriever (Req 18.1).

Privacy contract
----------------
NEVER log message content, chunk text, or health data.
Only structural fields are logged: user_id, chunks_count, rag_empty, role.

Requirements: 14.4, 15.4, 15.6, 18.1
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from app.dependencies import _AsyncSessionFactory
from app.models.knowledge_document import KnowledgeCategory
from app.models.user import UserRole

if TYPE_CHECKING:
    from app.core.intent_router import RouteResult
    from app.core.llm_client import LLMClient
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# No-guidance fallback (Req 15.6) — only used when LLM also fails
# ---------------------------------------------------------------------------

_NO_GUIDANCE_MESSAGE = (
    "I wasn't able to answer that right now. "
    "For accurate advice on your pregnancy, please consult your healthcare provider "
    "or midwife — they're the best source of information for your individual situation."
)

# ---------------------------------------------------------------------------
# System prompt for the reasoning-tier LLM composition step (with RAG context)
# ---------------------------------------------------------------------------

_KNOWLEDGE_SYSTEM_PROMPT_TEMPLATE = """\
You are Famosi, a knowledgeable and empathetic pregnancy assistant.

Answer the user's question using ONLY the context passages provided below.
Do not invent information that is not present in the context.
If the context does not contain enough information to answer confidently,
say so clearly and recommend the user speak with their healthcare provider.

Guidelines:
- Be warm, clear, and concise — this is a Telegram message.
- Cite the source name (e.g. ACOG, WHO, NHS, CDC) when relevant.
- Do NOT include any medical disclaimers beyond recommending a healthcare
  provider when uncertain.
- Keep the response under 400 words.

--- CONTEXT PASSAGES ---
{context}
--- END OF CONTEXT ---
"""

# ---------------------------------------------------------------------------
# System prompt for LLM-only fallback (no RAG context available)
# ---------------------------------------------------------------------------

_FALLBACK_SYSTEM_PROMPT_TEMPLATE = """\
You are Famosi, a knowledgeable and empathetic pregnancy assistant.

The user is currently {gestational_context}.

Answer their question using your general pregnancy knowledge.
Guidelines:
- Tailor your answer to their gestational stage where relevant.
- Be warm, clear, and concise — this is a Telegram message, not a medical document.
- Mention any well-known safety considerations specific to pregnancy.
- End with a brief note to confirm with their healthcare provider for personalised advice.
- Keep the response under 400 words.
- Do NOT say you don't have information — you are a knowledgeable assistant.
"""

# Category filter applied for Partner role (Req 18.1)
_PARTNER_CATEGORY_FILTER = KnowledgeCategory.dad_support


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def handle_knowledge_intent(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
    llm_client: "LLMClient",
    route_result: "RouteResult",
    user_message: str,
) -> tuple[str | None, dict[str, Any]]:
    """
    Entry point for KNOWLEDGE_QUESTION intents, called by ``dispatcher.py``.

    Parameters
    ----------
    update:
        Incoming Telegram ``Update``.
    context:
        PTB context carrying ``bot_data`` (auth user) and ``user_data``.
    llm_client:
        Pre-initialised ``LLMClient`` from the dispatcher.
    route_result:
        Classification result from ``IntentRouter.route()``.
    user_message:
        The raw text of the user's message.

    Returns
    -------
    ``(response_text | None, {"model_used": str | None, "tokens_used": int | None})``

    ``response_text`` is the composed answer or the no-guidance fallback.
    ``None`` is returned only on unrecoverable failure (dispatcher handles
    error messaging in that case).
    """
    effective_user = update.effective_user
    telegram_user_id: int | None = effective_user.id if effective_user else None

    # ------------------------------------------------------------------
    # Resolve the internal DB user from bot_data (set by auth middleware)
    # ------------------------------------------------------------------
    user_id: int | None = None
    user_role: UserRole | None = None
    user_obj = None

    if context.bot_data:
        user_obj = context.bot_data.get("current_user")
        if user_obj is not None:
            user_id = getattr(user_obj, "id", None)
            raw_role = getattr(user_obj, "role", None)
            if raw_role is not None:
                role_value = str(getattr(raw_role, "value", raw_role)).lower()
                try:
                    user_role = UserRole(role_value)
                except ValueError:
                    user_role = None

    log = logger.bind(
        telegram_user_id=telegram_user_id,
        user_id=user_id,
        role=user_role.value if user_role else None,
    )
    log.info("knowledge_handler_invoked")

    # ------------------------------------------------------------------
    # Step 1 — determine category bias for Partner role (Req 18.1)
    # ------------------------------------------------------------------
    category_filter: KnowledgeCategory | None = None
    if user_role == UserRole.partner:
        category_filter = _PARTNER_CATEGORY_FILTER
        log.debug("knowledge_handler_partner_category_filter", category=category_filter.value)

    # ------------------------------------------------------------------
    # Step 2 — retrieve relevant chunks via RAG
    # ------------------------------------------------------------------
    from app.knowledge import retriever  # noqa: PLC0415

    chunks = []
    try:
        async with _AsyncSessionFactory() as db:
            chunks = await retriever.retrieve(
                query_text=user_message,
                db=db,
                llm_client=llm_client,
                top_k=5,
                category=category_filter,
            )
    except Exception:  # noqa: BLE001
        log.exception("knowledge_handler_retrieval_failed")
        return None, {"model_used": None, "tokens_used": None}

    log.info(
        "knowledge_handler_retrieval_done",
        chunks_count=len(chunks),
        rag_empty=len(chunks) == 0,
    )

    # ------------------------------------------------------------------
    # Step 3 — LLM fallback when RAG returns empty (Req 15.6 adapted)
    # When the knowledge base has no relevant chunks, fall through to
    # the LLM using the user's gestational stage as grounding context.
    # ------------------------------------------------------------------
    if not chunks:
        log.info("knowledge_handler_rag_empty_using_llm_fallback")
        return await _llm_fallback(user_message, user_obj, llm_client, log)

    # ------------------------------------------------------------------
    # Step 4 — compose grounded response via the reasoning tier (Req 14.4)
    # ------------------------------------------------------------------
    context_text = _build_context_text(chunks)
    system_prompt = _KNOWLEDGE_SYSTEM_PROMPT_TEMPLATE.format(context=context_text)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        # Try reasoning tier; fall back to mini if Bedrock isn't configured
        try:
            response = await llm_client.complete("reasoning", messages)
        except Exception:
            log.info("knowledge_handler_using_mini_tier_fallback")
            response = await llm_client.complete("mini", messages)
        return response.content, {
            "model_used": response.model,
            "tokens_used": response.tokens_used,
        }
    except Exception:  # noqa: BLE001
        log.exception("knowledge_handler_llm_completion_failed")
        return None, {"model_used": None, "tokens_used": None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _llm_fallback(
    user_message: str,
    user_obj: Any,
    llm_client: "LLMClient",
    log: Any,
) -> tuple[str | None, dict]:
    """
    Answer the question using the LLM's own knowledge when RAG has no chunks.
    Injects the user's gestational stage to personalise the response.
    """
    from datetime import date
    from app.components.pregnancy_engine import calculate_gestational_age

    # Build gestational context string
    gestational_context = "pregnant"
    if user_obj is not None:
        due_date = getattr(user_obj, "due_date", None)
        lmp_date = getattr(user_obj, "lmp_date", None)
        try:
            if due_date:
                weeks, days = calculate_gestational_age(due_date, date.today())
                gestational_context = f"{weeks} weeks and {days} days pregnant"
            elif lmp_date:
                # Estimate due date from LMP
                from datetime import timedelta
                estimated_due = lmp_date + timedelta(days=280)
                weeks, days = calculate_gestational_age(estimated_due, date.today())
                gestational_context = f"approximately {weeks} weeks and {days} days pregnant"
        except Exception:
            pass  # fall back to generic string

    system_prompt = _FALLBACK_SYSTEM_PROMPT_TEMPLATE.format(
        gestational_context=gestational_context
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        # Try reasoning tier first; fall back to mini if Bedrock isn't configured
        try:
            response = await llm_client.complete("reasoning", messages)
        except Exception:
            log.info("knowledge_handler_llm_fallback_using_mini_tier")
            response = await llm_client.complete("mini", messages)
        return response.content, {
            "model_used": response.model,
            "tokens_used": response.tokens_used,
        }
    except Exception:  # noqa: BLE001
        log.exception("knowledge_handler_llm_fallback_failed")
        return _NO_GUIDANCE_MESSAGE, {"model_used": None, "tokens_used": None}


def _build_context_text(chunks: list) -> str:
    """
    Serialise a list of ``KnowledgeChunk`` ORM objects into a numbered
    context string for injection into the system prompt.

    Format::

        [1] <chunk content>

        [2] <chunk content>
        ...

    Chunk content is included verbatim — it has already been reviewed and
    approved during ingestion.
    """
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        parts.append(f"[{i}] {chunk.content}")
    return "\n\n".join(parts)
