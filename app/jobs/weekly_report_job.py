"""
Weekly Report Job — nutrition trend + symptom trend digest for active users.

Triggered by AWS EventBridge (or POST /jobs/weekly-report) once per week.

For each active user (subscription_status IN ('trial', 'active', 'grace')
with onboarding_complete=TRUE):
  1. Generate a weekly nutrition trend digest via
     ``nutrition_assistant.weekly_trend`` (Req 7.3)
  2. Generate a 90-day symptom trend digest via
     ``symptom_assistant.trend_report`` (Req 8.2)
  3. Deliver both reports to the user via the Telegram bot

Users with inactive subscriptions are skipped — they do not receive weekly
digests.  Users who have not completed onboarding are also skipped because
they have no health records to summarise.

The job is idempotent: each digest is generated on-demand from the current
state of the database, so re-running the job for the same week produces
consistent results without creating duplicate records.

Privacy contract:
  - Nutrient percentages and symptom trend summaries are passed to the LLM;
    structlog processors strip health-data fields before any logging occurs.
  - User PII (telegram_user_id) is only used internally to route the Telegram
    message and is never logged.

Requirements: 16.3
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.components import nutrition_assistant, symptom_assistant
from app.core.llm_client import LLMClient
from app.dependencies import _AsyncSessionFactory
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User

logger = structlog.get_logger(__name__)

# Subscription statuses that qualify a user to receive the weekly report.
_ACTIVE_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.trial,
        SubscriptionStatus.active,
        SubscriptionStatus.grace,
    }
)

# Header prepended to the combined weekly digest message sent to the user.
_DIGEST_HEADER = (
    "📊 *Your Weekly Pregnancy Digest*\n\n"
    "Here's a summary of your nutrition and symptoms over the past week."
)

_NUTRITION_SECTION_HEADER = "\n\n─────────────────────\n🥗 *Nutrition Trends*\n─────────────────────\n"
_SYMPTOM_SECTION_HEADER = "\n\n─────────────────────\n🩺 *Symptom Trends*\n─────────────────────\n"


async def run_weekly_report_job(bot) -> dict:
    """
    Entry point for the weekly report job.

    Fetches all eligible users from the database and, for each one, generates
    and delivers a combined nutrition + symptom digest via the Telegram bot.

    Args:
        bot: A ``telegram.Bot`` instance used to send messages to users.

    Returns:
        A summary dict with the fields ``users_processed``, ``users_skipped``,
        and ``users_failed`` for operator observability.

    Requirements: 16.3
    """
    log = logger.bind(job="weekly_report")
    log.info("weekly_report_job_started")

    llm_client = LLMClient()
    users_processed = 0
    users_skipped = 0
    users_failed = 0

    async with _AsyncSessionFactory() as db:
        users = await _fetch_active_users(db)
        log.info("weekly_report_users_fetched", count=len(users))

        for user in users:
            success = await _deliver_report_for_user(user, db, llm_client, bot, log)
            if success is True:
                users_processed += 1
            elif success is False:
                users_failed += 1
            else:
                users_skipped += 1

    summary = {
        "users_processed": users_processed,
        "users_skipped": users_skipped,
        "users_failed": users_failed,
    }
    log.info("weekly_report_job_complete", **summary)
    return summary


async def _fetch_active_users(db: AsyncSession) -> list[User]:
    """
    Return all users that should receive a weekly report.

    Criteria:
      - ``onboarding_complete = TRUE``
      - Joined subscription record with ``subscription_status`` IN
        ('trial', 'active', 'grace')

    Args:
        db: Active async DB session.

    Returns:
        List of ``User`` ORM instances.
    """
    stmt = (
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.onboarding_complete.is_(True),
            Subscription.subscription_status.in_(
                [s.value for s in _ACTIVE_STATUSES]
            ),
        )
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _deliver_report_for_user(
    user: User,
    db: AsyncSession,
    llm_client: LLMClient,
    bot,
    log,
) -> bool | None:
    """
    Generate and deliver the weekly digest for a single user.

    Generates the nutrition trend report and the symptom trend report
    independently.  Even if one report generation fails, delivery proceeds
    with the successfully generated section and a note about the unavailable
    section.

    Args:
        user:       ``User`` ORM instance.
        db:         Active async DB session (shared across the job run).
        llm_client: Shared ``LLMClient`` instance.
        bot:        ``telegram.Bot`` instance.
        log:        Bound structlog logger.

    Returns:
        ``True`` if the message was delivered successfully,
        ``False`` if delivery failed,
        ``None`` if the user was skipped for a non-error reason.
    """
    user_log = log.bind(user_id=user.id)

    # ── 1. Generate nutrition weekly trend (Req 7.3) ─────────────────────
    nutrition_text: str | None = None
    try:
        nutrition_text = await nutrition_assistant.weekly_trend(
            user_id=user.id,
            db=db,
            llm_client=llm_client,
        )
        user_log.info("weekly_nutrition_trend_generated")
    except Exception:
        user_log.exception("weekly_nutrition_trend_failed")

    # ── 2. Generate symptom trend report (Req 8.2) ───────────────────────
    symptom_text: str | None = None
    try:
        symptom_text = await symptom_assistant.trend_report(
            user_id=user.id,
            db=db,
            llm_client=llm_client,
        )
        user_log.info("weekly_symptom_trend_generated")
    except Exception:
        user_log.exception("weekly_symptom_trend_failed")

    # If both reports failed, skip this user
    if nutrition_text is None and symptom_text is None:
        user_log.warning("weekly_report_both_sections_failed_skipping_user")
        return False

    # ── 3. Compose the digest message ─────────────────────────────────────
    parts: list[str] = [_DIGEST_HEADER]

    if nutrition_text is not None:
        parts.append(_NUTRITION_SECTION_HEADER + nutrition_text)
    else:
        parts.append(
            _NUTRITION_SECTION_HEADER
            + "Nutrition trend data is temporarily unavailable. "
            "Please try requesting your daily summary manually."
        )

    if symptom_text is not None:
        parts.append(_SYMPTOM_SECTION_HEADER + symptom_text)
    else:
        parts.append(
            _SYMPTOM_SECTION_HEADER
            + "Symptom trend data is temporarily unavailable. "
            "Please try requesting your symptom report manually."
        )

    digest_message = "\n".join(parts)

    # ── 4. Deliver via Telegram bot ───────────────────────────────────────
    try:
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=digest_message,
            parse_mode="Markdown",
        )
        user_log.info("weekly_report_delivered")
        return True
    except Exception:
        user_log.exception("weekly_report_delivery_failed")
        return False
