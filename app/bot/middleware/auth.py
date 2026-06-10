"""
Authentication and authorisation middleware for the Famosi bot.

Two layers:

Part 1 — FastAPI dependency (``verify_telegram_secret``)
    Validates the ``X-Telegram-Bot-Api-Secret-Token`` request header against
    ``settings.telegram_webhook_secret``.  Applied as a FastAPI ``Depends``
    on the ``POST /webhook`` route.

Part 2 — PTB update-level middleware (``AuthMiddleware``)
    A callable class registered as a ``TypeHandler`` in handler group -1 so
    it executes before every other handler.  It:
      - opens a DB session from the async session factory
      - looks up the ``User`` row by ``telegram_user_id``
      - stores ``current_user`` and ``read_only`` flags on ``context.bot_data``
      - binds the DB ``user.id`` to the structlog context for log correlation

Usage
-----
FastAPI route::

    @router.post("/webhook")
    async def webhook(
        request: Request,
        _: None = Depends(verify_telegram_secret),
    ):
        ...

PTB application setup::

    from telegram.ext import Application, TypeHandler
    from app.bot.middleware.auth import AuthMiddleware

    app = Application.builder().token(...).build()
    auth_mw = AuthMiddleware()
    app.add_handler(TypeHandler(object, auth_mw), group=-1)
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog
import structlog.contextvars
from fastapi import HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import CallbackContext

from app.config import settings
from app.dependencies import _AsyncSessionFactory
from app.models.request_log import RequestLog
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User

# Daily token cap threshold (Req 16.4)
_DAILY_TOKEN_CAP = 100_000
DAILY_TOKEN_CAP = _DAILY_TOKEN_CAP  # public alias for tests / dispatcher
# Number of consecutive cap days that triggers an operator alert
_CONSECUTIVE_CAP_ALERT_DAYS = 3
CONSECUTIVE_CAP_ALERT_DAYS = _CONSECUTIVE_CAP_ALERT_DAYS  # public alias

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Part 1: FastAPI dependency — webhook secret header validation
# ---------------------------------------------------------------------------


async def verify_telegram_secret(request: Request) -> None:
    """
    FastAPI dependency that validates the ``X-Telegram-Bot-Api-Secret-Token``
    header sent by the Telegram servers.

    - Uses :func:`hmac.compare_digest` to prevent timing-based attacks.
    - Raises ``HTTP 403`` on mismatch.
    - Skips validation entirely when ``telegram_webhook_secret`` is not
      configured (useful for local development without a registered secret).

    Raises:
        :class:`fastapi.HTTPException`: status 403 when the header is absent
            or does not match the configured secret.
    """
    secret = settings.telegram_webhook_secret
    if not secret:
        # No secret configured — skip validation (local dev mode).
        return

    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # hmac.compare_digest requires both operands to be the same type.
    if not hmac.compare_digest(received.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Part 2: PTB middleware — user loading and subscription check
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """
    PTB update-level middleware registered at handler group -1.

    On every incoming :class:`telegram.Update` it:

    1. Opens a fresh :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
    2. Loads the :class:`~app.models.user.User` row whose
       ``telegram_user_id`` matches ``update.effective_user.id``.
    3. Writes the result (or ``None``) to ``context.bot_data["current_user"]``.
    4. Checks the linked :class:`~app.models.subscription.Subscription`'s
       ``subscription_status``.  If ``inactive``, sets
       ``context.bot_data["read_only"] = True``; otherwise ``False``.
    5. Binds ``user_id`` (DB primary key) to the structlog context so that
       all log statements emitted during handler execution are correlated.
       Personal information (name, telegram handle, etc.) is **never** bound.
    6. Delegates to the next handler by returning normally (PTB handlers
       registered in higher-numbered groups continue processing).

    Registers as::

        app.add_handler(TypeHandler(object, AuthMiddleware()), group=-1)

    The DB session is always closed in a ``finally`` block.
    """

    async def __call__(self, update: object, context: CallbackContext) -> None:  # type: ignore[override]
        """Process an incoming update: load user, set context state, proceed."""
        if not isinstance(update, Update):
            # Non-Update objects (e.g. TelegramError) — skip user loading.
            return

        effective_user = update.effective_user
        if effective_user is None:
            # Updates without a user (channel posts, etc.) — skip.
            context.bot_data["current_user"] = None
            context.bot_data["read_only"] = False
            context.bot_data["force_mini_tier"] = False
            context.bot_data["daily_cap_note"] = None
            return

        telegram_user_id: int = effective_user.id
        session: AsyncSession = _AsyncSessionFactory()

        try:
            user = await _load_user(session, telegram_user_id)
            context.bot_data["current_user"] = user

            if user is not None:
                # Admin is never read-only regardless of subscription state.
                from app.services.admin_service import is_admin as _is_admin, is_pending as _is_pending
                if _is_admin(telegram_user_id):
                    context.bot_data["read_only"] = False
                    context.bot_data["force_mini_tier"] = False
                    context.bot_data["daily_cap_note"] = None
                    context.bot_data["is_admin"] = True
                    structlog.contextvars.bind_contextvars(user_id=str(user.id))
                else:
                    context.bot_data["is_admin"] = False
                    # Block pending users from using the bot until approved
                    if _is_pending(telegram_user_id):
                        context.bot_data["read_only"] = True
                        context.bot_data["approval_pending"] = True
                    else:
                        context.bot_data["approval_pending"] = False
                        read_only = await _is_read_only(session, user.id)
                        context.bot_data["read_only"] = read_only
                    structlog.contextvars.bind_contextvars(user_id=str(user.id))
                    await _enforce_daily_token_cap(session, user.id, context)
            else:
                context.bot_data["read_only"] = False
                context.bot_data["force_mini_tier"] = False
                context.bot_data["daily_cap_note"] = None
                context.bot_data["is_admin"] = False
                context.bot_data["approval_pending"] = False

        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_user(session: AsyncSession, telegram_user_id: int) -> User | None:
    """Return the ``User`` whose ``telegram_user_id`` matches, or ``None``."""
    result = await session.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    return result.scalar_one_or_none()


async def _is_read_only(session: AsyncSession, user_id: int) -> bool:
    """
    Return ``True`` when the user's subscription status is ``inactive``.

    An inactive subscriber may only issue read-only queries
    (``PERSONAL_DATA_QUERY``, ``KNOWLEDGE_QUESTION``); ``LOGGING`` intent
    must be blocked by the dispatcher.

    If no subscription row exists the user is treated as *not* read-only so
    that newly-created users are not silently locked out before their trial is
    set up.
    """
    result = await session.execute(
        select(Subscription.subscription_status).where(Subscription.user_id == user_id)
    )
    status: SubscriptionStatus | None = result.scalar_one_or_none()
    if status is None:
        return False
    return status == SubscriptionStatus.inactive


async def _get_daily_tokens_used(session: AsyncSession, user_id: int, day: datetime) -> int:
    """
    Return the total ``tokens_used`` for *user_id* on the UTC calendar day
    represented by *day*.

    The query sums ``request_logs.tokens_used`` where the row falls within
    ``[day_start, day_end)`` in UTC.  ``NULL`` ``tokens_used`` values are
    treated as 0 by PostgreSQL's ``SUM`` (which returns ``NULL`` for an
    all-NULL / empty set — coalesced to 0 here).
    """
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    result = await session.execute(
        select(func.coalesce(func.sum(RequestLog.tokens_used), 0)).where(
            RequestLog.user_id == user_id,
            RequestLog.created_at >= day_start,
            RequestLog.created_at < day_end,
        )
    )
    return int(result.scalar_one())


async def _check_consecutive_cap_days(
    session: AsyncSession, user_id: int, today: datetime
) -> bool:
    """
    Return ``True`` when the user has exceeded the daily token cap on *each*
    of the previous ``_CONSECUTIVE_CAP_ALERT_DAYS - 1`` calendar days
    (i.e., the user hit the cap on every day leading up to *today*).

    This is called only after we've confirmed the user already hit the cap
    today, so returning ``True`` means they've hit it on
    ``_CONSECUTIVE_CAP_ALERT_DAYS`` consecutive days in total.

    No PII is logged — only the DB ``user_id`` integer is referenced.
    """
    for offset in range(1, _CONSECUTIVE_CAP_ALERT_DAYS):
        prior_day = today - timedelta(days=offset)
        tokens = await _get_daily_tokens_used(session, user_id, prior_day)
        if tokens <= _DAILY_TOKEN_CAP:
            return False
    return True


async def _enforce_daily_token_cap(
    session: AsyncSession, user_id: int, context: CallbackContext
) -> None:
    """
    Check whether *user_id* has exceeded the daily token cap for today (UTC).

    Side-effects on *context.bot_data*:

    * ``force_mini_tier`` (``bool``) — ``True`` when the cap is exceeded;
      the dispatcher MUST honour this flag by routing the request to the
      Mini tier instead of any higher tier.
    * ``daily_cap_note`` (``str | None``) — A short, user-visible note to
      append to the response when the cap is active; ``None`` otherwise.

    Additionally, if the user has exceeded the cap on
    ``_CONSECUTIVE_CAP_ALERT_DAYS`` consecutive days, a WARNING-level
    structured log entry is emitted for operator review.  Only the opaque
    ``user_id`` integer is included — no PII or message content is logged.
    """
    today = datetime.now(tz=timezone.utc)
    daily_tokens = await _get_daily_tokens_used(session, user_id, today)

    if daily_tokens > _DAILY_TOKEN_CAP:
        context.bot_data["force_mini_tier"] = True
        context.bot_data["daily_cap_note"] = (
            "⚠️ You've reached your daily usage limit. "
            "Responses will use a lighter model until midnight UTC."
        )

        # Check whether operator alert threshold has been reached.
        consecutive = await _check_consecutive_cap_days(session, user_id, today)
        if consecutive:
            logger.warning(
                "daily_token_cap_consecutive_alert",
                user_id=user_id,
                consecutive_days=_CONSECUTIVE_CAP_ALERT_DAYS,
                today_tokens=daily_tokens,
                cap=_DAILY_TOKEN_CAP,
                # Explicitly document what is NOT logged to aid future audits.
                pii_logged=False,
            )
    else:
        context.bot_data["force_mini_tier"] = False
        context.bot_data["daily_cap_note"] = None


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "verify_telegram_secret",
    "AuthMiddleware",
    "_get_daily_tokens_used",
    "_check_consecutive_cap_days",
    "_enforce_daily_token_cap",
    "DAILY_TOKEN_CAP",
    "CONSECUTIVE_CAP_ALERT_DAYS",
]
