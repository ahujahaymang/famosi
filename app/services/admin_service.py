"""
Admin service — business logic for all admin operations.

Provides:
- ``is_admin(telegram_user_id)``   — check if a Telegram user is the admin
- ``get_metrics_summary()``        — in-memory metrics for the last 24h window
- ``list_pending_users()``         — users awaiting admin approval
- ``approve_user()``               — activate trial for a pending user
- ``reject_user()``                — delete a pending user's record
- ``list_all_users()``             — paginated snapshot of all users
- ``send_admin_notification()``    — notify admin via Telegram

Design notes
------------
- Approval gate: new users are created with ``onboarding_complete=True`` but
  ``approval_pending=True`` (a flag stored in-process via a module-level set).
  The auth middleware treats them as read_only until approved.
  On approval the admin calls ``approve_user()`` which calls
  ``PaymentStateMachine.activate_trial()`` and clears the pending flag.
- Metrics are in-memory (reset on process restart) following the baker-assist
  pattern.  They are sufficient for a small-scale SaaS; add a time-series DB
  when the user base grows.

Privacy contract
----------------
No user message content, health data, or PII is stored in metrics.
Only structural fields are tracked: user_id (DB integer), intent labels,
model names, token counts, and latency.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.request_log import RequestLog
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User, UserRole

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Admin identity check
# ---------------------------------------------------------------------------


def is_admin(telegram_user_id: int) -> bool:
    """Return True when *telegram_user_id* matches the configured admin ID."""
    return (
        settings.admin_telegram_user_id != 0
        and telegram_user_id == settings.admin_telegram_user_id
    )


# ---------------------------------------------------------------------------
# Pending-approval registry (in-process)
#
# Keyed by telegram_user_id (int) so lookups are O(1).
# Each entry stores a small metadata dict for the admin notification.
# ---------------------------------------------------------------------------

# {telegram_user_id: {"name": str, "role": str, "country": str, "user_id": int}}
_pending_approval: dict[int, dict[str, Any]] = {}
_pending_lock = Lock()


def mark_pending(
    telegram_user_id: int,
    *,
    user_id: int,
    role: str,
    country: str,
) -> None:
    """Register *telegram_user_id* as awaiting admin approval."""
    with _pending_lock:
        _pending_approval[telegram_user_id] = {
            "user_id": user_id,
            "role": role,
            "country": country,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
    logger.info("admin_approval_pending", telegram_user_id=telegram_user_id)


def clear_pending(telegram_user_id: int) -> None:
    """Remove *telegram_user_id* from the pending registry."""
    with _pending_lock:
        _pending_approval.pop(telegram_user_id, None)


def is_pending(telegram_user_id: int) -> bool:
    """Return True if *telegram_user_id* is awaiting approval."""
    with _pending_lock:
        return telegram_user_id in _pending_approval


def list_pending() -> list[dict[str, Any]]:
    """Return a snapshot of all pending-approval entries."""
    with _pending_lock:
        return [
            {"telegram_user_id": tid, **meta}
            for tid, meta in _pending_approval.items()
        ]


# ---------------------------------------------------------------------------
# In-memory metrics accumulator
# ---------------------------------------------------------------------------

# Pricing (USD per 1 000 tokens) — update when pricing changes
_COST_PER_1K: dict[str, dict[str, float]] = {
    "gpt-4.1-nano":    {"input": 0.00010, "output": 0.00040},
    "gpt-4.1-mini":    {"input": 0.00015, "output": 0.00060},
    "gpt-4o-mini":     {"input": 0.00015, "output": 0.00060},
    # Bedrock Claude Sonnet 4.5 (on-demand, us-east-1)
    "anthropic.claude-sonnet-4-5-20251001-v1:0": {"input": 0.003, "output": 0.015},
}


def _estimate_cost(model: str, tokens: int) -> float:
    """Very rough cost estimate assuming tokens are split 60/40 input/output."""
    pricing = _COST_PER_1K.get(model)
    if not pricing:
        return 0.0
    input_tok = int(tokens * 0.6)
    output_tok = tokens - input_tok
    return (input_tok / 1000) * pricing["input"] + (output_tok / 1000) * pricing["output"]


class _MetricsStore:
    """Thread-safe in-memory metrics accumulator."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = datetime.now(timezone.utc)
        # Rolling windows (capped to avoid unbounded memory)
        self._requests: deque[dict[str, Any]] = deque(maxlen=20_000)
        self._llm_calls: deque[dict[str, Any]] = deque(maxlen=20_000)
        self._errors: deque[dict[str, Any]] = deque(maxlen=5_000)
        self._intent_counts: dict[str, int] = defaultdict(int)

    def record_request(
        self,
        *,
        user_id: int | None,
        intent: str | None,
        model_used: str | None,
        tokens_used: int | None,
        latency_ms: int,
    ) -> None:
        """Record one completed user request. Called by the dispatcher."""
        now = time.time()
        entry: dict[str, Any] = {
            "ts": now,
            "user_id": user_id,
            "intent": intent,
            "model": model_used,
            "tokens": tokens_used or 0,
            "latency_ms": latency_ms,
            "cost_usd": _estimate_cost(model_used or "", tokens_used or 0),
        }
        with self._lock:
            self._requests.append(entry)
            if model_used:
                self._llm_calls.append(entry)
            if intent:
                self._intent_counts[intent] += 1

    def record_error(self, *, user_id: int | None, error_type: str) -> None:
        """Record an application error."""
        with self._lock:
            self._errors.append(
                {"ts": time.time(), "user_id": user_id, "error_type": error_type}
            )

    def summary(self, window_hours: int = 24) -> dict[str, Any]:
        """Return a JSON-serialisable summary for the given rolling window."""
        cutoff = time.time() - window_hours * 3600
        now_dt = datetime.now(timezone.utc)

        with self._lock:
            reqs = [r for r in self._requests if r["ts"] >= cutoff]
            errs = [e for e in self._errors if e["ts"] >= cutoff]
            intent_counts = dict(self._intent_counts)

        # Active users
        active_uids_24h = {r["user_id"] for r in reqs if r["user_id"]}
        active_uids_7d = {
            r["user_id"]
            for r in self._requests
            if r["ts"] >= time.time() - 7 * 86400 and r["user_id"]
        }

        # Latency percentiles
        latencies = sorted(r["latency_ms"] for r in reqs)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
        p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0

        # LLM stats
        llm_reqs = [r for r in reqs if r.get("model")]
        total_tokens = sum(r["tokens"] for r in llm_reqs)
        total_cost = sum(r["cost_usd"] for r in llm_reqs)
        avg_latency = (
            sum(r["latency_ms"] for r in llm_reqs) / len(llm_reqs) if llm_reqs else 0
        )

        # Cost by model
        cost_by_model: dict[str, float] = defaultdict(float)
        calls_by_model: dict[str, int] = defaultdict(int)
        for r in llm_reqs:
            cost_by_model[r["model"]] += r["cost_usd"]
            calls_by_model[r["model"]] += 1

        # Error breakdown
        error_by_type: dict[str, int] = defaultdict(int)
        for e in errs:
            error_by_type[e["error_type"]] += 1

        uptime_hours = round(
            (now_dt - self._started_at).total_seconds() / 3600, 1
        )

        return {
            "window_hours": window_hours,
            "generated_at": now_dt.isoformat() + "Z",
            "uptime_hours": uptime_hours,
            "users": {
                "active_24h": len(active_uids_24h),
                "active_7d": len(active_uids_7d),
            },
            "requests": {
                "total": len(reqs),
                "latency_p50_ms": round(p50),
                "latency_p95_ms": round(p95),
                "latency_p99_ms": round(p99),
            },
            "llm": {
                "total_calls": len(llm_reqs),
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 4),
                "avg_latency_ms": round(avg_latency),
                "by_model": {
                    model: {
                        "calls": calls_by_model[model],
                        "cost_usd": round(cost_by_model[model], 4),
                    }
                    for model in calls_by_model
                },
            },
            "intents": dict(intent_counts),
            "errors": {
                "total": len(errs),
                "by_type": dict(error_by_type),
            },
        }

    def daily_digest_text(self) -> str:
        """Format a human-readable daily digest for the Telegram admin chat."""
        s = self.summary(window_hours=24)
        now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

        llm = s["llm"]
        users = s["users"]
        reqs = s["requests"]
        errs = s["errors"]
        intents = s["intents"]

        err_line = (
            f"🔴 *{errs['total']} error(s)*"
            if errs["total"] > 0
            else "✅ No errors"
        )
        if errs["by_type"]:
            breakdown = ", ".join(
                f"{t}: {c}" for t, c in list(errs["by_type"].items())[:3]
            )
            err_line += f" ({breakdown})"

        intent_lines = "\n".join(
            f"  • `{k}`: {v}" for k, v in sorted(intents.items(), key=lambda x: -x[1])
        ) or "  (none)"

        cost_str = (
            f"${llm['total_cost_usd']:.4f}"
            if llm["total_cost_usd"] < 1
            else f"${llm['total_cost_usd']:.2f}"
        )

        return (
            f"📊 *Famosi Daily Digest*\n"
            f"_{now}_\n\n"
            f"👥 *Users*\n"
            f"  Active today: {users['active_24h']}  |  This week: {users['active_7d']}\n\n"
            f"💬 *Requests ({s['window_hours']}h)*\n"
            f"  Total: {reqs['total']}  |  p50: {reqs['latency_p50_ms']}ms"
            f"  |  p95: {reqs['latency_p95_ms']}ms\n\n"
            f"🤖 *LLM*\n"
            f"  Calls: {llm['total_calls']}  |  Tokens: {llm['total_tokens']:,}"
            f"  |  Cost: {cost_str}\n"
            f"  Avg latency: {llm['avg_latency_ms']}ms\n\n"
            f"🎯 *Intents*\n"
            f"{intent_lines}\n\n"
            f"{err_line}\n\n"
            f"_Uptime: {s['uptime_hours']}h_"
        )


# Module-level singleton — import from here:
#   from app.services.admin_service import metrics
metrics = _MetricsStore()


# ---------------------------------------------------------------------------
# DB helpers for admin queries
# ---------------------------------------------------------------------------


async def list_all_users_db(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Return a list of user snapshots for the admin /users command.

    Fields returned per user: id, telegram_user_id, role, country, onboarding_complete,
    subscription_status (joined), approval_pending (in-memory check).

    No health data or personal content is returned.
    """
    stmt = (
        select(
            User.id,
            User.telegram_user_id,
            User.role,
            User.country,
            User.onboarding_complete,
            Subscription.subscription_status,
        )
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .order_by(User.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    rows = result.all()

    with _pending_lock:
        pending_tids = set(_pending_approval)

    return [
        {
            "id": r.id,
            "telegram_user_id": r.telegram_user_id,
            "role": r.role.value if hasattr(r.role, "value") else r.role,
            "country": r.country,
            "onboarding_complete": r.onboarding_complete,
            "subscription_status": (
                r.subscription_status.value
                if r.subscription_status and hasattr(r.subscription_status, "value")
                else (r.subscription_status or "none")
            ),
            "approval_pending": r.telegram_user_id in pending_tids,
        }
        for r in rows
    ]


async def get_user_detail_db(
    db: AsyncSession, telegram_user_id: int
) -> dict[str, Any] | None:
    """Return a single user's detail for the admin /user <id> command."""
    result = await db.execute(
        select(
            User.id,
            User.telegram_user_id,
            User.role,
            User.country,
            User.timezone,
            User.language,
            User.onboarding_complete,
            User.due_date,
            Subscription.subscription_status,
            Subscription.trial_start,
            Subscription.trial_end,
            Subscription.current_period_end,
        )
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .where(User.telegram_user_id == telegram_user_id)
    )
    row = result.one_or_none()
    if row is None:
        return None

    with _pending_lock:
        pending = telegram_user_id in _pending_approval

    return {
        "id": row.id,
        "telegram_user_id": row.telegram_user_id,
        "role": row.role.value if hasattr(row.role, "value") else row.role,
        "country": row.country,
        "timezone": row.timezone,
        "language": row.language,
        "onboarding_complete": row.onboarding_complete,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "subscription_status": (
            row.subscription_status.value
            if row.subscription_status and hasattr(row.subscription_status, "value")
            else (row.subscription_status or "none")
        ),
        "trial_start": row.trial_start.isoformat() if row.trial_start else None,
        "trial_end": row.trial_end.isoformat() if row.trial_end else None,
        "current_period_end": (
            row.current_period_end.isoformat() if row.current_period_end else None
        ),
        "approval_pending": pending,
    }


async def approve_user_db(
    db: AsyncSession,
    telegram_user_id: int,
) -> str:
    """
    Approve a pending user — activate their 7-day trial and clear pending flag.

    Returns a human-readable result string for the admin.
    Raises LookupError if the user is not found.
    """
    result = await db.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise LookupError(f"No user with telegram_user_id={telegram_user_id}")

    from app.payment.state_machine import PaymentStateMachine

    sm = PaymentStateMachine()
    sub = await sm.activate_trial(user.id, db)
    await db.commit()

    clear_pending(telegram_user_id)

    logger.info(
        "admin_user_approved",
        telegram_user_id=telegram_user_id,
        user_id=user.id,
        trial_end=sub.trial_end.isoformat(),
    )
    return (
        f"✅ User `{telegram_user_id}` approved.\n"
        f"Trial runs until *{sub.trial_end.strftime('%d %b %Y')}*."
    )


async def reject_user_db(
    db: AsyncSession,
    telegram_user_id: int,
) -> str:
    """
    Reject a pending user — delete their User record and clear pending flag.

    Returns a human-readable result string.
    """
    result = await db.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise LookupError(f"No user with telegram_user_id={telegram_user_id}")

    await db.delete(user)
    await db.commit()
    clear_pending(telegram_user_id)

    logger.info("admin_user_rejected", telegram_user_id=telegram_user_id)
    return f"🚫 User `{telegram_user_id}` rejected and removed."


async def get_db_stats(db: AsyncSession) -> dict[str, Any]:
    """Return high-level DB stats for the /stats command."""
    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    total_subscriptions = (
        await db.execute(select(func.count(Subscription.id)))
    ).scalar_one()
    active_subs = (
        await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.subscription_status == SubscriptionStatus.active
            )
        )
    ).scalar_one()
    trial_subs = (
        await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.subscription_status == SubscriptionStatus.trial
            )
        )
    ).scalar_one()
    total_requests = (
        await db.execute(select(func.count(RequestLog.id)))
    ).scalar_one()

    with _pending_lock:
        pending_count = len(_pending_approval)

    return {
        "total_users": total_users,
        "pending_approval": pending_count,
        "subscriptions": {
            "total": total_subscriptions,
            "active": active_subs,
            "trial": trial_subs,
        },
        "total_requests_logged": total_requests,
    }
