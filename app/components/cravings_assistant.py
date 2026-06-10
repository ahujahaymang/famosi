"""
Cravings Assistant — food safety guidance for pregnancy cravings.

Handles:
  - Retrieving food safety knowledge chunks via RAG (Req 11.1)
  - Composing a structured guidance response that includes safety classification
    (safe / moderation / avoid), a recommended serving quantity with unit, the
    nutritional driver behind the craving, and a healthy alternative (Req 11.2)
  - Personalising the healthy-alternative suggestions based on the user's stored
    preferences and allergies; generating without personalisation when none are
    stored (Req 11.3)
  - Prepending a bold safety warning when the food is classified as "avoid"
    (Req 11.4)
  - Declining to respond when the food-safety knowledge base is unavailable and
    directing the user to a healthcare provider (Req 11.5)

LLM tier: "reasoning" (Claude Sonnet 4.6) — craving guidance requires safety
classification with qualifiers, so the Reasoning tier is used per the design's
model-tier table.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Safety warning prepended when safety classification is "avoid" (Req 11.4)
# ---------------------------------------------------------------------------

_AVOID_WARNING = (
    "⚠️ **Safety Warning:** This food is generally recommended to be **avoided** "
    "during pregnancy. Please consult your healthcare provider before consuming it.\n\n"
)

# ---------------------------------------------------------------------------
# Message returned when the knowledge base is unavailable (Req 11.5)
# ---------------------------------------------------------------------------

_KB_UNAVAILABLE_MESSAGE = (
    "I'm sorry, but I'm unable to provide craving guidance right now because the "
    "food safety knowledge base is temporarily unavailable. "
    "For accurate guidance on what foods are safe during pregnancy, please consult "
    "your healthcare provider or midwife."
)

# ---------------------------------------------------------------------------
# LLM system prompts
# ---------------------------------------------------------------------------

_GUIDANCE_WITH_PREFS_SYSTEM = """\
You are a knowledgeable and compassionate pregnancy nutrition assistant.
Using the food safety knowledge context provided and the user's stored dietary
preferences and allergies, compose a structured craving guidance response.

Your response MUST include all four of the following sections:
1. Safety classification: clearly state whether this food is "safe", requires
   "moderation", or should be "avoided" during pregnancy. Include any trimester-
   specific nuances from the knowledge context.
2. Serving quantity: recommend a specific serving size with a clear unit
   (e.g., "1 cup", "30 g", "2 tablespoons").
3. Nutritional driver: briefly explain the underlying nutritional need this
   craving may signal (e.g., craving ice cream may indicate calcium needs).
4. Healthy alternative: suggest one or two pregnancy-safe alternatives that
   address the same nutritional need. Apply all listed dietary preferences,
   food restrictions, and allergies when selecting alternatives.

Keep the response warm, concise, and under 250 words.
Do not provide medical diagnoses or replace professional medical advice.
Always encourage the user to consult their healthcare provider for personal guidance.
"""

_GUIDANCE_NO_PREFS_SYSTEM = """\
You are a knowledgeable and compassionate pregnancy nutrition assistant.
Using the food safety knowledge context provided, compose a structured craving
guidance response.

Your response MUST include all four of the following sections:
1. Safety classification: clearly state whether this food is "safe", requires
   "moderation", or should be "avoided" during pregnancy. Include any trimester-
   specific nuances from the knowledge context.
2. Serving quantity: recommend a specific serving size with a clear unit
   (e.g., "1 cup", "30 g", "2 tablespoons").
3. Nutritional driver: briefly explain the underlying nutritional need this
   craving may signal (e.g., craving ice cream may indicate calcium needs).
4. Healthy alternative: suggest one or two pregnancy-safe alternatives that
   address the same nutritional need.

Keep the response warm, concise, and under 250 words.
Do not provide medical diagnoses or replace professional medical advice.
Always encourage the user to consult their healthcare provider for personal guidance.
"""


# ---------------------------------------------------------------------------
# Public: get_craving_guidance (Req 11.1–11.5)
# ---------------------------------------------------------------------------


async def get_craving_guidance(
    craving_text: str,
    user_id: int,
    db: AsyncSession,
    llm_client: "LLMClient",
    retriever,  # Callable matching app.knowledge.retriever.retrieve signature
) -> str:
    """
    Generate knowledge-grounded, personalised food safety guidance for a craving.

    Steps
    -----
    1. Retrieve food-safety knowledge chunks for *craving_text* (Req 11.1).
    2. If the knowledge base is unavailable (0 chunks or exception), return a
       user-facing message declining to answer and directing to a healthcare
       provider (Req 11.5).
    3. Retrieve the user's active preferences and allergies via
       ``preference_engine.get_active_preferences`` (Req 11.3).
    4. Compose a Reasoning-tier LLM response that includes: safety
       classification (safe/moderation/avoid), serving quantity with unit,
       nutritional driver, and a healthy alternative (Req 11.2).
       If preferences exist, they are passed to the LLM prompt to filter
       alternatives; otherwise the response is generated without
       personalisation (Req 11.3).
    5. If the LLM response contains "avoid" as the safety classification,
       prepend a bold safety warning before the rest of the content (Req 11.4).

    Args:
        craving_text: The user's free-text description of the craving.
                      Must not be logged per privacy rules.
        user_id:      Internal user PK.
        db:           Active async DB session.
        llm_client:   Shared ``LLMClient`` instance.
        retriever:    The ``retrieve`` coroutine from
                      ``app.knowledge.retriever`` (injected to allow testing
                      without a live database).  Expected signature:
                      ``retrieve(query_text, db, llm_client, top_k, category)``

    Returns:
        A formatted guidance string ready to send to the user.

    Requirements: 11.1, 11.2, 11.3, 11.4, 11.5
    """
    from app.components.preference_engine import get_active_preferences
    from app.models.knowledge_document import KnowledgeCategory

    log = logger.bind(user_id=user_id)

    # ── Step 1: Retrieve food safety knowledge chunks (Req 11.1) ────────────
    knowledge_available = True
    knowledge_context = ""

    try:
        chunks = await retriever(
            query_text=craving_text,
            db=db,
            llm_client=llm_client,
            top_k=5,
            category=KnowledgeCategory.nutrition,
        )
        if chunks:
            knowledge_context = "\n\n".join(c.content for c in chunks)
            log.info("craving_guidance_kb_hit", chunks_count=len(chunks))
        else:
            knowledge_available = False
            log.info("craving_guidance_kb_empty")
    except Exception:  # noqa: BLE001
        knowledge_available = False
        log.warning("craving_guidance_kb_unavailable")

    # ── Step 2: Decline if KB is unavailable (Req 11.5) ─────────────────────
    if not knowledge_available:
        log.info("craving_guidance_declined_kb_unavailable")
        return _KB_UNAVAILABLE_MESSAGE

    # ── Step 3: Retrieve user preferences and allergies (Req 11.3) ──────────
    preferences = await get_active_preferences(user_id, db)

    pref_lines: list[str] = []
    for pref in preferences:
        pref_lines.append(f"- {pref.preference_type.value}: {pref.food_item}")

    has_preferences = bool(pref_lines)

    # ── Step 4: Compose LLM response (Req 11.2, 11.3) ───────────────────────
    if has_preferences:
        # Personalised: pass preferences to filter alternatives (Req 11.3)
        system_prompt = _GUIDANCE_WITH_PREFS_SYSTEM
        pref_block = (
            "User dietary preferences, restrictions, and allergies:\n"
            + "\n".join(pref_lines)
        )
        user_content = (
            f"Food safety knowledge context:\n{knowledge_context}\n\n"
            f"{pref_block}\n\n"
            f"Please provide pregnancy craving guidance for: {craving_text}"
        )
    else:
        # No stored preferences — generate without personalisation (Req 11.3)
        system_prompt = _GUIDANCE_NO_PREFS_SYSTEM
        user_content = (
            f"Food safety knowledge context:\n{knowledge_context}\n\n"
            f"Please provide pregnancy craving guidance for: {craving_text}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = await llm_client.complete("reasoning", messages)

    log.info(
        "craving_guidance_generated",
        has_preferences=has_preferences,
        tokens_used=response.tokens_used,
    )

    guidance_text = response.content.strip()

    # ── Step 5: Prepend safety warning if classification is "avoid" (Req 11.4)
    # Detect "avoid" classification by checking the LLM response text.
    # We look for the word "avoid" as a safety classification marker, guarding
    # against false positives by checking common phrasings the prompt instructs
    # the model to use (e.g., "avoid", "should be avoided", "classified as avoid").
    response_lower = guidance_text.lower()
    is_avoid = (
        "safety classification: avoid" in response_lower
        or "classified as avoid" in response_lower
        or "classification: avoid" in response_lower
        or ": avoid" in response_lower
    )

    if is_avoid:
        log.info("craving_guidance_avoid_warning_prepended")
        guidance_text = _AVOID_WARNING + guidance_text

    return guidance_text
