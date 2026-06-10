"""
Pregnancy Update Job — daily fact and weekly milestone delivery.

Dispatched daily (typically at 08:00 local time per user, or as a single
batch run) via EventBridge → POST /jobs/pregnancy-update.

Responsibilities:
  1. Query all users with ``onboarding_complete = TRUE``.
  2. For each user, call:
       - ``pregnancy_engine.deliver_daily_fact``   (Req 3.3)
       - ``pregnancy_engine.deliver_weekly_milestone``  (Req 3.2, 3.4)
  3. If content was generated, deliver it to the user via the Telegram bot.
  4. Idempotency is enforced inside the engine functions via
     ``users.last_daily_fact_date`` and ``users.last_milestone_week``;
     this job is therefore safe to invoke multiple times in the same day.

Privacy contract:
  - Never log raw message content, user text, or health data.
  - Log only structural fields: user_id, current_week, tokens_used,
    job_id, error type (not content).

Requirements: 3.2, 3.3, 3.4
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import Bot

from app.components import pregnancy_engine
from app.config import settings
from app.core.llm_client import LLMClient
from app.models.user import User

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------


async def run_pregnancy_update_job(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    llm_client: Optional[LLMClient] = None,
    *,
    job_id: Optional[str] = None,
) -> dict[str, int]:
    """
    Execute the daily pregnancy-update batch for all onboarded users.

    Iterates through every user with ``onboarding_complete = TRUE`` and
    calls :func:`~app.components.pregnancy_engine.deliver_daily_fact` and
    :func:`~app.components.pregnancy_engine.deliver_weekly_milestone`.

    Each engine function enforces its own idempotency guard, so running
    this job multiple times per day is safe — users receive at most one
    daily fact and one weekly milestone per day/week respectively.

    Args:
        bot:             Initialised ``telegram.Bot`` instance used to send
                         generated content to users.
        session_factory: Async SQLAlchemy session factory used to open
                         per-user DB sessions.
        llm_client:      Shared :class:`~app.core.llm_client.LLMClient`
                         instance.  A fresh instance is created if ``None``.
        job_id:          Optional idempotency / tracing identifier for this
                         job run.  A UUID is generated automatically when
                         omitted.

    Returns:
        A summary dict with the following integer counters:
        ``users_processed``, ``facts_sent``, ``milestones_sent``,
        ``users_skipped`` (no due date), ``errors``.

    Requirements: 3.2, 3.3, 3.4
    """
    if job_id is None:
        job_id = str(uuid.uuid4())

    if llm_client is None:
        llm_client = LLMClient()

    log = logger.bind(job="pregnancy_update", job_id=job_id)
    log.info("job_started")

    counters = {
        "users_processed": 0,
        "facts_sent": 0,
        "milestones_sent": 0,
        "users_skipped": 0,
        "errors": 0,
    }

    # Fetch all onboarded user IDs in a single query, then process each
    # user in its own session to keep transactions short and avoid
    # holding a connection open for the entire batch duration.
    async with session_factory() as read_session:
        user_ids: list[int] = list(
            (
                await read_session.execute(
                    select(User.id).where(User.onboarding_complete.is_(True))
                )
            ).scalars()
        )

    log.info("users_fetched", count=len(user_ids))

    for user_id in user_ids:
        await _process_user(
            user_id=user_id,
            bot=bot,
            session_factory=session_factory,
            llm_client=llm_client,
            counters=counters,
            log=log,
        )

    log.info(
        "job_completed",
        users_processed=counters["users_processed"],
        facts_sent=counters["facts_sent"],
        milestones_sent=counters["milestones_sent"],
        users_skipped=counters["users_skipped"],
        errors=counters["errors"],
    )

    return counters


# ---------------------------------------------------------------------------
# Per-user processing helper
# ---------------------------------------------------------------------------


async def _process_user(
    *,
    user_id: int,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    llm_client: LLMClient,
    counters: dict[str, int],
    log: structlog.BoundLogger,  # type: ignore[type-arg]
) -> None:
    """
    Process a single user within its own DB session and transaction.

    Opens a dedicated async session so that a failure for one user does
    not affect other users in the batch.  The session is committed after
    both engine calls succeed, persisting the updated
    ``last_daily_fact_date`` and ``last_milestone_week`` markers.

    Args:
        user_id:         Primary key of the user to process.
        bot:             Telegram bot used to deliver generated content.
        session_factory: Session factory for creating a per-user session.
        llm_client:      Shared LLM client.
        counters:        Mutable counter dict updated in-place.
        log:             Bound structlog logger with job context.
    """
    user_log = log.bind(user_id=user_id)

    try:
        async with session_factory() as db:
            # Load the user inside this session so ORM mutations are tracked.
            user: Optional[User] = await db.get(User, user_id)
            if user is None:
                user_log.warning("user_not_found_in_batch")
                counters["errors"] += 1
                return

            if user.due_date is None:
                user_log.debug("user_skipped_no_due_date")
                counters["users_skipped"] += 1
                return

            counters["users_processed"] += 1

            # ── 1. Daily fact ────────────────────────────────────────────
            fact_text = await _deliver_fact(
                user=user,
                db=db,
                bot=bot,
                llm_client=llm_client,
                counters=counters,
                log=user_log,
            )

            # ── 2. Weekly milestone ──────────────────────────────────────
            await _deliver_milestone(
                user=user,
                db=db,
                bot=bot,
                llm_client=llm_client,
                counters=counters,
                log=user_log,
                fact_already_sent=(fact_text is not None),
            )

            # Commit both idempotency marker updates atomically.
            await db.commit()

    except Exception as exc:  # noqa: BLE001
        # Log the error type but never the content to stay compliant with
        # the privacy contract (Req 16.1, 16.2).
        user_log.error(
            "user_processing_failed",
            error_type=type(exc).__name__,
        )
        counters["errors"] += 1


async def _deliver_fact(
    *,
    user: User,
    db: AsyncSession,
    bot: Bot,
    llm_client: LLMClient,
    counters: dict[str, int],
    log: structlog.BoundLogger,  # type: ignore[type-arg]
) -> Optional[str]:
    """
    Generate today's daily fact and send it to the user via Telegram.

    The engine function handles the ``last_daily_fact_date`` idempotency
    guard internally; if the fact has already been delivered today it
    returns ``None`` and this function does nothing.

    Args:
        user:       ORM ``User`` instance attached to *db*.
        db:         Active async DB session (within a transaction).
        bot:        Telegram bot for message delivery.
        llm_client: Shared LLM client.
        counters:   Mutable counter dict updated in-place.
        log:        Bound structlog logger with user context.

    Returns:
        The generated fact text, or ``None`` if already delivered today or
        if delivery failed.

    Requirements: 3.3
    """
    fact_text: Optional[str] = await pregnancy_engine.deliver_daily_fact(
        user=user,
        db=db,
        llm_client=llm_client,
    )

    if fact_text is None:
        # Already delivered today — engine skipped generation.
        log.debug("daily_fact_already_delivered_skipping_send")
        return None

    try:
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=fact_text,
        )
        counters["facts_sent"] += 1
        log.info("daily_fact_sent")
    except Exception as exc:  # noqa: BLE001
        log.error(
            "daily_fact_send_failed",
            error_type=type(exc).__name__,
        )
        counters["errors"] += 1
        # Roll back the last_daily_fact_date update so the engine will
        # retry delivery on the next job run.
        user.last_daily_fact_date = None
        return None

    return fact_text


async def _deliver_milestone(
    *,
    user: User,
    db: AsyncSession,
    bot: Bot,
    llm_client: LLMClient,
    counters: dict[str, int],
    log: structlog.BoundLogger,  # type: ignore[type-arg]
    fact_already_sent: bool,
) -> Optional[str]:
    """
    Generate the weekly milestone message and send it to the user via Telegram.

    The engine function handles the ``last_milestone_week`` idempotency
    guard internally; if the milestone has already been delivered this
    week it returns ``None`` and this function does nothing.

    Args:
        user:              ORM ``User`` instance attached to *db*.
        db:                Active async DB session (within a transaction).
        bot:               Telegram bot for message delivery.
        llm_client:        Shared LLM client.
        counters:          Mutable counter dict updated in-place.
        log:               Bound structlog logger with user context.
        fact_already_sent: Whether a daily fact was just sent in the same
                           invocation (used for debug context only).

    Returns:
        The generated milestone text, or ``None`` if already delivered
        this week or if delivery failed.

    Requirements: 3.2, 3.4
    """
    milestone_text: Optional[str] = await pregnancy_engine.deliver_weekly_milestone(
        user=user,
        db=db,
        llm_client=llm_client,
    )

    if milestone_text is None:
        # Already delivered this week — engine skipped generation.
        log.debug(
            "weekly_milestone_already_delivered_skipping_send",
            fact_also_skipped=(not fact_already_sent),
        )
        return None

    try:
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=milestone_text,
        )
        counters["milestones_sent"] += 1
        log.info("weekly_milestone_sent")
    except Exception as exc:  # noqa: BLE001
        log.error(
            "weekly_milestone_send_failed",
            error_type=type(exc).__name__,
        )
        counters["errors"] += 1
        # Roll back the last_milestone_week update so the engine will
        # retry delivery on the next job run.
        user.last_milestone_week = None
        return None

    return milestone_text


# ---------------------------------------------------------------------------
# Standalone runner (for direct invocation or testing)
# ---------------------------------------------------------------------------


async def _run_standalone() -> None:
    """
    Entry point for running the job as a standalone async script.

    Creates its own engine, session factory, LLM client, and Telegram bot
    from the application ``settings``, then executes the job.

    Usage (from repo root)::

        python -m app.jobs.pregnancy_update_job
    """
    log = logger.bind(job="pregnancy_update", mode="standalone")

    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    bot = Bot(token=settings.telegram_bot_token)
    llm_client = LLMClient()

    try:
        result = await run_pregnancy_update_job(
            bot=bot,
            session_factory=factory,
            llm_client=llm_client,
        )
        log.info("standalone_run_complete", **result)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run_standalone())
