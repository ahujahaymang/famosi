"""
Symptom Assistant — trend reports, severity alerts, and knowledge-grounded guidance.

Handles:
  - 90-day trend report grouped by symptom name with severity and frequency (Req 8.2)
  - Proactive severity alert when a logged symptom severity >= 8; appends a
    healthcare provider note to the confirmation reply (Req 8.3)
  - Knowledge-base-grounded guidance for a given symptom, with graceful
    fallback when the knowledge base is unavailable (Req 8.4, 8.5)

LLM tier used for guidance composition: "reasoning" (Claude Sonnet 4.6) per
the design's model-tier table — symptom guidance requires pattern recognition
and medical nuance.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.symptom import Symptom
from app.models.user import User

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient
    from telegram import Bot

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Healthcare provider note appended for high-severity symptoms (Req 8.3)
# ---------------------------------------------------------------------------

_HEALTHCARE_PROVIDER_NOTE = (
    "\n\n⚠️ *Please speak with your healthcare provider soon.* "
    "A severity of 8 or above may indicate that medical attention is needed."
)

# ---------------------------------------------------------------------------
# LLM system prompts
# ---------------------------------------------------------------------------

_TREND_REPORT_SYSTEM = """\
You are a compassionate pregnancy symptom assistant.
Given a structured log of symptom entries over the past 90 days (grouped by
symptom name, with dates, severity scores out of 10, and daily frequency),
write a concise, warm trend report for the user.
Highlight symptoms that appear to be worsening (increasing severity or
frequency over time), note any that are improving, and flag consistently
high-severity entries. Keep the tone supportive and free of alarmist language.
Do not provide diagnoses. Length: under 300 words.
"""

_GUIDANCE_SYSTEM = """\
You are a knowledgeable pregnancy symptom assistant.
Using the clinical knowledge context provided, compose clear and reassuring
guidance about the given symptom during pregnancy.
Cover: what is typically considered normal, what warning signs to watch for,
and self-care suggestions commonly recommended for this symptom.
Always include a note encouraging the user to consult their healthcare provider
for personalised advice. Keep the response under 250 words and avoid alarmist
language.
"""

_GUIDANCE_NO_KB_SYSTEM = """\
You are a knowledgeable pregnancy symptom assistant.
The knowledge base is temporarily unavailable. Begin your response with a
brief note that this response is based on general knowledge only, as the
knowledge base is temporarily unavailable.
Compose clear and reassuring guidance about the given symptom during pregnancy:
what is typically considered normal, what warning signs to watch for, and
common self-care suggestions.
Always encourage the user to consult their healthcare provider for personalised
advice. Keep the response under 250 words.
"""


# ---------------------------------------------------------------------------
# Public: trend_report (Req 8.2)
# ---------------------------------------------------------------------------


async def trend_report(
    user_id: int,
    db: AsyncSession,
    llm_client: "LLMClient",
) -> str:
    """
    Retrieve the last 90 days of ``Symptom`` records for *user_id*, group them
    chronologically by symptom name, and produce a formatted trend report.

    The report is composed via ``LLMClient.complete("reasoning", ...)`` so it
    benefits from the reasoning tier's pattern-recognition capability.

    Args:
        user_id:    Internal user PK.
        db:         Active async DB session.
        llm_client: Shared ``LLMClient`` instance.

    Returns:
        A formatted trend report string.  If no symptoms were logged in the
        past 90 days, returns a friendly "no symptoms logged" message without
        calling the LLM.

    Requirements: 8.2
    """
    log = logger.bind(user_id=user_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    stmt = (
        select(Symptom)
        .where(
            Symptom.user_id == user_id,
            Symptom.logged_at >= cutoff,
        )
        .order_by(Symptom.symptom_name.asc(), Symptom.logged_at.asc())
    )
    result = await db.execute(stmt)
    symptoms: list[Symptom] = list(result.scalars().all())

    log.info("trend_report_records_fetched", count=len(symptoms))

    if not symptoms:
        return (
            "No symptoms have been logged in the past 90 days. "
            "When you log symptoms I'll be able to show you trends over time."
        )

    # ── Group by symptom name ────────────────────────────────────────────────
    # Use defaultdict to accumulate entries per symptom name in chronological
    # order (the query already sorts by name then logged_at).
    grouped: dict[str, list[Symptom]] = defaultdict(list)
    for symptom in symptoms:
        grouped[symptom.symptom_name].append(symptom)

    # ── Build a structured text summary for the LLM ─────────────────────────
    lines: list[str] = []
    for name, entries in sorted(grouped.items()):
        lines.append(f"\n### {name}")
        for entry in entries:
            date_str = entry.logged_at.strftime("%Y-%m-%d")
            lines.append(
                f"  {date_str} — severity {entry.severity}/10, "
                f"frequency {entry.frequency}x/day"
            )

    symptom_context = "Symptom log (last 90 days):\n" + "\n".join(lines)

    messages = [
        {"role": "system", "content": _TREND_REPORT_SYSTEM},
        {"role": "user", "content": symptom_context},
    ]

    response = await llm_client.complete("reasoning", messages)
    log.info("trend_report_formatted", tokens_used=response.tokens_used)
    return response.content.strip()


# ---------------------------------------------------------------------------
# Public: severity_alert (Req 8.3)
# ---------------------------------------------------------------------------


async def severity_alert(
    symptom: Symptom,
    bot: "Bot",
) -> None:
    """
    If *symptom* has ``severity >= 8``, look up the owning user's Telegram
    chat ID and send a proactive message appending a healthcare provider note.

    This function is called after a symptom has been confirmed and persisted.
    It resolves the user's ``telegram_user_id`` via the same DB session
    pattern used by ``check_deficiency_alert`` in the Nutrition Assistant.

    The alert is sent as a separate Telegram message (not modifying the
    original confirmation) so the confirmation summary remains clean and the
    provider note is clearly distinct.

    If ``severity < 8`` the function returns immediately without any network
    call or DB lookup.

    Args:
        symptom: The confirmed and persisted ``Symptom`` ORM instance.
                 Must have ``symptom.user_id`` and ``symptom.severity`` set.
        bot:     The ``telegram.Bot`` instance used to send the alert message.

    Requirements: 8.3
    """
    if symptom.severity < 8:
        return

    log = logger.bind(
        user_id=symptom.user_id,
        symptom_id=symptom.id,
        severity=symptom.severity,
    )
    log.info("severity_alert_triggered")

    # Resolve the user's telegram_user_id from the database.
    # We open a new session via the async session factory — the same approach
    # used by check_deficiency_alert in nutrition_assistant.py.
    from app.dependencies import _AsyncSessionFactory  # noqa: PLC0415

    try:
        async with _AsyncSessionFactory() as db:
            user_result = await db.execute(
                select(User).where(User.id == symptom.user_id)
            )
            user: User | None = user_result.scalar_one_or_none()
    except Exception:  # noqa: BLE001
        log.exception("severity_alert_user_lookup_failed")
        return

    if user is None:
        log.warning("severity_alert_user_not_found")
        return

    alert_text = (
        f"ℹ️ Your *{symptom.symptom_name}* was logged with a severity of "
        f"{symptom.severity}/10.{_HEALTHCARE_PROVIDER_NOTE}"
    )

    try:
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=alert_text,
            parse_mode="Markdown",
        )
        log.info("severity_alert_sent")
    except Exception:  # noqa: BLE001
        log.exception("severity_alert_send_failed")


# ---------------------------------------------------------------------------
# Public: compose_guidance (Req 8.4, 8.5)
# ---------------------------------------------------------------------------


async def compose_guidance(
    symptom_name: str,
    llm_client: "LLMClient",
    retriever,  # Callable matching the signature of app.knowledge.retriever.retrieve
    db: AsyncSession,
) -> str:
    """
    Compose knowledge-base-grounded guidance for *symptom_name*.

    Steps
    -----
    1. Attempt to retrieve relevant knowledge chunks for *symptom_name* from
       the ``symptoms`` category of the Knowledge_Base via *retriever* (Req 8.4).
    2. If chunks are found, compose guidance using the Reasoning tier with the
       chunks as grounding context.
    3. If the knowledge base is unavailable (exception raised) or returns no
       chunks, fall back to a general-knowledge response using the Reasoning
       tier while notifying the user that the knowledge base is temporarily
       unavailable (Req 8.5).

    Args:
        symptom_name: The name of the symptom to generate guidance for.
        llm_client:   Shared ``LLMClient`` instance.
        retriever:    The ``retrieve`` coroutine from
                      ``app.knowledge.retriever`` (injected to allow testing
                      without a live database).  Expected signature:
                      ``retrieve(query_text, db, llm_client, top_k, category)``
        db:           Active async DB session (passed through to the retriever).

    Returns:
        A formatted guidance string.

    Requirements: 8.4, 8.5
    """
    from app.models.knowledge_document import KnowledgeCategory  # noqa: PLC0415

    log = logger.bind(symptom_name_len=len(symptom_name))

    # ── Step 1: Retrieve relevant knowledge chunks (Req 8.4) ────────────────
    knowledge_available = True
    knowledge_context = ""

    try:
        chunks = await retriever(
            query_text=f"pregnancy symptom guidance: {symptom_name}",
            db=db,
            llm_client=llm_client,
            top_k=5,
            category=KnowledgeCategory.symptoms,
        )
        if chunks:
            knowledge_context = "\n\n".join(c.content for c in chunks)
            log.info("compose_guidance_kb_hit", chunks_count=len(chunks))
        else:
            knowledge_available = False
            log.info("compose_guidance_kb_empty")
    except Exception:  # noqa: BLE001
        knowledge_available = False
        log.warning("compose_guidance_kb_unavailable")

    # ── Step 2 / 3: Compose guidance (with or without KB context) ───────────
    if knowledge_available and knowledge_context:
        # Knowledge base available — ground response in retrieved chunks (Req 8.4)
        system_prompt = _GUIDANCE_SYSTEM
        user_content = (
            f"Clinical knowledge context:\n{knowledge_context}\n\n"
            f"Please provide pregnancy guidance for the following symptom: "
            f"{symptom_name}"
        )
    else:
        # Graceful fallback — general knowledge, KB unavailable (Req 8.5)
        system_prompt = _GUIDANCE_NO_KB_SYSTEM
        user_content = (
            f"Please provide pregnancy guidance for the following symptom: "
            f"{symptom_name}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = await llm_client.complete("reasoning", messages)
    log.info(
        "compose_guidance_generated",
        knowledge_available=knowledge_available,
        tokens_used=response.tokens_used,
    )
    return response.content.strip()
