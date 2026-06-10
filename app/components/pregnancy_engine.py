"""
Pregnancy Engine — gestational age calculation, daily facts, and weekly milestones.

Handles:
  - Gestational age computation from due date or LMP
  - Daily fact delivery with idempotency via `users.last_daily_fact_date`
  - Weekly milestone delivery with idempotency via `users.last_milestone_week`
  - Partner-specific guidance sourced from `dad_support` knowledge chunks
  - Due-date and LMP validation (re-prompt boundaries)

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 1.2, 1.3
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm_client import LLMClient
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeCategory, KnowledgeDocument
from app.models.user import User, UserRole

logger = structlog.get_logger(__name__)

# ── validation constants ─────────────────────────────────────────────────────

#: Minimum number of days a due date must be in the future (Req 1.2)
DUE_DATE_MIN_DAYS_FUTURE: int = 1
#: Maximum number of days a due date may be in the future (Req 1.2)
DUE_DATE_MAX_DAYS_FUTURE: int = 280

#: Minimum number of days an LMP must be in the past (Req 1.3)
LMP_MIN_DAYS_PAST: int = 1
#: Maximum number of days an LMP may be in the past (Req 1.3)
LMP_MAX_DAYS_PAST: int = 280

#: Standard pregnancy length in days (40 weeks)
PREGNANCY_DAYS: int = 280

# ── gestational age ──────────────────────────────────────────────────────────


def calculate_gestational_age(due_date: date, today: date) -> tuple[int, int]:
    """
    Return the current gestational age as (weeks, days).

    Formula (from design §6.4):
        total_days = 280 - (due_date - today).days
        weeks      = total_days // 7
        days       = total_days % 7

    Args:
        due_date: The user's stored due date.
        today:    The reference date (normally today in UTC).

    Returns:
        A ``(weeks, days)`` tuple where ``weeks * 7 + days == total_days``.
        Both values are non-negative; weeks can exceed 40 if the due date
        has already passed.

    Requirements: 3.1, 1.2, 1.3
    """
    days_until_due = (due_date - today).days
    total_days = PREGNANCY_DAYS - days_until_due
    weeks = total_days // 7
    days = total_days % 7
    return weeks, days


def get_current_week(due_date: date, today: date) -> int:
    """
    Return the completed gestational week number (integer, 0-indexed).

    This is the ``weeks`` component of :func:`calculate_gestational_age`
    and represents the last fully completed week of pregnancy.

    Args:
        due_date: The user's stored due date.
        today:    The reference date (normally today in UTC).

    Returns:
        The completed week number (e.g. 12 during week 12+N days).
    """
    weeks, _days = calculate_gestational_age(due_date, today)
    return weeks


# ── validation helpers ───────────────────────────────────────────────────────


class DueDateValidationError(ValueError):
    """Raised when a proposed due date falls outside the 1-280 day window."""


class LMPValidationError(ValueError):
    """Raised when a proposed LMP falls outside the 1-280 day window."""


def validate_due_date(due_date: date, today: date) -> None:
    """
    Assert that *due_date* is between 1 and 280 days in the future.

    Raises:
        DueDateValidationError: with a human-readable re-prompt message
            when the validation fails (Req 1.2).
    """
    days_in_future = (due_date - today).days
    if not (DUE_DATE_MIN_DAYS_FUTURE <= days_in_future <= DUE_DATE_MAX_DAYS_FUTURE):
        raise DueDateValidationError(
            f"Due date must be between {DUE_DATE_MIN_DAYS_FUTURE} and "
            f"{DUE_DATE_MAX_DAYS_FUTURE} days from today. "
            f"Please enter a valid due date."
        )


def validate_lmp(lmp_date: date, today: date) -> None:
    """
    Assert that *lmp_date* is between 1 and 280 days in the past.

    Raises:
        LMPValidationError: with a human-readable re-prompt message
            when the validation fails (Req 1.3).
    """
    days_in_past = (today - lmp_date).days
    if not (LMP_MIN_DAYS_PAST <= days_in_past <= LMP_MAX_DAYS_PAST):
        raise LMPValidationError(
            f"LMP must be between {LMP_MIN_DAYS_PAST} and "
            f"{LMP_MAX_DAYS_PAST} days in the past. "
            f"Please enter a valid last menstrual period date."
        )


def lmp_to_due_date(lmp_date: date) -> date:
    """
    Estimate the due date by adding 280 days to the LMP (Req 1.3).

    Args:
        lmp_date: The user's last menstrual period date.

    Returns:
        The estimated due date.
    """
    return lmp_date + timedelta(days=PREGNANCY_DAYS)


# ── knowledge chunk retrieval helper ────────────────────────────────────────


async def _retrieve_dad_support_chunks(
    db: AsyncSession,
    current_week: int,
) -> list[str]:
    """
    Fetch ``dad_support`` knowledge chunk contents relevant to *current_week*.

    The query joins ``knowledge_chunks`` to ``knowledge_documents`` and
    filters on ``category = 'dad_support'`` and ``active = TRUE``.
    The week number is searched for in the chunk content as a simple heuristic
    (``WEEK <N>`` or ``week <N>``).  All matching chunks are returned; the
    caller decides how many to use.

    Args:
        db:           Active async DB session.
        current_week: Current completed gestational week.

    Returns:
        A list of chunk content strings (may be empty if none found).
    """
    stmt = (
        select(KnowledgeChunk.content)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.category == KnowledgeCategory.dad_support,
            KnowledgeDocument.active.is_(True),
        )
        .limit(5)
    )
    result = await db.execute(stmt)
    chunks: list[str] = list(result.scalars().all())

    # Prefer chunks that mention the current week number explicitly
    week_str = str(current_week)
    prioritised = [c for c in chunks if week_str in c]
    return prioritised if prioritised else chunks


async def _retrieve_gestational_age_chunks(
    db: AsyncSession,
    current_week: int,
) -> list[str]:
    """
    Fetch ``baby_development`` knowledge chunk contents for *current_week*.

    Args:
        db:           Active async DB session.
        current_week: Current completed gestational week.

    Returns:
        A list of chunk content strings (may be empty if none found).
    """
    stmt = (
        select(KnowledgeChunk.content)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.category == KnowledgeCategory.baby_development,
            KnowledgeDocument.active.is_(True),
        )
        .limit(5)
    )
    result = await db.execute(stmt)
    chunks: list[str] = list(result.scalars().all())

    # Prefer chunks that mention the current week number explicitly
    week_str = str(current_week)
    prioritised = [c for c in chunks if week_str in c]
    return prioritised if prioritised else chunks


# ── daily fact delivery ──────────────────────────────────────────────────────

_GENERIC_DAILY_FACT_SYSTEM = (
    "You are a warm, knowledgeable pregnancy companion. "
    "Generate a single, concise, evidence-based baby development fact for "
    "the gestational age provided. Keep the tone encouraging and the length "
    "under 120 words. Do not use alarming language."
)

_GENERIC_DAILY_FACT_WITH_CONTEXT_SYSTEM = (
    "You are a warm, knowledgeable pregnancy companion. "
    "Using the knowledge context provided, generate a single, concise, "
    "evidence-based baby development fact for the gestational age given. "
    "Ground your answer in the context. Keep the tone encouraging and the "
    "length under 120 words. Do not use alarming language."
)


async def deliver_daily_fact(
    user: User,
    db: AsyncSession,
    llm_client: LLMClient,
) -> Optional[str]:
    """
    Generate and return today's baby development fact for *user*.

    Idempotency: if ``user.last_daily_fact_date`` equals today (UTC) the
    function returns ``None`` without calling the LLM (Req 3.3).

    Steps:
    1. Compute today's UTC date.
    2. Skip if already delivered today.
    3. Calculate gestational age from ``user.due_date``.
    4. Retrieve ``baby_development`` knowledge chunks for grounding.
    5. Call ``LLMClient.complete("reasoning", ...)`` to generate the fact.
    6. Update ``user.last_daily_fact_date`` and flush.

    Args:
        user:       The ORM ``User`` instance (attached to *db* session).
        db:         Active async DB session.
        llm_client: Shared ``LLMClient`` instance.

    Returns:
        The generated fact text, or ``None`` if already delivered today.

    Requirements: 3.3
    """
    today_utc: date = datetime.now(timezone.utc).date()

    # ── idempotency guard ────────────────────────────────────────────────────
    if user.last_daily_fact_date == today_utc:
        logger.debug(
            "daily_fact_already_delivered",
            user_id=user.id,
            date=str(today_utc),
        )
        return None

    if user.due_date is None:
        logger.warning("daily_fact_skipped_no_due_date", user_id=user.id)
        return None

    weeks, days = calculate_gestational_age(user.due_date, today_utc)

    log = logger.bind(user_id=user.id, weeks=weeks, days=days)
    log.info("delivering_daily_fact")

    # ── retrieve grounding knowledge ─────────────────────────────────────────
    chunks = await _retrieve_gestational_age_chunks(db, weeks)

    if chunks:
        context_block = "\n\n".join(chunks)
        system_prompt = _GENERIC_DAILY_FACT_WITH_CONTEXT_SYSTEM
        user_content = (
            f"Knowledge context:\n{context_block}\n\n"
            f"Current gestational age: {weeks} weeks and {days} days. "
            f"Generate today's baby development fact."
        )
    else:
        system_prompt = _GENERIC_DAILY_FACT_SYSTEM
        user_content = (
            f"Current gestational age: {weeks} weeks and {days} days. "
            f"Generate today's baby development fact."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = await llm_client.complete("reasoning", messages)
    fact_text = response.content.strip()

    # ── update idempotency marker ─────────────────────────────────────────────
    user.last_daily_fact_date = today_utc
    await db.flush()

    log.info("daily_fact_delivered", tokens_used=response.tokens_used)
    return fact_text


# ── weekly milestone delivery ────────────────────────────────────────────────

_MILESTONE_MOM_SYSTEM = (
    "You are a warm, knowledgeable pregnancy companion. "
    "Generate a weekly milestone message describing the baby's development "
    "and what Mom might be experiencing physically or emotionally. "
    "Keep the tone encouraging, the length under 150 words, and do not "
    "use alarming language."
)

_MILESTONE_MOM_WITH_CONTEXT_SYSTEM = (
    "You are a warm, knowledgeable pregnancy companion. "
    "Using the knowledge context provided, generate a weekly milestone message "
    "describing the baby's development and what Mom might be experiencing. "
    "Ground your answer in the context. Keep the tone encouraging, the length "
    "under 150 words, and do not use alarming language."
)

_MILESTONE_PARTNER_SYSTEM = (
    "You are a warm, knowledgeable pregnancy companion. "
    "Generate a weekly partner/dad support message describing how to support "
    "Mom during the current pregnancy week. "
    "Keep the tone warm and practical, under 150 words."
)

_MILESTONE_PARTNER_WITH_CONTEXT_SYSTEM = (
    "You are a warm, knowledgeable pregnancy companion. "
    "Using the knowledge context provided, generate a weekly partner/dad support "
    "message describing how to support Mom this week. "
    "Ground your answer in the context. Keep the tone warm and practical, "
    "under 150 words."
)

_GENERIC_PARTNER_FALLBACK = (
    "This week is a special milestone in your pregnancy journey. "
    "As a partner, your support, presence, and understanding mean everything. "
    "Consider checking in with Mom, helping with tasks that feel tiring, and "
    "celebrating this moment together. You're both doing great."
)


async def deliver_weekly_milestone(
    user: User,
    db: AsyncSession,
    llm_client: LLMClient,
) -> Optional[str]:
    """
    Generate and return the weekly milestone message for *user*.

    Idempotency: if ``user.last_milestone_week`` equals (or exceeds) the
    current gestational week the function returns ``None`` (Req 3.2).

    For Partner users the content is sourced from ``dad_support`` knowledge
    chunks; if no chunks are available a generic support message is returned
    instead (Req 3.4).

    Steps:
    1. Calculate current gestational week from ``user.due_date``.
    2. Skip if already delivered for this week.
    3. For Partner: retrieve ``dad_support`` chunks; fall back to generic.
    4. For Mom: retrieve ``baby_development`` chunks.
    5. Call ``LLMClient.complete("reasoning", ...)`` to generate the milestone.
    6. Update ``user.last_milestone_week`` and flush.

    Args:
        user:       The ORM ``User`` instance (attached to *db* session).
        db:         Active async DB session.
        llm_client: Shared ``LLMClient`` instance.

    Returns:
        The milestone text, or ``None`` if already delivered this week.

    Requirements: 3.2, 3.4
    """
    today_utc: date = datetime.now(timezone.utc).date()

    if user.due_date is None:
        logger.warning("weekly_milestone_skipped_no_due_date", user_id=user.id)
        return None

    current_week = get_current_week(user.due_date, today_utc)

    # ── idempotency guard ────────────────────────────────────────────────────
    if (
        user.last_milestone_week is not None
        and user.last_milestone_week >= current_week
    ):
        logger.debug(
            "weekly_milestone_already_delivered",
            user_id=user.id,
            current_week=current_week,
            last_milestone_week=user.last_milestone_week,
        )
        return None

    log = logger.bind(
        user_id=user.id,
        current_week=current_week,
        role=user.role,
    )
    log.info("delivering_weekly_milestone")

    is_partner = user.role == UserRole.partner

    if is_partner:
        milestone_text = await _deliver_partner_milestone(
            user, db, llm_client, current_week, log
        )
    else:
        milestone_text = await _deliver_mom_milestone(
            user, db, llm_client, current_week, today_utc, log
        )

    # ── update idempotency marker ─────────────────────────────────────────────
    user.last_milestone_week = current_week
    await db.flush()

    log.info("weekly_milestone_delivered")
    return milestone_text


async def _deliver_partner_milestone(
    user: User,
    db: AsyncSession,
    llm_client: LLMClient,
    current_week: int,
    log: structlog.BoundLogger,  # type: ignore[type-arg]
) -> str:
    """
    Generate partner-specific milestone content (Req 3.4).

    Falls back to a generic support message when no ``dad_support`` chunks
    are available in the Knowledge_Base.
    """
    chunks = await _retrieve_dad_support_chunks(db, current_week)

    if not chunks:
        log.info(
            "partner_milestone_fallback_to_generic",
            reason="no_dad_support_chunks",
        )
        return _GENERIC_PARTNER_FALLBACK

    context_block = "\n\n".join(chunks)
    system_prompt = _MILESTONE_PARTNER_WITH_CONTEXT_SYSTEM
    user_content = (
        f"Knowledge context:\n{context_block}\n\n"
        f"Current gestational week: {current_week}. "
        f"Generate a partner/dad support milestone message for this week."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = await llm_client.complete("reasoning", messages)
    log.info("partner_milestone_generated", tokens_used=response.tokens_used)
    return response.content.strip()


async def _deliver_mom_milestone(
    user: User,
    db: AsyncSession,
    llm_client: LLMClient,
    current_week: int,
    today_utc: date,
    log: structlog.BoundLogger,  # type: ignore[type-arg]
) -> str:
    """
    Generate Mom-facing weekly milestone content (Req 3.2).
    """
    assert user.due_date is not None  # guaranteed by caller
    _weeks, days = calculate_gestational_age(user.due_date, today_utc)
    chunks = await _retrieve_gestational_age_chunks(db, current_week)

    if chunks:
        context_block = "\n\n".join(chunks)
        system_prompt = _MILESTONE_MOM_WITH_CONTEXT_SYSTEM
        user_content = (
            f"Knowledge context:\n{context_block}\n\n"
            f"Current gestational age: {current_week} weeks and {days} days. "
            f"Generate the weekly milestone message."
        )
    else:
        system_prompt = _MILESTONE_MOM_SYSTEM
        user_content = (
            f"Current gestational age: {current_week} weeks and {days} days. "
            f"Generate the weekly milestone message."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = await llm_client.complete("reasoning", messages)
    log.info("mom_milestone_generated", tokens_used=response.tokens_used)
    return response.content.strip()
