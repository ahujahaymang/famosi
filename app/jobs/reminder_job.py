"""
Reminder delivery job for Famosi.

Queries the ``reminders`` table for all due, undelivered reminders and
dispatches them to users via Telegram.  On a delivery failure the job waits
60 seconds and retries once.  Persistent failures are marked ``failed=TRUE``
and logged at ERROR level with the full structured context required by
Requirement 10.8.

Delivery logic (Req 10.3, 10.8)
---------------------------------
1. SELECT reminders WHERE scheduled_at <= now() AND active = TRUE
   AND delivered = FALSE AND failed = FALSE
2. For each due reminder:
   a. Attempt Telegram delivery.
   b. SUCCESS → set delivered=TRUE, last_attempt_at=now()
   c. FAILURE → wait 60 s, retry once
   d. RETRY FAILURE → set failed=TRUE, log ERROR with request_id,
      user_id, reminder_id, timestamp

Privacy contract
----------------
NEVER log ``message_text`` or any health-identifying content.
Log only structural fields: user_id, reminder_id, reminder_type,
scheduled_at, request_id, timestamp.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import _AsyncSessionFactory
from app.models.reminder import Reminder
from app.models.user import User

logger = structlog.get_logger(__name__)

# Seconds to wait before the single retry attempt (Req 10.8)
_RETRY_DELAY_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_due_reminders(db: AsyncSession) -> list[Reminder]:
    """
    Return all reminders that are due for delivery.

    A reminder is due when:
      - ``scheduled_at <= now()``  — the scheduled time has been reached
      - ``active = TRUE``          — the reminder has not been cancelled
      - ``delivered = FALSE``      — not yet successfully delivered
      - ``failed = FALSE``         — not permanently failed

    The partial index ``idx_reminders_scheduled`` on the ``reminders`` table
    covers exactly this predicate, making this query efficient.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Reminder).where(
            Reminder.scheduled_at <= now,
            Reminder.active.is_(True),
            Reminder.delivered.is_(False),
            Reminder.failed.is_(False),
        )
    )
    return list(result.scalars().all())


async def _resolve_telegram_user_id(user_id: int, db: AsyncSession) -> int | None:
    """
    Return the Telegram chat ID for an internal *user_id*.

    Returns ``None`` when the user record cannot be found (e.g. the user was
    deleted after the reminder was created).
    """
    result = await db.execute(
        select(User.telegram_user_id).where(User.id == user_id)
    )
    return result.scalar_one_or_none()


async def _attempt_delivery(bot, telegram_user_id: int, message_text: str) -> None:
    """
    Send a Telegram message via ``bot.send_message``.

    Raises any exception raised by the Telegram API so that the caller can
    decide how to handle it (retry, mark failed, etc.).
    """
    await bot.send_message(
        chat_id=telegram_user_id,
        text=message_text,
    )


# ---------------------------------------------------------------------------
# Per-reminder delivery with retry
# ---------------------------------------------------------------------------


async def _deliver_reminder(
    bot,
    reminder: Reminder,
    db: AsyncSession,
) -> None:
    """
    Deliver a single reminder and update its DB state.

    Delivery flow:
      1. Resolve the user's ``telegram_user_id``.
      2. Attempt delivery.
      3a. SUCCESS → mark ``delivered=TRUE``, ``last_attempt_at=now()``.
      3b. FAILURE → wait ``_RETRY_DELAY_SECONDS``, retry once.
      4a. RETRY SUCCESS → mark ``delivered=TRUE``, ``last_attempt_at=now()``.
      4b. RETRY FAILURE → mark ``failed=TRUE``, ``last_attempt_at=now()``,
          log ERROR with request_id, user_id, reminder_id, timestamp (Req 10.8).

    All state mutations are flushed to the session but NOT committed here —
    the caller (``run_reminder_job``) commits after processing each reminder
    to keep individual reminder failures isolated.
    """
    request_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    log = logger.bind(
        request_id=request_id,
        user_id=reminder.user_id,
        reminder_id=reminder.id,
        reminder_type=reminder.reminder_type.value,
        scheduled_at=reminder.scheduled_at.isoformat() if reminder.scheduled_at else None,
        timestamp=now.isoformat(),
    )

    # -- 1. Resolve telegram_user_id ----------------------------------------
    telegram_user_id = await _resolve_telegram_user_id(reminder.user_id, db)
    if telegram_user_id is None:
        log.error(
            "reminder_delivery_user_not_found",
            hint="User was deleted after the reminder was created; marking failed",
        )
        reminder.failed = True
        reminder.last_attempt_at = now
        await db.flush()
        return

    # -- 2. First delivery attempt ------------------------------------------
    try:
        await _attempt_delivery(bot, telegram_user_id, reminder.message_text)
    except Exception as first_exc:  # noqa: BLE001
        log.warning(
            "reminder_delivery_attempt_1_failed",
            error_type=type(first_exc).__name__,
        )
        # -- 3b. Wait then retry --------------------------------------------
        await asyncio.sleep(_RETRY_DELAY_SECONDS)

        retry_now = datetime.now(timezone.utc)
        try:
            await _attempt_delivery(bot, telegram_user_id, reminder.message_text)
        except Exception as retry_exc:  # noqa: BLE001
            # -- 4b. Retry failed — permanent failure -----------------------
            reminder.failed = True
            reminder.last_attempt_at = retry_now
            await db.flush()

            # Req 10.8: log ERROR with request_id, user_id, reminder_id, timestamp
            log.error(
                "reminder_delivery_failed",
                error_type=type(retry_exc).__name__,
                retry_timestamp=retry_now.isoformat(),
            )
            return

        # -- 4a. Retry succeeded -------------------------------------------
        reminder.delivered = True
        reminder.last_attempt_at = retry_now
        await db.flush()

        log.info("reminder_delivered_on_retry")
        return

    # -- 3a. First attempt succeeded ----------------------------------------
    reminder.delivered = True
    reminder.last_attempt_at = now
    await db.flush()

    log.info("reminder_delivered")


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------


async def run_reminder_job() -> dict[str, int]:
    """
    Query all due reminders and dispatch them to users via Telegram.

    Called from the ``POST /jobs/reminders`` HTTP endpoint (task 28.5) which
    is triggered by AWS EventBridge every minute.

    Returns a summary dict with counts for operational visibility::

        {
            "due": 5,
            "delivered": 4,
            "failed": 1,
        }

    Each reminder is committed in its own transaction so that a failure on
    one reminder does not roll back deliveries for others.
    """
    from telegram import Bot  # local import — not available in all test environments

    bot = Bot(token=settings.telegram_bot_token)

    delivered_count = 0
    failed_count = 0

    async with _AsyncSessionFactory() as db:
        due_reminders = await _fetch_due_reminders(db)

    due_count = len(due_reminders)

    job_log = logger.bind(due_count=due_count)
    job_log.info("reminder_job_started")

    for reminder in due_reminders:
        async with _AsyncSessionFactory() as db:
            # Re-load the reminder in this session to get a live ORM object
            # that we can mutate and commit independently.
            result = await db.execute(
                select(Reminder).where(Reminder.id == reminder.id)
            )
            live_reminder: Reminder | None = result.scalar_one_or_none()

            if live_reminder is None:
                # Already deleted between the bulk query and this iteration
                continue

            # Double-check it is still pending (another worker may have
            # delivered it between the bulk SELECT and now).
            if live_reminder.delivered or live_reminder.failed or not live_reminder.active:
                continue

            await _deliver_reminder(bot, live_reminder, db)

            if live_reminder.delivered:
                delivered_count += 1
            elif live_reminder.failed:
                failed_count += 1

            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                logger.error(
                    "reminder_job_commit_failed",
                    reminder_id=reminder.id,
                    error_type=type(exc).__name__,
                )

    job_log.info(
        "reminder_job_completed",
        delivered=delivered_count,
        failed=failed_count,
    )

    return {"due": due_count, "delivered": delivered_count, "failed": failed_count}


__all__ = ["run_reminder_job"]
