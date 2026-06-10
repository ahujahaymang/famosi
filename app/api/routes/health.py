"""
Health check and metrics routes.

GET /health   — verifies database reachability and returns a structured status
                response.  Always returns HTTP 200 so that load-balancers and
                monitoring systems can distinguish a running (but degraded)
                service from a crashed one.

GET /metrics  — operator-only endpoint (protected by JOB_SECRET) that returns
                aggregated product and AI cost metrics computed from the
                request_logs table.  Metrics include DAU, WAU, 7-day retention,
                token cost per user, RAG hit rate, intent distribution, and
                latency percentiles (p50, p95, p99).

Privacy contract: no health data is logged here — only connectivity status
and aggregate metrics.  Individual user data is never surfaced.

Requirements: 16.3, 16.4, 16.7, 17.7
"""

import hmac
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db

router = APIRouter()

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Secret validation helper (shared pattern with app/api/routes/jobs.py)
# ---------------------------------------------------------------------------

def _verify_job_secret(request: Request) -> None:
    """
    Validate the ``X-Job-Secret`` header against ``settings.job_secret``.

    Uses constant-time comparison (``hmac.compare_digest``) to prevent
    timing-based secret discovery.

    Raises:
        HTTPException(403): when the header is missing, empty, or incorrect.
    """
    incoming = request.headers.get("X-Job-Secret", "")
    configured = settings.job_secret

    if not incoming or not configured:
        log.warning(
            "metrics_secret_validation_failed",
            reason="missing_secret" if not incoming else "job_secret_not_configured",
        )
        raise HTTPException(status_code=403, detail="Forbidden")

    if not hmac.compare_digest(incoming, configured):
        log.warning("metrics_secret_validation_failed", reason="incorrect_secret")
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@router.get("/health", summary="Health check")
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Probe database connectivity and return a structured health payload.

    Returns HTTP 200 in both healthy and degraded states so that upstream
    proxies and monitoring agents always receive a response body they can
    parse without treating a degraded service as completely unreachable.

    Responses:
        200 {"status": "ok",       "db": "ok"}    — DB reachable
        200 {"status": "degraded", "db": "error"} — DB unreachable / error
    """
    try:
        await db.execute(text("SELECT 1"))
        log.info("health_check_ok", db_status="ok")
        return {"status": "ok", "db": "ok"}
    except Exception as exc:  # noqa: BLE001 — intentionally broad catch
        # Log the error type only; never log raw exception messages that
        # could contain connection strings or other sensitive config.
        log.error(
            "health_check_degraded",
            db_status="error",
            error_type=type(exc).__name__,
        )
        return {"status": "degraded", "db": "error"}


# ---------------------------------------------------------------------------
# GET /metrics — operator-only aggregated metrics
# ---------------------------------------------------------------------------

@router.get("/metrics", summary="Operator metrics dashboard")
async def get_metrics(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return aggregated product, AI cost, and operational metrics computed
    from the ``request_logs`` table.

    Access is restricted to operators holding the ``JOB_SECRET`` value,
    supplied via the ``X-Job-Secret`` request header.

    Metrics returned
    ----------------
    engagement:
      - dau           : distinct users with at least one request today (UTC)
      - wau           : distinct users in the past 7 days
      - retention_7d  : fraction of last-week cohort that returned this week

    ai_cost:
      - avg_tokens_per_user : average token consumption per distinct user (all time)
      - rag_hit_rate        : fraction of RAG requests that retrieved ≥1 chunk

    intent_distribution:
      - counts per intent label from request_logs (all time)

    latency_percentiles:
      - p50_ms, p95_ms, p99_ms computed via PERCENTILE_CONT on latency_ms

    Requirements: 16.3, 16.4, 16.7
    """
    _verify_job_secret(request)
    log.info("metrics_requested")

    # ------------------------------------------------------------------
    # 1. DAU — distinct user_ids with a request today (UTC)
    # ------------------------------------------------------------------
    dau_row = await db.execute(
        text(
            """
            SELECT COUNT(DISTINCT user_id) AS dau
            FROM request_logs
            WHERE user_id IS NOT NULL
              AND created_at >= CURRENT_DATE AT TIME ZONE 'UTC'
              AND created_at <  CURRENT_DATE AT TIME ZONE 'UTC' + INTERVAL '1 day'
            """
        )
    )
    dau: int = dau_row.scalar() or 0

    # ------------------------------------------------------------------
    # 2. WAU — distinct user_ids in the last 7 days
    # ------------------------------------------------------------------
    wau_row = await db.execute(
        text(
            """
            SELECT COUNT(DISTINCT user_id) AS wau
            FROM request_logs
            WHERE user_id IS NOT NULL
              AND created_at >= NOW() AT TIME ZONE 'UTC' - INTERVAL '7 days'
            """
        )
    )
    wau: int = wau_row.scalar() or 0

    # ------------------------------------------------------------------
    # 3. 7-day retention
    #    Cohort  = users active in the 7-day window ending 7 days ago.
    #    Retained = cohort users who were also active in the last 7 days.
    # ------------------------------------------------------------------
    retention_row = await db.execute(
        text(
            """
            WITH prior_week AS (
                SELECT DISTINCT user_id
                FROM request_logs
                WHERE user_id IS NOT NULL
                  AND created_at >= NOW() AT TIME ZONE 'UTC' - INTERVAL '14 days'
                  AND created_at <  NOW() AT TIME ZONE 'UTC' - INTERVAL '7 days'
            ),
            current_week AS (
                SELECT DISTINCT user_id
                FROM request_logs
                WHERE user_id IS NOT NULL
                  AND created_at >= NOW() AT TIME ZONE 'UTC' - INTERVAL '7 days'
            )
            SELECT
                COUNT(prior_week.user_id)                           AS cohort_size,
                COUNT(current_week.user_id)                         AS retained_count,
                CASE
                    WHEN COUNT(prior_week.user_id) = 0 THEN NULL
                    ELSE ROUND(
                        COUNT(current_week.user_id)::numeric /
                        COUNT(prior_week.user_id)::numeric,
                        4
                    )
                END                                                  AS retention_rate
            FROM prior_week
            LEFT JOIN current_week USING (user_id)
            """
        )
    )
    retention_result = retention_row.mappings().one()
    retention_7d: float | None = (
        float(retention_result["retention_rate"])
        if retention_result["retention_rate"] is not None
        else None
    )

    # ------------------------------------------------------------------
    # 4. Average tokens consumed per distinct user (all time)
    # ------------------------------------------------------------------
    tokens_row = await db.execute(
        text(
            """
            SELECT
                ROUND(
                    COALESCE(SUM(tokens_used), 0)::numeric /
                    NULLIF(COUNT(DISTINCT user_id), 0),
                    2
                ) AS avg_tokens_per_user
            FROM request_logs
            WHERE user_id IS NOT NULL
              AND tokens_used IS NOT NULL
            """
        )
    )
    avg_tokens_per_user: float | None = tokens_row.scalar()
    if avg_tokens_per_user is not None:
        avg_tokens_per_user = float(avg_tokens_per_user)

    # ------------------------------------------------------------------
    # 5. RAG hit rate — fraction of RAG requests with rag_empty = FALSE
    #    (i.e., at least one chunk was retrieved)
    # ------------------------------------------------------------------
    rag_row = await db.execute(
        text(
            """
            SELECT
                COUNT(*)                                           AS total_rag_requests,
                COUNT(*) FILTER (WHERE rag_empty = FALSE)         AS rag_hits,
                CASE
                    WHEN COUNT(*) = 0 THEN NULL
                    ELSE ROUND(
                        COUNT(*) FILTER (WHERE rag_empty = FALSE)::numeric /
                        COUNT(*)::numeric,
                        4
                    )
                END                                                 AS rag_hit_rate
            FROM request_logs
            WHERE is_rag = TRUE
              AND rag_empty IS NOT NULL
            """
        )
    )
    rag_result = rag_row.mappings().one()
    rag_hit_rate: float | None = (
        float(rag_result["rag_hit_rate"])
        if rag_result["rag_hit_rate"] is not None
        else None
    )

    # ------------------------------------------------------------------
    # 6. Intent distribution — counts per intent label (all time)
    # ------------------------------------------------------------------
    intent_rows = await db.execute(
        text(
            """
            SELECT
                COALESCE(intent::text, 'unclassified') AS intent_label,
                COUNT(*)                               AS request_count
            FROM request_logs
            GROUP BY COALESCE(intent::text, 'unclassified')
            ORDER BY request_count DESC
            """
        )
    )
    intent_distribution: dict[str, int] = {
        row["intent_label"]: int(row["request_count"])
        for row in intent_rows.mappings()
    }

    # ------------------------------------------------------------------
    # 7. Latency percentiles — p50, p95, p99 via PERCENTILE_CONT
    # ------------------------------------------------------------------
    latency_row = await db.execute(
        text(
            """
            SELECT
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_ms
            FROM request_logs
            WHERE latency_ms IS NOT NULL
            """
        )
    )
    latency_result = latency_row.mappings().one()
    latency_percentiles: dict[str, float | None] = {
        "p50_ms": float(latency_result["p50_ms"]) if latency_result["p50_ms"] is not None else None,
        "p95_ms": float(latency_result["p95_ms"]) if latency_result["p95_ms"] is not None else None,
        "p99_ms": float(latency_result["p99_ms"]) if latency_result["p99_ms"] is not None else None,
    }

    log.info(
        "metrics_computed",
        dau=dau,
        wau=wau,
        retention_7d=retention_7d,
        rag_hit_rate=rag_hit_rate,
    )

    return {
        "engagement": {
            "dau": dau,
            "wau": wau,
            "retention_7d": retention_7d,
        },
        "ai_cost": {
            "avg_tokens_per_user": avg_tokens_per_user,
            "rag_hit_rate": rag_hit_rate,
        },
        "intent_distribution": intent_distribution,
        "latency_percentiles": latency_percentiles,
    }
