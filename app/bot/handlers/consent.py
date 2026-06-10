"""
Consent handler for Famosi.

Implements Requirements 2.1–2.5:

  Req 2.1 — Present a Privacy Policy + ToS summary after onboarding completes.
  Req 2.2 — Explicitly list all data processors: Telegram, OpenAI, Famosi
             infrastructure.
  Req 2.3 — On affirmative input: persist a ConsentRecord(user_id,
             policy_version=CURRENT_POLICY_VERSION, accepted_at=utcnow()).
  Req 2.4 — While no valid consent exists: decline to store health data, inform
             the user that consent is required.
  Req 2.5 — On policy version change: re-present the updated policy to users
             whose stored consent version differs from CURRENT_POLICY_VERSION
             before any new health data writes.

Public API
----------
  ``consent_conversation_handler()``
      Returns a :class:`telegram.ext.ConversationHandler` that manages the
      full consent flow.  Register it in the PTB Application *before* other
      handlers so it intercepts interactions from users who have not yet
      consented (or whose consent is stale).

  ``check_consent(telegram_user_id) -> bool``
      Async helper.  Returns ``True`` when the user has a valid (current-
      version) Consent_Record; ``False`` otherwise.  Use this in any handler
      that touches health data.

  ``require_consent(handler_func)``
      Decorator for PTB handler coroutines.  Wraps the function so that it
      checks consent before executing.  If consent is absent or stale the user
      receives the "consent required" message and the wrapped handler is
      **not** invoked.

State machine
-------------
  SHOW_POLICY → (user taps "I Accept")  → END   [record written]
             → (user taps "I Decline")  → END   [no record; user informed]

Logging contract
----------------
  - NEVER log message text, food items, symptom names, or any health data.
  - Log only structural fields: telegram_user_id (numeric), policy_version,
    action taken.
"""

from __future__ import annotations

import functools
from datetime import datetime, timezone
from typing import Callable

import structlog
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config import settings
from app.dependencies import _AsyncSessionFactory
from app.models.consent import ConsentRecord
from app.models.user import User

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Conversation state constant
# ---------------------------------------------------------------------------

SHOW_POLICY: int = 0

# ---------------------------------------------------------------------------
# Privacy Policy text (Req 2.1, 2.2)
# ---------------------------------------------------------------------------

_POLICY_TEXT = """
🔒 *Privacy Policy & Terms of Service — Famosi*
_(Policy version: {version})_

Before we proceed, please read and accept our Privacy Policy and Terms of\
 Service.

*Data we collect*
• Health information you log (meals, symptoms, exercise, etc.)
• Pregnancy details (due date or LMP, gestational age)
• Profile data (country, timezone, language, dietary preferences)
• Conversation messages submitted to the Bot

*Data processors*
Your data is processed by the following entities:

1. 🤖 *Telegram* — message delivery and Telegram account identity
2. 🧠 *OpenAI* — AI processing of your messages to understand intent and \
extract structured records
3. 🏗️ *Famosi infrastructure* — secure storage in our PostgreSQL \
database hosted on AWS EC2 and S3; no data is sold or shared with third \
parties beyond the processors listed above

*Your rights*
You may request deletion of your data at any time by contacting support.  \
Data is retained for the duration of your account and up to 30 days after \
deletion.

*Terms of Service*
By accepting you agree to use Famosi for personal, non-commercial \
purposes only and to provide accurate information.  Famosi is not a \
medical device and does not provide medical advice.  Always consult a \
qualified healthcare provider for medical decisions.

*Consent is required* to store any health data.  You may decline, but the \
Bot will not be able to log or retrieve your personal health records until \
consent is given.
""".strip()

# Text shown when policy version has been updated (Req 2.5)
_POLICY_UPDATE_PREFIX = (
    "⚠️ *Our Privacy Policy has been updated* (new version: {version}).\n\n"
    "Please review and re-accept before continuing to use the Bot.\n\n"
)


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------

def _consent_keyboard() -> InlineKeyboardMarkup:
    """Two-button keyboard: accept or decline."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ I Accept", callback_data="consent:accept"
                ),
                InlineKeyboardButton(
                    "❌ I Decline", callback_data="consent:decline"
                ),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_user_by_telegram_id(telegram_user_id: int) -> User | None:
    """Return the User row for *telegram_user_id*, or None if not found."""
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()


async def _get_latest_consent(user_id: int) -> ConsentRecord | None:
    """Return the most recent ConsentRecord for *user_id*, or None."""
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(ConsentRecord)
            .where(ConsentRecord.user_id == user_id)
            .order_by(ConsentRecord.accepted_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _write_consent_record(user_id: int, policy_version: str) -> ConsentRecord:
    """Insert a new ConsentRecord and return it."""
    async with _AsyncSessionFactory() as session:
        record = ConsentRecord(
            user_id=user_id,
            policy_version=policy_version,
            accepted_at=datetime.now(tz=timezone.utc),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


# ---------------------------------------------------------------------------
# Public helper: check_consent (Req 2.4, 2.5)
# ---------------------------------------------------------------------------

async def check_consent(telegram_user_id: int) -> bool:
    """
    Return ``True`` when the user has a current, valid Consent_Record.

    A record is considered valid when its ``policy_version`` matches
    ``settings.current_policy_version``.

    Parameters
    ----------
    telegram_user_id:
        The Telegram numeric user identifier.

    Returns
    -------
    bool
        ``True``  → user has accepted the current policy version.
        ``False`` → no consent record exists, or the stored version is stale.
    """
    user = await _get_user_by_telegram_id(telegram_user_id)
    if user is None:
        return False

    latest = await _get_latest_consent(user.id)
    if latest is None:
        return False

    return latest.policy_version == settings.current_policy_version


# ---------------------------------------------------------------------------
# Public helper: require_consent decorator (Req 2.4)
# ---------------------------------------------------------------------------

def require_consent(handler_func: Callable) -> Callable:
    """
    Decorator for PTB handler coroutines that gate execution on consent.

    Usage::

        @require_consent
        async def handle_log_meal(update, context):
            ...

    If the user has not accepted the current policy version the handler is
    not called and the user receives the standard "consent required" message.
    The decorated handler must accept ``(update: Update, context:
    ContextTypes.DEFAULT_TYPE)`` as its first two positional arguments.
    """

    @functools.wraps(handler_func)
    async def _wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        assert update.effective_user is not None
        telegram_user_id = update.effective_user.id

        if not await check_consent(telegram_user_id):
            logger.info(
                "consent_required_gate_triggered",
                telegram_user_id=telegram_user_id,
                handler=handler_func.__name__,
            )
            # Inform the user how to provide consent (Req 2.4)
            msg = (
                "🔒 *Consent required.*\n\n"
                "You must accept the Privacy Policy and Terms of Service "
                "before I can store health data.\n\n"
                "Please use /consent to review and accept the policy."
            )
            if update.message:
                await update.message.reply_text(
                    msg, parse_mode="Markdown"
                )
            elif update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.message.reply_text(  # type: ignore[union-attr]
                    msg, parse_mode="Markdown"
                )
            return  # Do not invoke the original handler

        return await handler_func(update, context, *args, **kwargs)

    return _wrapper


# ---------------------------------------------------------------------------
# ConversationHandler entry point
# ---------------------------------------------------------------------------

async def cmd_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    ``/consent`` command — show the Privacy Policy and request acceptance.

    Also used after onboarding completion (Req 2.1) and when the policy
    version has changed (Req 2.5).
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_user_id = update.effective_user.id
    version = settings.current_policy_version

    # Determine if this is a re-consent after a policy update (Req 2.5)
    user = await _get_user_by_telegram_id(telegram_user_id)
    is_policy_update = False
    if user is not None:
        latest = await _get_latest_consent(user.id)
        if latest is not None and latest.policy_version != version:
            is_policy_update = True

    prefix = (
        _POLICY_UPDATE_PREFIX.format(version=version)
        if is_policy_update
        else ""
    )
    policy_body = _POLICY_TEXT.format(version=version)
    full_message = f"{prefix}{policy_body}"

    logger.info(
        "consent_policy_presented",
        telegram_user_id=telegram_user_id,
        policy_version=version,
        is_policy_update=is_policy_update,
    )

    await update.message.reply_text(
        full_message,
        parse_mode="Markdown",
        reply_markup=_consent_keyboard(),
    )
    return SHOW_POLICY


# ---------------------------------------------------------------------------
# Callback handlers for the inline buttons
# ---------------------------------------------------------------------------

async def handle_consent_accept(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle the "I Accept" button press (Req 2.3)."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id
    version = settings.current_policy_version

    # Look up the User row — must exist (onboarding should have created it)
    user = await _get_user_by_telegram_id(telegram_user_id)
    if user is None:
        # Safety net: should not happen in normal flow
        logger.warning(
            "consent_accept_user_not_found",
            telegram_user_id=telegram_user_id,
        )
        await query.edit_message_text(
            "⚠️ Your user profile was not found.  Please restart with /start."
        )
        return ConversationHandler.END

    # Persist the Consent_Record (Req 2.3)
    record = await _write_consent_record(
        user_id=user.id,
        policy_version=version,
    )

    logger.info(
        "consent_accepted",
        telegram_user_id=telegram_user_id,
        user_id=user.id,
        policy_version=version,
        accepted_at=record.accepted_at.isoformat(),
    )

    await query.edit_message_text(
        "✅ *Consent recorded.* Thank you!\n\n"
        f"You have accepted policy version *{version}*.\n"
        "You can now use all features of Famosi.  "
        "Use /help to explore what I can do for you.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def handle_consent_decline(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle the "I Decline" button press (Req 2.4)."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id

    logger.info(
        "consent_declined",
        telegram_user_id=telegram_user_id,
    )

    await query.edit_message_text(
        "❌ *Consent not provided.*\n\n"
        "Without your consent I am unable to store any personal health data.\n\n"
        "You can review and accept the policy at any time by sending /consent.  "
        "Your data will not be stored until consent is given.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def consent_conversation_handler() -> ConversationHandler:
    """
    Build and return the PTB :class:`ConversationHandler` for consent flow.

    Entry points
    ------------
    ``/consent`` — manual or programmatic trigger (Req 2.1, 2.5).

    States
    ------
    ``SHOW_POLICY``
        Awaits the user's inline button press: accept or decline.

    Fallbacks
    ---------
    ``/cancel`` — exit the conversation without recording consent.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("consent", cmd_consent),
        ],
        states={
            SHOW_POLICY: [
                CallbackQueryHandler(
                    handle_consent_accept,
                    pattern=r"^consent:accept$",
                ),
                CallbackQueryHandler(
                    handle_consent_decline,
                    pattern=r"^consent:decline$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _handle_cancel),
        ],
        # Allow the conversation to be re-entered if the user issues /consent
        # again (e.g. after a policy version change).
        allow_reentry=True,
        # Do not persist state across restarts — consent flow is fast.
        name="consent_conversation",
    )


async def _handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Fallback handler for ``/cancel`` inside the consent conversation.

    Cancels without recording consent and informs the user.
    """
    assert update.message is not None
    assert update.effective_user is not None

    logger.info(
        "consent_cancelled",
        telegram_user_id=update.effective_user.id,
    )

    await update.message.reply_text(
        "Consent flow cancelled.  You can return to it at any time with /consent."
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Helper: present policy to a user programmatically (called after onboarding)
# ---------------------------------------------------------------------------

async def present_policy_if_needed(
    telegram_user_id: int, bot_instance
) -> bool:
    """
    Send the Privacy Policy to a user if they have not yet consented (or if
    their stored consent version is stale).

    Intended for use by the onboarding handler immediately after it sets
    ``onboarding_complete = True`` (Req 2.1) and by other components that
    detect a stale consent version (Req 2.5).

    Parameters
    ----------
    telegram_user_id:
        The Telegram numeric user identifier.
    bot_instance:
        A ``telegram.Bot`` instance used to send the message.

    Returns
    -------
    bool
        ``True``  → a policy message was sent (consent was absent/stale).
        ``False`` → the user already has a valid current consent record;
                    no message was sent.
    """
    already_consented = await check_consent(telegram_user_id)
    if already_consented:
        return False

    version = settings.current_policy_version

    # Detect whether this is an update vs. first-time presentation
    user = await _get_user_by_telegram_id(telegram_user_id)
    is_policy_update = False
    if user is not None:
        latest = await _get_latest_consent(user.id)
        if latest is not None and latest.policy_version != version:
            is_policy_update = True

    prefix = (
        _POLICY_UPDATE_PREFIX.format(version=version) if is_policy_update else ""
    )
    policy_body = _POLICY_TEXT.format(version=version)
    full_message = f"{prefix}{policy_body}"

    logger.info(
        "consent_policy_presented_programmatic",
        telegram_user_id=telegram_user_id,
        policy_version=version,
        is_policy_update=is_policy_update,
    )

    await bot_instance.send_message(
        chat_id=telegram_user_id,
        text=full_message,
        parse_mode="Markdown",
        reply_markup=_consent_keyboard(),
    )
    return True


__all__ = [
    "consent_conversation_handler",
    "check_consent",
    "require_consent",
    "present_policy_if_needed",
    "SHOW_POLICY",
]
