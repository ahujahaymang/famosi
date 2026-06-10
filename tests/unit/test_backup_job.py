"""
Unit tests for app/jobs/backup_job.py

Covers:
  - _parse_db_url: various URL formats including asyncpg driver prefix
  - _silent_delete: no error on missing file, deletes existing file
  - _emit_error_log: emits ERROR with required fields (job_id, failure_stage,
    timestamp) per Req 16.9
  - run_backup: success path — calls dump, compress, upload in order
  - run_backup: failure at dump stage — cleans up and raises BackupError
  - run_backup: failure at compress stage — cleans up and raises BackupError
  - run_backup: failure at upload stage — cleans up and raises BackupError
  - BackupError: captures stage and message attributes

Requirements: 16.8, 16.9
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.backup_job import (
    STAGE_COMPRESS,
    STAGE_DUMP,
    STAGE_UPLOAD,
    BackupError,
    _emit_error_log,
    _parse_db_url,
    _silent_delete,
    run_backup,
)


# ---------------------------------------------------------------------------
# _parse_db_url
# ---------------------------------------------------------------------------


class TestParseDbUrl:
    def test_standard_postgresql_url(self):
        url = "postgresql://myuser:mypassword@db.example.com:5432/mydb"
        result = _parse_db_url(url)
        assert result["host"] == "db.example.com"
        assert result["port"] == "5432"
        assert result["username"] == "myuser"
        assert result["password"] == "mypassword"
        assert result["dbname"] == "mydb"

    def test_asyncpg_prefix_is_normalised(self):
        url = "postgresql+asyncpg://admin:secret@localhost:5432/famosi"
        result = _parse_db_url(url)
        assert result["host"] == "localhost"
        assert result["port"] == "5432"
        assert result["username"] == "admin"
        assert result["password"] == "secret"
        assert result["dbname"] == "famosi"

    def test_missing_port_defaults_to_5432(self):
        url = "postgresql://user:pass@host/dbname"
        result = _parse_db_url(url)
        assert result["port"] == "5432"

    def test_empty_password_returns_empty_string(self):
        url = "postgresql://user:@host:5432/db"
        result = _parse_db_url(url)
        assert result["password"] == ""

    def test_dbname_leading_slash_stripped(self):
        url = "postgresql://user:pass@host:5432/my_database"
        result = _parse_db_url(url)
        assert result["dbname"] == "my_database"


# ---------------------------------------------------------------------------
# _silent_delete
# ---------------------------------------------------------------------------


class TestSilentDelete:
    def test_does_not_raise_for_missing_file(self, tmp_path):
        path = tmp_path / "nonexistent.sql"
        # Should not raise even though the file does not exist
        _silent_delete(path)

    def test_deletes_existing_file(self, tmp_path):
        path = tmp_path / "backup.sql"
        path.write_text("data")
        assert path.exists()
        _silent_delete(path)
        assert not path.exists()


# ---------------------------------------------------------------------------
# _emit_error_log
# ---------------------------------------------------------------------------


class TestEmitErrorLog:
    def test_emits_error_with_required_fields(self):
        """Req 16.9: ERROR log must contain job_id, failure_stage, timestamp."""
        import structlog
        from structlog.testing import capture_logs

        bound_log = structlog.get_logger(__name__).bind(job_id="test-job-001")
        exc = RuntimeError("connection refused")

        with capture_logs() as logs:
            _emit_error_log(bound_log, stage=STAGE_DUMP, exc=exc)

        assert len(logs) == 1
        event = logs[0]
        assert event["log_level"] == "error"
        assert event["event"] == "backup_job_failed"
        assert event["failure_stage"] == STAGE_DUMP
        assert "timestamp" in event
        assert event["error_type"] == "RuntimeError"
        # job_id is bound on the logger — check it propagates
        assert event["job_id"] == "test-job-001"

    def test_error_summary_truncated_to_200_chars(self):
        import structlog
        from structlog.testing import capture_logs

        bound_log = structlog.get_logger(__name__).bind(job_id="j")
        exc = ValueError("x" * 500)

        with capture_logs() as logs:
            _emit_error_log(bound_log, stage=STAGE_UPLOAD, exc=exc)

        assert len(logs[0]["error_summary"]) <= 200

    def test_stage_name_preserved_in_log(self):
        import structlog
        from structlog.testing import capture_logs

        bound_log = structlog.get_logger(__name__).bind(job_id="j2")

        for stage in (STAGE_DUMP, STAGE_COMPRESS, STAGE_UPLOAD):
            with capture_logs() as logs:
                _emit_error_log(bound_log, stage=stage, exc=OSError("err"))
            assert logs[0]["failure_stage"] == stage


# ---------------------------------------------------------------------------
# BackupError
# ---------------------------------------------------------------------------


class TestBackupError:
    def test_is_runtime_error(self):
        err = BackupError(STAGE_DUMP, "pg_dump not found")
        assert isinstance(err, RuntimeError)

    def test_stage_attribute(self):
        err = BackupError(STAGE_COMPRESS, "gzip failed")
        assert err.stage == STAGE_COMPRESS

    def test_message_attribute(self):
        err = BackupError(STAGE_UPLOAD, "S3 403 Forbidden")
        assert err.message == "S3 403 Forbidden"

    def test_str_contains_stage(self):
        err = BackupError(STAGE_UPLOAD, "timeout")
        assert STAGE_UPLOAD in str(err)


# ---------------------------------------------------------------------------
# run_backup — success path
# ---------------------------------------------------------------------------


class TestRunBackupSuccess:
    @pytest.mark.asyncio
    async def test_successful_backup_returns_ok_status(self):
        """Happy path: all three stages succeed."""
        with (
            patch("app.jobs.backup_job._stage_dump", new_callable=AsyncMock) as mock_dump,
            patch(
                "app.jobs.backup_job._stage_compress", new_callable=AsyncMock
            ) as mock_compress,
            patch(
                "app.jobs.backup_job._stage_upload", new_callable=AsyncMock
            ) as mock_upload,
            patch("app.jobs.backup_job._silent_delete") as mock_delete,
        ):
            result = await run_backup(job_id="happy-path-001")

        assert result["status"] == "ok"
        assert result["job_id"] == "happy-path-001"
        assert "s3_key" in result
        assert result["s3_key"].startswith("daily/backup_")
        assert result["s3_key"].endswith(".sql.gz")
        assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_stages_called_in_order(self):
        """Stages must be called in sequence: dump → compress → upload."""
        call_order: list[str] = []

        async def mock_dump(*a, **kw):
            call_order.append("dump")

        async def mock_compress(*a, **kw):
            call_order.append("compress")

        async def mock_upload(*a, **kw):
            call_order.append("upload")

        with (
            patch("app.jobs.backup_job._stage_dump", side_effect=mock_dump),
            patch("app.jobs.backup_job._stage_compress", side_effect=mock_compress),
            patch("app.jobs.backup_job._stage_upload", side_effect=mock_upload),
            patch("app.jobs.backup_job._silent_delete"),
        ):
            await run_backup(job_id="order-test")

        assert call_order == ["dump", "compress", "upload"]

    @pytest.mark.asyncio
    async def test_job_id_auto_generated_when_not_provided(self):
        with (
            patch("app.jobs.backup_job._stage_dump", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_compress", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_upload", new_callable=AsyncMock),
            patch("app.jobs.backup_job._silent_delete"),
        ):
            result = await run_backup()

        # job_id should be a non-empty string (UUID)
        assert result["job_id"]
        assert len(result["job_id"]) == 36  # standard UUID format

    @pytest.mark.asyncio
    async def test_cleanup_called_after_success(self):
        """Even on success, /tmp files should be cleaned up."""
        deleted_paths: list[str] = []

        def mock_delete(path: Path):
            deleted_paths.append(str(path))

        with (
            patch("app.jobs.backup_job._stage_dump", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_compress", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_upload", new_callable=AsyncMock),
            patch("app.jobs.backup_job._silent_delete", side_effect=mock_delete),
        ):
            await run_backup(job_id="cleanup-test")

        # Both the .sql and .gz paths should be scheduled for cleanup
        assert any(".sql" in p for p in deleted_paths)
        assert any(".sql.gz" in p for p in deleted_paths)


# ---------------------------------------------------------------------------
# run_backup — failure paths (Req 16.9)
# ---------------------------------------------------------------------------


class TestRunBackupFailure:
    @pytest.mark.asyncio
    async def test_dump_failure_raises_backup_error(self):
        async def fail_dump(*a, **kw):
            raise BackupError(STAGE_DUMP, "pg_dump not found")

        with (
            patch("app.jobs.backup_job._stage_dump", side_effect=fail_dump),
            patch("app.jobs.backup_job._stage_compress", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_upload", new_callable=AsyncMock),
            patch("app.jobs.backup_job._silent_delete"),
        ):
            with pytest.raises(BackupError) as exc_info:
                await run_backup(job_id="fail-dump-001")

        assert exc_info.value.stage == STAGE_DUMP

    @pytest.mark.asyncio
    async def test_compress_failure_raises_backup_error(self):
        async def fail_compress(*a, **kw):
            raise BackupError(STAGE_COMPRESS, "gzip binary missing")

        with (
            patch("app.jobs.backup_job._stage_dump", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_compress", side_effect=fail_compress),
            patch("app.jobs.backup_job._stage_upload", new_callable=AsyncMock),
            patch("app.jobs.backup_job._silent_delete"),
        ):
            with pytest.raises(BackupError) as exc_info:
                await run_backup(job_id="fail-compress-001")

        assert exc_info.value.stage == STAGE_COMPRESS

    @pytest.mark.asyncio
    async def test_upload_failure_raises_backup_error(self):
        async def fail_upload(*a, **kw):
            raise BackupError(STAGE_UPLOAD, "S3 credentials invalid")

        with (
            patch("app.jobs.backup_job._stage_dump", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_compress", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_upload", side_effect=fail_upload),
            patch("app.jobs.backup_job._silent_delete"),
        ):
            with pytest.raises(BackupError) as exc_info:
                await run_backup(job_id="fail-upload-001")

        assert exc_info.value.stage == STAGE_UPLOAD

    @pytest.mark.asyncio
    async def test_cleanup_still_runs_after_failure(self):
        """Req 16.9: partial artifact must be deleted even on failure."""
        deleted_paths: list[str] = []

        def mock_delete(path: Path):
            deleted_paths.append(str(path))

        async def fail_dump(*a, **kw):
            raise BackupError(STAGE_DUMP, "error")

        with (
            patch("app.jobs.backup_job._stage_dump", side_effect=fail_dump),
            patch("app.jobs.backup_job._stage_compress", new_callable=AsyncMock),
            patch("app.jobs.backup_job._stage_upload", new_callable=AsyncMock),
            patch("app.jobs.backup_job._silent_delete", side_effect=mock_delete),
        ):
            with pytest.raises(BackupError):
                await run_backup(job_id="cleanup-on-fail")

        # Cleanup must be attempted for both potential artifacts
        assert len(deleted_paths) >= 2
        assert any(".sql" in p for p in deleted_paths)
        assert any(".sql.gz" in p for p in deleted_paths)

    @pytest.mark.asyncio
    async def test_upstream_stage_not_called_after_dump_failure(self):
        """Once dump fails, compress and upload must not be called."""
        compress_called = False
        upload_called = False

        async def fail_dump(*a, **kw):
            raise BackupError(STAGE_DUMP, "error")

        async def record_compress(*a, **kw):
            nonlocal compress_called
            compress_called = True

        async def record_upload(*a, **kw):
            nonlocal upload_called
            upload_called = True

        with (
            patch("app.jobs.backup_job._stage_dump", side_effect=fail_dump),
            patch("app.jobs.backup_job._stage_compress", side_effect=record_compress),
            patch("app.jobs.backup_job._stage_upload", side_effect=record_upload),
            patch("app.jobs.backup_job._silent_delete"),
        ):
            with pytest.raises(BackupError):
                await run_backup()

        assert not compress_called
        assert not upload_called
