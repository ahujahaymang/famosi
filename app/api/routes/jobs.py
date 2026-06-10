"""
FastAPI router for scheduled job trigger endpoints.

These endpoints are called exclusively by AWS EventBridge (or an equivalent
scheduler) to kick off background jobs.  They are **never** called by
Telegram users or payment providers.

Endpoints
---------
POST /jobs/reminders
    Triggered every minute by EventBridge.  Queries due reminders and
    delivers them via Telegram (Req 10.3, 10.8).

POST /jobs/pregnancy-update
    Triggered daily at 08:00 local time by EventBridge.  Delivers daily
    facts and weekly milestone messages to all onboarded users (Req 3.2, 3.3).

POST /jobs/weekly-report
    Triggered weekly by EventBridge.  Generates and delivers weekly
    nutrition and symptom digests to active users (Req 16.3).

POST /jobs/backup
    Triggered nightly at 02:00 UTC by EventBridge.  Runs pg_dump, compresses
    the output, and uploads to S3 (Req 16.8).

Security
--------
Every request must include the ``X-Job-Secret`` header with a value that
matches ``settings.job_secret``.  Requests with a missing or incorrect
header are rejected with HTTP 403 before any job logic executes (Req 17.5).

A constant-time comparison is used to prevent timing-based secret leakage.

Design patterns (matching ``app/api/routes/payment.py``)
---------------------------------------------------------
- Each job module import is guarded with try/except so this module can be
  imported before the individual job files have been written.
- structlog is used for all log statements.
- Jobs run as FastAPI ``BackgroundTasks`` so the HTTP 200 is returned
  immediately and the EventBridge caller does not time out.
- No health data is ever logged here — only job names and timing metadata.

Requirements: 17.5
"""

from __future__ import annotations

import hmac
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.config import settings
from app.dependencies import get_db

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Job module imports — each guarded so the router can be included in
# app/main.py before the individual job files have been written.
# ---------------------------------------------------------------------------

try:
    from app.jobs.reminder_job import run as run_reminder_job  # type: ignore[import]

    _reminder_job_available = True
except ImportError:
    _reminder_job_available = False
    log.debug(
        "job_not_yet_available",
        job="reminder_job",
        hint="app/jobs/reminder_job.py has not been created yet",
    )

try:
    from app.jobs.pregnancy_update_job import run as run_pregnancy_update_job  # type: ignore[import]

    _pregnancy_update_job_available = True
except ImportError:
    _pregnancy_update_job_available = False
    log.debug(
        "job_not_yet_available",
        job="pregnancy_update_job",
        hint="app/jobs/pregnancy_update_job.py has not been created yet",
    )

try:
    from app.jobs.weekly_report_job import run as run_weekly_report_job  # type: ignore[import]

    _weekly_report_job_available = True
except ImportError:
    _weekly_report_job_available = False
    log.debug(
        "job_not_yet_available",
        job="weekly_report_job",
        hint="app/jobs/weekly_report_job.py has not been created yet",
    )

try:
    from app.jobs.backup_job import run as run_backup_job  # type: ignore[import]

    _backup_job_available = True
except ImportError:
    _backup_job_available = False
    log.debug(
        "job_not_yet_available",
        job="backup_job",
        hint="app/jobs/backup_job.py has not been created yet",
    )


# ---------------------------------------------------------------------------
# Secret validation helper
# ---------------------------------------------------------------------------

def _verify_job_secret(request: Request) -> None:
    """
    Validate the ``X-Job-Secret`` header against ``settings.job_secret``.

    Uses a constant-time comparison (``hmac.compare_digest``) to prevent
    timing-based secret discovery.

    Raises:
        HTTPException(403): when the header is missing, empty, or does not
            match the configured secret.
    """
    incoming = request.headers.get("X-Job-Secret", "")
    configured = settings.job_secret

    # Reject if either side is empty — an unconfigured secret must not
    # allow unrestricted access.
    if not incoming or not configured:
        log.warning(
            "job_secret_validation_failed",
            reason="missing_secret" if not incoming else "job_secret_not_configured",
        )
        raise HTTPException(status_code=403, detail="Forbidden")

    if not hmac.compare_digest(incoming, configured):
        log.warning("job_secret_validation_failed", reason="incorrect_secret")
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Background task wrappers
# ---------------------------------------------------------------------------

async def _dispatch_reminder_job(db: AsyncSession) -> None:
    """Run the reminder job; errors are caught so BackgroundTasks never crash."""
    if not _reminder_job_available:
        log.warning(
            "job_dispatch_skipped",
            job="reminder_job",
            hint="app/jobs/reminder_job.py not found",
        )
        return
    try:
        await run_reminder_job(db=db)
    except Exception as exc:  # noqa: BLE001
        log.error("job_dispatch_error", job="reminder_job", error=str(exc), exc_info=True)


async def _dispatch_pregnancy_update_job(db: AsyncSession) -> None:
    """Run the pregnancy-update job; errors are caught and logged."""
    if not _pregnancy_update_job_available:
        log.warning(
            "job_dispatch_skipped",
            job="pregnancy_update_job",
            hint="app/jobs/pregnancy_update_job.py not found",
        )
        return
    try:
        await run_pregnancy_update_job(db=db)
    except Exception as exc:  # noqa: BLE001
        log.error("job_dispatch_error", job="pregnancy_update_job", error=str(exc), exc_info=True)


async def _dispatch_weekly_report_job(db: AsyncSession) -> None:
    """Run the weekly-report job; errors are caught and logged."""
    if not _weekly_report_job_available:
        log.warning(
            "job_dispatch_skipped",
            job="weekly_report_job",
            hint="app/jobs/weekly_report_job.py not found",
        )
        return
    try:
        await run_weekly_report_job(db=db)
    except Exception as exc:  # noqa: BLE001
        log.error("job_dispatch_error", job="weekly_report_job", error=str(exc), exc_info=True)


async def _dispatch_backup_job(db: AsyncSession) -> None:
    """Run the backup job; errors are caught and logged."""
    if not _backup_job_available:
        log.warning(
            "job_dispatch_skipped",
            job="backup_job",
            hint="app/jobs/backup_job.py not found",
        )
        return
    try:
        await run_backup_job(db=db)
    except Exception as exc:  # noqa: BLE001
        log.error("job_dispatch_error", job="backup_job", error=str(exc), exc_info=True)


# ---------------------------------------------------------------------------
# POST /jobs/reminders
# ---------------------------------------------------------------------------


@router.post("/reminders", status_code=200, summary="Trigger reminder delivery job")
async def trigger_reminders(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Trigger the reminder delivery job.

    Called by EventBridge every minute (cron: ``* * * * ? *``).

    Flow:
      1. Validate ``X-Job-Secret`` header → 403 on mismatch or absence.
      2. Dispatch ``reminder_job.run`` as a background task.
      3. Return HTTP 200 immediately so EventBridge does not retry.

    Requirements: 10.3, 10.8, 17.5
    """
    _verify_job_secret(request)
    log.info("job_triggered", job="reminders")
    background_tasks.add_task(_dispatch_reminder_job, db)
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)


# ---------------------------------------------------------------------------
# POST /jobs/pregnancy-update
# ---------------------------------------------------------------------------


@router.post("/pregnancy-update", status_code=200, summary="Trigger pregnancy update job")
async def trigger_pregnancy_update(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Trigger the daily pregnancy update job.

    Called by EventBridge daily at 08:00 user local time.  Delivers daily
    facts and weekly milestone messages to all onboarded users.

    Flow:
      1. Validate ``X-Job-Secret`` header → 403 on mismatch or absence.
      2. Dispatch ``pregnancy_update_job.run`` as a background task.
      3. Return HTTP 200 immediately.

    Requirements: 3.2, 3.3, 17.5
    """
    _verify_job_secret(request)
    log.info("job_triggered", job="pregnancy-update")
    background_tasks.add_task(_dispatch_pregnancy_update_job, db)
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)


# ---------------------------------------------------------------------------
# POST /jobs/weekly-report
# ---------------------------------------------------------------------------


@router.post("/weekly-report", status_code=200, summary="Trigger weekly report job")
async def trigger_weekly_report(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Trigger the weekly digest report job.

    Called by EventBridge once per week.  Generates and delivers weekly
    nutrition and symptom trend digests to all active users.

    Flow:
      1. Validate ``X-Job-Secret`` header → 403 on mismatch or absence.
      2. Dispatch ``weekly_report_job.run`` as a background task.
      3. Return HTTP 200 immediately.

    Requirements: 16.3, 17.5
    """
    _verify_job_secret(request)
    log.info("job_triggered", job="weekly-report")
    background_tasks.add_task(_dispatch_weekly_report_job, db)
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)


# ---------------------------------------------------------------------------
# POST /jobs/backup
# ---------------------------------------------------------------------------


@router.post("/backup", status_code=200, summary="Trigger database backup job")
async def trigger_backup(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Trigger the nightly database backup job.

    Called by EventBridge nightly at 02:00 UTC.  Runs pg_dump, compresses
    the output with gzip, and uploads the archive to S3 (Req 16.8).

    Flow:
      1. Validate ``X-Job-Secret`` header → 403 on mismatch or absence.
      2. Dispatch ``backup_job.run`` as a background task.
      3. Return HTTP 200 immediately.

    Requirements: 16.8, 17.5
    """
    _verify_job_secret(request)
    log.info("job_triggered", job="backup")
    background_tasks.add_task(_dispatch_backup_job, db)
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)
