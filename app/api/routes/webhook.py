"""
FastAPI router for the Telegram webhook endpoint.

This module handles incoming Telegram Update payloads pushed by Telegram's
servers in webhook mode (BOT_MODE=webhook).  In polling mode the endpoint is
registered but never called — Telegram simply never sends POST requests to it.

Design principles:
  - HTTP 200 is always returned within 10 seconds (Req 17.7).
  - Processing is dispatched as a background task so the HTTP response is not
    blocked by handler execution time.
  - The X-Telegram-Bot-Api-Secret-Token header is validated against
    ``settings.telegram_webhook_secret`` before any processing occurs (Req 17.6).
  - Update body is *never* logged (privacy contract, Req 16.1, 16.2).

The module-level ``ptb_application`` variable must be set by ``app/main.py``
(or another startup routine) before any webhook requests arrive in webhook
mode.  In polling mode it remains ``None`` and requests are handled gracefully
with a warning log.

Usage (webhook mode startup)::

    from app.api.routes.webhook import ptb_application, router as webhook_router
    import app.api.routes.webhook as webhook_module

    # After building the PTB Application:
    webhook_module.ptb_application = bot_app
    app.include_router(webhook_router, tags=["webhook"])
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from telegram import Bot, Update

from app.bot.middleware.auth import verify_telegram_secret
from app.config import settings

# ---------------------------------------------------------------------------
# Module-level PTB Application reference
# ---------------------------------------------------------------------------

# Set this variable from app/main.py (or a startup hook) once the PTB
# Application has been built.  In polling mode this stays None.
ptb_application: Any = None  # telegram.ext.Application | None

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Dispatcher import — guarded so main.py can import this module before
# app/bot/dispatcher.py has been written (task 15.1).
# ---------------------------------------------------------------------------

try:
    from app.bot.dispatcher import dispatch  # type: ignore[import]

    _dispatch_available = True
except ImportError:
    _dispatch_available = False
    log.debug(
        "dispatcher_not_yet_available",
        hint="app/bot/dispatcher.py has not been created yet; webhook will no-op dispatch",
    )


# ---------------------------------------------------------------------------
# Background processing helper
# ---------------------------------------------------------------------------


async def _process_update(update: Update) -> None:
    """
    Dispatch a parsed Telegram Update to the PTB Application.

    This coroutine runs as a background task (via ``asyncio.create_task`` or
    FastAPI ``BackgroundTasks``) so it never blocks the HTTP response.

    Behaviour:
    - If ``ptb_application`` is not set (polling mode or pre-init), logs a
      warning and returns immediately.
    - If the dispatcher module is not yet available, logs a warning and
      returns immediately.
    - Otherwise, delegates to ``app.bot.dispatcher.dispatch``.

    Errors raised inside this coroutine are caught and logged; they must not
    propagate to the HTTP layer because the HTTP 200 has already been sent.
    """
    if ptb_application is None:
        log.warning(
            "ptb_application_not_initialised",
            hint="Webhook received but PTB Application is not set; "
            "update will not be processed. "
            "Set webhook_module.ptb_application after building the PTB Application.",
        )
        return

    if not _dispatch_available:
        log.warning(
            "dispatcher_unavailable",
            hint="app/bot/dispatcher.py not found; update will not be processed",
        )
        return

    try:
        await dispatch(update, ptb_application)
    except Exception as exc:  # noqa: BLE001
        # Catch-all: the HTTP 200 has already been returned so we log and swallow.
        log.error(
            "update_dispatch_error",
            error=str(exc),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# POST /webhook
# ---------------------------------------------------------------------------


@router.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_telegram_secret),
) -> Response:
    """
    Receive and dispatch a Telegram Update.

    Flow:
      1. ``verify_telegram_secret`` dependency validates the
         ``X-Telegram-Bot-Api-Secret-Token`` header → 403 on mismatch.
      2. Raw request body is parsed as JSON → 400 on malformed payload.
      3. JSON is deserialised into a ``telegram.Update`` object.
      4. Update is dispatched asynchronously via ``BackgroundTasks`` so this
         handler returns HTTP 200 immediately, well within Telegram's 10-second
         delivery timeout (Req 17.7).

    Returns:
        HTTP 200 ``{"ok": true}`` in all cases except:
        - HTTP 403 when the secret header is absent or wrong.
        - HTTP 400 when the request body is not valid JSON.

    Privacy note:
        The Update body is **never** logged — only metadata such as
        ``update_id`` and ``update_type`` may be logged.
    """
    # -- 1. Parse raw body -------------------------------------------------
    try:
        body: bytes = await request.body()
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("webhook_invalid_json", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    # -- 2. Deserialise into a PTB Update ----------------------------------
    try:
        # Bot instance is required by Update.de_json for some field resolvers.
        # We create a lightweight Bot here solely for deserialisation; it does
        # not make any network call at this point.
        bot = Bot(token=settings.telegram_bot_token)
        update: Update = Update.de_json(data=payload, bot=bot)
    except Exception as exc:  # noqa: BLE001
        log.warning("webhook_deserialisation_error", error=str(exc))
        raise HTTPException(status_code=400, detail="Could not deserialise Update") from exc

    if update is None:
        log.warning("webhook_empty_update")
        # Still return 200 — Telegram may send non-update pings.
        return Response(content='{"ok":true}', media_type="application/json", status_code=200)

    # Log minimal metadata only — never the message body.
    update_type = _update_type(update)
    log.info(
        "webhook_update_received",
        update_id=update.update_id,
        update_type=update_type,
    )

    # -- 3. Dispatch asynchronously ----------------------------------------
    # Use FastAPI BackgroundTasks so the HTTP response is sent first.
    # This satisfies the 10-second hard deadline from Req 17.7 even when
    # handler execution takes longer.
    background_tasks.add_task(_process_update, update)

    # -- 4. Return HTTP 200 immediately ------------------------------------
    return Response(content='{"ok":true}', media_type="application/json", status_code=200)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _update_type(update: Update) -> str:
    """
    Return a safe, non-sensitive string describing the type of the Update.

    This is used for logging purposes only.  It never includes message
    content, user identifiers, or any other PII.
    """
    if update.message is not None:
        return "message"
    if update.edited_message is not None:
        return "edited_message"
    if update.callback_query is not None:
        return "callback_query"
    if update.channel_post is not None:
        return "channel_post"
    if update.edited_channel_post is not None:
        return "edited_channel_post"
    if update.inline_query is not None:
        return "inline_query"
    if update.chosen_inline_result is not None:
        return "chosen_inline_result"
    if update.shipping_query is not None:
        return "shipping_query"
    if update.pre_checkout_query is not None:
        return "pre_checkout_query"
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
    return "unknown"
