"""
PTB middleware: assign a unique request_id to every incoming Telegram update.

Design note
-----------
python-telegram-bot 21.x does not ship a ``BaseMiddleware`` class.
The idiomatic PTB way to run code *before and after* every handler is to
subclass :class:`telegram.ext.Application` and override
:meth:`process_update`.  This gives us a proper try/finally envelope
around the full handler dispatch so we can:

  1. Assign a UUID4 ``request_id`` before any handler runs.
  2. Bind it (and the Telegram user-id) to the structlog contextvars so
     every log event emitted during the request automatically carries these
     fields (Req 16.1).
  3. Clear the context after the update is fully handled (clean slate for
     the next coroutine that reuses this event-loop task).

Privacy contract (Req 16.1 / 16.2):
  - NEVER bind the user's name, username, or any message text.
  - ONLY ``request_id`` and ``telegram_user_id`` (an opaque integer) are
    bound to the structlog context.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
import structlog.contextvars
from telegram import Update
from telegram.ext import Application, CallbackContext

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _update_type(update: object) -> str:
    """
    Return a concise string describing the kind of Telegram update.

    Only inspects structural attributes — never reads message text.
    """
    if not isinstance(update, Update):
        return "unknown"

    if update.message is not None:
        return "message"
    if update.edited_message is not None:
        return "edited_message"
    if update.callback_query is not None:
        return "callback_query"
    if update.inline_query is not None:
        return "inline_query"
    if update.chosen_inline_result is not None:
        return "chosen_inline_result"
    if update.channel_post is not None:
        return "channel_post"
    if update.edited_channel_post is not None:
        return "edited_channel_post"
    if update.pre_checkout_query is not None:
        return "pre_checkout_query"
    if update.shipping_query is not None:
        return "shipping_query"
    if update.poll is not None:
        return "poll"
    if update.poll_answer is not None:
        return "poll_answer"
    if update.my_chat_member is not None:
        return "my_chat_member"
    if update.chat_member is not None:
        return "chat_member"
    if update.chat_join_request is not None:
        return "chat_join_request"
    return "other"


class RequestIdMiddleware(Application):
    """
    A :class:`telegram.ext.Application` subclass that injects a
    ``request_id`` into every update's structlog context.

    Usage — pass this class to :class:`telegram.ext.ApplicationBuilder`::

        from telegram.ext import ApplicationBuilder
        from app.bot.middleware.request_id import RequestIdMiddleware

        app = (
            ApplicationBuilder()
            .token(settings.telegram_bot_token)
            .application_class(RequestIdMiddleware)
            .build()
        )

    Every handler registered on this application will automatically have
    ``request_id`` and (when available) ``telegram_user_id`` present in
    all structlog log events.
    """

    async def process_update(self, update: object) -> None:
        """
        Wrap :meth:`telegram.ext.Application.process_update` with
        structlog context management.

        Steps
        -----
        1. Clear any stale context from a previous coroutine.
        2. Generate a fresh UUID4 ``request_id``.
        3. Bind ``request_id`` and (optionally) ``telegram_user_id``.
        4. Log ``request_started`` at DEBUG level — update type only,
           never message content.
        5. Delegate to the parent ``process_update`` so all registered
           handlers run normally.
        6. Clear the context again in ``finally`` so there is no leakage
           into subsequent work on this coroutine / task.
        """
        # --- 1. Start with a clean slate --------------------------------
        structlog.contextvars.clear_contextvars()

        # --- 2. Assign a unique request identifier ----------------------
        request_id: str = str(uuid.uuid4())

        # --- 3. Bind context variables ----------------------------------
        context_fields: dict[str, Any] = {"request_id": request_id}

        if isinstance(update, Update) and update.effective_user is not None:
            # Only bind the numeric user id — never name or username
            context_fields["telegram_user_id"] = update.effective_user.id

        structlog.contextvars.bind_contextvars(**context_fields)

        # --- 4. Log request start (no message content) ------------------
        update_type: str = _update_type(update)
        log.debug(
            "request_started",
            request_id=request_id,
            update_type=update_type,
        )

        # --- 5. Dispatch to all registered handlers ---------------------
        try:
            await super().process_update(update)
        finally:
            # --- 6. Clean up context ------------------------------------
            structlog.contextvars.clear_contextvars()
