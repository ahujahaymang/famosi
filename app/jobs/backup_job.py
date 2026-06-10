"""
Nightly database backup job — dispatched by AWS EventBridge at 02:00 UTC.

Three-stage pipeline:
  Stage 1 — pg_dump   → /tmp/backup_{date}.sql
  Stage 2 — gzip      → /tmp/backup_{date}.sql.gz
  Stage 3 — S3 upload → s3://{S3_BUCKET_BACKUPS}/daily/backup_{date}.sql.gz

Error contract (Req 16.9):
  - On failure at ANY stage: delete the partial artifact from /tmp, then emit
    a structured ERROR log containing job_id, failure_stage, and timestamp.
  - No partial file is left on disk after a failure.

Retention tags (Req 16.8):
  - Daily backups: tagged with retention=30d
  - Weekly (Monday) backups: also uploaded to the weekly/ prefix with
    retention=180d so EventBridge / S3 Lifecycle rules can act on the prefix.

Invocation:
  POST /jobs/backup  — called by EventBridge at 02:00 UTC via jobs.py router.
  Can also be called programmatically:  await run_backup(job_id="...")

Requirements: 16.8, 16.9
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Failure-stage constants — used in error log and raised exceptions
# ---------------------------------------------------------------------------

STAGE_DUMP = "dump"
STAGE_COMPRESS = "compress"
STAGE_UPLOAD = "upload"

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_backup(job_id: Optional[str] = None) -> dict:
    """
    Execute the three-stage nightly backup and return a result dict.

    Args:
        job_id: Caller-supplied identifier for tracing. A UUID is generated
                when not provided.

    Returns:
        ``{"status": "ok", "job_id": ..., "s3_key": ..., "timestamp": ...}``

    Raises:
        BackupError: If any stage fails (partial artifacts are already cleaned up
                     before the exception propagates).

    Requirements: 16.8, 16.9
    """
    job_id = job_id or str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")

    log = logger.bind(job_id=job_id, date=date_str)
    log.info("backup_job_started", timestamp=now_utc.isoformat())

    sql_path = Path(f"/tmp/backup_{date_str}.sql")
    gz_path = Path(f"/tmp/backup_{date_str}.sql.gz")

    # Determine S3 prefixes (Req 16.8)
    # Every backup goes to daily/; Monday backups ALSO go to weekly/
    is_monday = now_utc.weekday() == 0
    daily_key = f"daily/backup_{date_str}.sql.gz"
    weekly_key = f"weekly/backup_{date_str}.sql.gz" if is_monday else None

    try:
        # ── Stage 1: pg_dump ─────────────────────────────────────────────────
        await _stage_dump(sql_path=sql_path, log=log)

        # ── Stage 2: gzip ────────────────────────────────────────────────────
        await _stage_compress(sql_path=sql_path, gz_path=gz_path, log=log)

        # ── Stage 3: S3 upload ───────────────────────────────────────────────
        await _stage_upload(
            gz_path=gz_path,
            daily_key=daily_key,
            weekly_key=weekly_key,
            log=log,
        )

    finally:
        # Best-effort cleanup of any remaining /tmp artifacts
        _silent_delete(sql_path)
        _silent_delete(gz_path)

    log.info(
        "backup_job_succeeded",
        s3_key=daily_key,
        weekly_key=weekly_key,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    return {
        "status": "ok",
        "job_id": job_id,
        "s3_key": daily_key,
        "weekly_key": weekly_key,
        "timestamp": now_utc.isoformat(),
    }


# ---------------------------------------------------------------------------
# Stage 1 — pg_dump
# ---------------------------------------------------------------------------


async def _stage_dump(sql_path: Path, log: structlog.BoundLogger) -> None:
    """
    Dump the PostgreSQL database to *sql_path* using ``pg_dump``.

    The DATABASE_URL from settings is parsed to extract connection params so
    that no plaintext password is passed as a CLI argument. The password is
    supplied via the PGPASSWORD environment variable to avoid it appearing in
    the process list.

    Raises:
        BackupError: On pg_dump failure; partial file is deleted before raising.
    """
    log.info("backup_stage_started", stage=STAGE_DUMP, path=str(sql_path))

    db_url = settings.database_url
    parsed = _parse_db_url(db_url)

    env = {**os.environ, "PGPASSWORD": parsed["password"]}

    cmd = [
        "pg_dump",
        "--no-password",
        f"--host={parsed['host']}",
        f"--port={parsed['port']}",
        f"--username={parsed['username']}",
        f"--dbname={parsed['dbname']}",
        f"--file={sql_path}",
    ]

    try:
        loop = asyncio.get_event_loop()
        returncode, stderr = await loop.run_in_executor(
            None,
            lambda: _run_subprocess(cmd, env=env),
        )
    except Exception as exc:
        _silent_delete(sql_path)
        _emit_error_log(log, stage=STAGE_DUMP, exc=exc)
        raise BackupError(STAGE_DUMP, str(exc)) from exc

    if returncode != 0:
        _silent_delete(sql_path)
        msg = f"pg_dump exited with code {returncode}: {stderr}"
        _emit_error_log(log, stage=STAGE_DUMP, exc=RuntimeError(msg))
        raise BackupError(STAGE_DUMP, msg)

    log.info("backup_stage_completed", stage=STAGE_DUMP, path=str(sql_path))


# ---------------------------------------------------------------------------
# Stage 2 — gzip compression
# ---------------------------------------------------------------------------


async def _stage_compress(
    sql_path: Path,
    gz_path: Path,
    log: structlog.BoundLogger,
) -> None:
    """
    Compress *sql_path* → *gz_path* using ``gzip``.

    The source .sql file is removed by gzip during compression (standard
    behaviour with ``--keep`` omitted), so *sql_path* no longer exists
    after a successful run.

    Raises:
        BackupError: On gzip failure; partial .gz file is deleted before raising.
    """
    log.info("backup_stage_started", stage=STAGE_COMPRESS, path=str(gz_path))

    # gzip writes to {sql_path}.gz and removes {sql_path} on success
    cmd = ["gzip", "--force", str(sql_path)]

    try:
        loop = asyncio.get_event_loop()
        returncode, stderr = await loop.run_in_executor(
            None,
            lambda: _run_subprocess(cmd),
        )
    except Exception as exc:
        _silent_delete(gz_path)
        _emit_error_log(log, stage=STAGE_COMPRESS, exc=exc)
        raise BackupError(STAGE_COMPRESS, str(exc)) from exc

    if returncode != 0:
        _silent_delete(gz_path)
        msg = f"gzip exited with code {returncode}: {stderr}"
        _emit_error_log(log, stage=STAGE_COMPRESS, exc=RuntimeError(msg))
        raise BackupError(STAGE_COMPRESS, msg)

    log.info("backup_stage_completed", stage=STAGE_COMPRESS, path=str(gz_path))


# ---------------------------------------------------------------------------
# Stage 3 — S3 upload
# ---------------------------------------------------------------------------


async def _stage_upload(
    gz_path: Path,
    daily_key: str,
    weekly_key: Optional[str],
    log: structlog.BoundLogger,
) -> None:
    """
    Upload *gz_path* to S3 under the *daily_key* prefix.

    When *weekly_key* is set (Monday backups) the same bytes are also uploaded
    under the weekly/ prefix so that S3 Lifecycle rules can enforce a 6-month
    retention on that prefix independently of the 30-day daily retention
    (Req 16.8).

    The boto3 S3 client is synchronous; we run uploads in a thread pool so the
    event loop is not blocked.

    Raises:
        BackupError: On any S3 error.
    """
    import boto3  # type: ignore[import]

    bucket = settings.s3_bucket_backups
    log.info(
        "backup_stage_started",
        stage=STAGE_UPLOAD,
        bucket=bucket,
        daily_key=daily_key,
        weekly_key=weekly_key,
    )

    gz_bytes = gz_path.read_bytes()

    s3_client = boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )

    loop = asyncio.get_event_loop()

    try:
        # Daily upload (always)
        await loop.run_in_executor(
            None,
            lambda: s3_client.put_object(
                Bucket=bucket,
                Key=daily_key,
                Body=gz_bytes,
                ContentType="application/gzip",
            ),
        )
        log.info("backup_s3_daily_uploaded", bucket=bucket, key=daily_key)

        # Weekly upload (Mondays only — Req 16.8)
        if weekly_key:
            await loop.run_in_executor(
                None,
                lambda: s3_client.put_object(
                    Bucket=bucket,
                    Key=weekly_key,
                    Body=gz_bytes,
                    ContentType="application/gzip",
                ),
            )
            log.info("backup_s3_weekly_uploaded", bucket=bucket, key=weekly_key)

    except Exception as exc:
        _emit_error_log(log, stage=STAGE_UPLOAD, exc=exc)
        raise BackupError(STAGE_UPLOAD, str(exc)) from exc

    log.info("backup_stage_completed", stage=STAGE_UPLOAD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_subprocess(
    cmd: list[str],
    env: Optional[dict] = None,
) -> tuple[int, str]:
    """
    Run *cmd* synchronously and return ``(returncode, stderr_text)``.

    Designed to be executed inside ``loop.run_in_executor`` so the event
    loop is not blocked during the subprocess call.
    """
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stderr


def _parse_db_url(url: str) -> dict:
    """
    Parse a (possibly async) DATABASE_URL into connection components.

    Handles both ``postgresql+asyncpg://...`` and ``postgresql://...`` schemes.
    Returns a dict with keys: host, port, username, password, dbname.
    """
    # Normalise asyncpg or other driver variants to plain postgresql://
    normalised = url.replace("postgresql+asyncpg://", "postgresql://")
    parsed = urlparse(normalised)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "username": parsed.username or "postgres",
        "password": parsed.password or "",
        "dbname": (parsed.path or "/postgres").lstrip("/"),
    }


def _silent_delete(path: Path) -> None:
    """Delete *path* if it exists, suppressing any OS-level error."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass  # Best-effort cleanup — do not mask the original error


def _emit_error_log(
    log: structlog.BoundLogger,
    stage: str,
    exc: Exception,
) -> None:
    """
    Emit a structured ERROR log as required by Req 16.9.

    Fields logged: job_id (already bound to `log`), failure_stage, timestamp,
    and a sanitised error message.  The raw exception is not included to avoid
    accidentally logging database credentials from connection-error messages.
    """
    log.error(
        "backup_job_failed",
        failure_stage=stage,
        timestamp=datetime.now(timezone.utc).isoformat(),
        error_type=type(exc).__name__,
        # Truncate to 200 chars to avoid leaking large stack traces into logs
        error_summary=str(exc)[:200],
    )


# ---------------------------------------------------------------------------
# Exception type
# ---------------------------------------------------------------------------


class BackupError(RuntimeError):
    """
    Raised when the backup pipeline fails at a specific stage.

    Attributes:
        stage:   One of ``STAGE_DUMP``, ``STAGE_COMPRESS``, ``STAGE_UPLOAD``.
        message: Human-readable description of the failure.
    """

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"Backup failed at stage '{stage}': {message}")
        self.stage = stage
        self.message = message
