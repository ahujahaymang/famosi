"""
FastAPI application factory, structlog configuration, and bot entry point.

Run modes (controlled by BOT_MODE env var):
  polling (default) — python -m app.main
                      Starts python-telegram-bot in long-polling mode.
                      No public URL, domain, or TLS required.
                      Suitable for local dev and EC2 deployments without a domain.

  webhook           — uvicorn app.main:app
                      Starts the FastAPI web server; Telegram pushes updates to
                      POST /webhook. Requires HTTPS and TELEGRAM_WEBHOOK_SECRET.

All FastAPI routes (webhook, payment, jobs, health) are included regardless of
BOT_MODE so that switching modes only requires changing the env var — no code
change needed.

Privacy contract (Requirements 16.1, 16.2):
  - NEVER log raw message content (user text, Telegram message bodies)
  - NEVER log food item names, meal descriptions, or ingredient lists
  - NEVER log symptom names, severity values, or frequency counts
  - NEVER log any raw health data (weight, water intake, medication names, etc.)

  The `_strip_health_data` structlog processor enforces this contract by
  dropping the following keys from every log event before it reaches any
  renderer or transport:
    message_text, user_message, text, food_name, food_item, food_items,
    symptom_name, symptom, symptoms, meal_items, medication_name, medication,
    dose, weight_value, water_volume, question_text, raw_text, health_data,
    extraction, extracted_fields, craving_text
"""

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import structlog
from fastapi import FastAPI

from app.config import settings

# ---------------------------------------------------------------------------
# Sensitive field names that must never appear in logs.
# Add to this set — never remove from it.
# ---------------------------------------------------------------------------
_HEALTH_DATA_FIELDS: frozenset[str] = frozenset(
    {
        # Raw message content
        "message_text",
        "user_message",
        "text",
        # Food / meal data
        "food_name",
        "food_item",
        "food_items",
        "meal_items",
        "craving_text",
        # Symptom data
        "symptom_name",
        "symptom",
        "symptoms",
        # Medication data
        "medication_name",
        "medication",
        "dose",
        # Biometric / measurement data
        "weight_value",
        "water_volume",
        # Free-text health content
        "question_text",
        "raw_text",
        # Aggregated / structured health payloads
        "health_data",
        "extraction",
        "extracted_fields",
    }
)


def _strip_health_data(
    logger: Any,  # noqa: ANN401
    method: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """
    Structlog processor that removes sensitive health-data fields from every
    log event before it is rendered or transmitted.

    This processor is the enforcement layer for the privacy contract described
    in the module docstring.  It operates in-place on `event_dict` and returns
    the sanitised dict so that downstream processors and renderers never see
    protected values.
    """
    for field in _HEALTH_DATA_FIELDS:
        event_dict.pop(field, None)

    # Also strip any key whose name contains health-related substrings,
    # providing a defence-in-depth catch for dynamically named fields.
    keys_to_drop = [
        k
        for k in list(event_dict.keys())
        if any(
            marker in k.lower()
            for marker in ("symptom", "food", "meal", "medication", "health", "craving")
        )
    ]
    for key in keys_to_drop:
        event_dict.pop(key, None)

    return event_dict


def configure_structlog() -> None:
    """
    Configure structlog for production-ready, privacy-safe JSON logging.

    Pipeline (applied in order for every log call):
      1. merge_contextvars    — injects bound context (e.g. request_id, user_id)
      2. add_log_level        — adds "level" field
      3. add_logger_name      — adds "logger" field
      4. TimeStamper(utc)     — adds ISO-8601 UTC "timestamp" field (Req 16.1)
      5. _strip_health_data   — drops all protected health-data fields
      6. StackInfoRenderer    — renders exception stack info if present
      7. JSONRenderer         — serialises the event to a JSON string (Req 16.2)

    The standard-library logging bridge is also configured so that third-party
    libraries (SQLAlchemy, uvicorn, etc.) emit JSON-formatted logs at the same
    level and are subject to the same health-data stripping processor.
    """
    log_level_name: str = settings.log_level.upper()
    log_level: int = getattr(logging, log_level_name, logging.INFO)

    # --- Standard-library root logger -----------------------------------
    # Route stdlib logs through structlog's foreign-logger bridge so all
    # output shares the same JSON format and privacy processors.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _strip_health_data,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Attach a JSON-rendering handler to the root stdlib logger so that
    # foreign-library log records go through the same privacy pipeline.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)


# ---------------------------------------------------------------------------
# Lifespan context manager (webhook mode: DB pool init + webhook registration)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI lifespan context manager.

    Startup tasks (webhook mode only):
      1. Verify the DB connection pool is reachable.
      2. Register the Telegram webhook URL via ``bot.set_webhook()``.

    In polling mode the lifespan handler is a no-op because the PTB
    Application manages its own event loop via ``run_polling()``.

    Shutdown tasks (both modes):
      - Log graceful shutdown; pool cleanup is handled by SQLAlchemy's
        connection pool on process exit.
    """
    log = structlog.get_logger(__name__)

    if settings.bot_mode == "webhook":
        # -- 1. Warm up the DB connection pool --------------------------
        from app.dependencies import _engine

        try:
            from sqlalchemy import text

            async with _engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            log.info("db_pool_initialised", mode="webhook")
        except Exception as exc:
            # Log and continue — the health route will surface DB issues.
            log.error("db_pool_init_failed", error=str(exc), mode="webhook")

        # -- 2. Register the Telegram webhook URL -----------------------
        webhook_url: str | None = settings.telegram_webhook_url
        if not webhook_url:
            log.warning(
                "webhook_url_not_configured",
                hint="Set TELEGRAM_WEBHOOK_URL in .env to enable Telegram push delivery",
                mode="webhook",
            )
        else:
            try:
                from telegram import Bot

                bot = Bot(token=settings.telegram_bot_token)
                kwargs: dict[str, Any] = {"url": webhook_url}
                if settings.telegram_webhook_secret:
                    kwargs["secret_token"] = settings.telegram_webhook_secret
                await bot.set_webhook(**kwargs)
                log.info(
                    "telegram_webhook_registered",
                    # Log only the hostname, not the full URL (may contain secrets).
                    webhook_host=webhook_url.split("/")[2] if "://" in webhook_url else webhook_url,
                    mode="webhook",
                )
            except Exception as exc:
                # Non-fatal on startup — surface in logs; operator can fix and restart.
                log.error(
                    "telegram_webhook_registration_failed",
                    error=str(exc),
                    mode="webhook",
                )
    else:
        log.info("app_startup", mode="polling", note="webhook registration skipped in polling mode")

    yield  # Application is now running — handle requests

    log.info("app_shutdown", mode=settings.bot_mode)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application instance.

    Startup sequence:
      1. Configure structlog (must happen before any log call).
      2. Create FastAPI app with lifespan context manager.
      3. Include all API routers regardless of BOT_MODE.
    """
    configure_structlog()

    log = structlog.get_logger(__name__)
    log.info("structlog_configured", log_level=settings.log_level.upper())

    application = FastAPI(
        title="Famosi",
        description="AI-powered pregnancy copilot delivered via Telegram.",
        version="1.0.0",
        lifespan=lifespan,
        # Disable interactive docs in production as appropriate.
        # Left enabled here so developers can explore the API locally.
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -----------------------------------------------------------------------
    # Router includes — all routers are registered regardless of BOT_MODE.
    # Routes that are irrelevant in a given mode are simply dormant (never
    # called) but must be present so a mode switch requires only an env var
    # change, not a code change.
    #
    # Each import is guarded with try/except so that main.py can be imported
    # and the app created even before the route modules are written.
    # -----------------------------------------------------------------------

    # Health route — GET /health
    try:
        from app.api.routes.health import router as health_router
        application.include_router(health_router, tags=["health"])
        log.debug("router_included", router="health")
    except ImportError:
        log.debug("router_not_yet_available", router="health")

    # Webhook route — POST /webhook (dormant in polling mode)
    try:
        from app.api.routes.webhook import router as webhook_router
        application.include_router(webhook_router, tags=["webhook"])
        log.debug("router_included", router="webhook")
    except ImportError:
        log.debug("router_not_yet_available", router="webhook")

    # Payment route — POST /payment/razorpay/webhook, /payment/stripe/webhook
    try:
        from app.api.routes.payment import router as payment_router
        application.include_router(payment_router, prefix="/payment", tags=["payment"])
        log.debug("router_included", router="payment")
    except ImportError:
        log.debug("router_not_yet_available", router="payment")

    # Jobs route — POST /jobs/reminders, /jobs/pregnancy-update, etc.
    try:
        from app.api.routes.jobs import router as jobs_router
        application.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
        log.debug("router_included", router="jobs")
    except ImportError:
        log.debug("router_not_yet_available", router="jobs")

    return application


# ---------------------------------------------------------------------------
# Module-level app instance (used by uvicorn: uvicorn app.main:app)
# ---------------------------------------------------------------------------
app: FastAPI = create_app()


# ---------------------------------------------------------------------------
# Polling entry point
# ---------------------------------------------------------------------------

def _register_handlers(bot_app: Any) -> None:
    """
    Register all PTB handlers and middleware on the Application instance.

    Handlers are imported conditionally so that this file can be executed
    even when handler modules haven't been written yet.  Each guard logs
    a debug message so developers can see which handlers are active.
    """
    log = structlog.get_logger(__name__)

    # --- Middleware (group -1: runs before all other handlers) ----------
    try:
        from telegram.ext import TypeHandler
        from app.bot.middleware.auth import AuthMiddleware

        bot_app.add_handler(TypeHandler(object, AuthMiddleware()), group=-1)
        log.debug("handler_registered", handler="AuthMiddleware", group=-1)
    except ImportError:
        log.debug("handler_not_yet_available", handler="AuthMiddleware")

    # --- Onboarding handler (task 13) -----------------------------------
    try:
        from app.bot.handlers.onboarding import register as register_onboarding
        register_onboarding(bot_app)
        log.debug("handler_registered", handler="onboarding")
    except ImportError:
        log.debug("handler_not_yet_available", handler="onboarding")

    # --- Consent handler (task 14) --------------------------------------
    try:
        from app.bot.handlers.consent import register as register_consent
        register_consent(bot_app)
        log.debug("handler_registered", handler="consent")
    except ImportError:
        log.debug("handler_not_yet_available", handler="consent")

    # --- Logging handler (task 15) --------------------------------------
    try:
        from app.bot.handlers.logging_handler import register as register_logging
        register_logging(bot_app)
        log.debug("handler_registered", handler="logging_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="logging_handler")

    # --- Query handler (task 16) ----------------------------------------
    try:
        from app.bot.handlers.query_handler import register as register_query
        register_query(bot_app)
        log.debug("handler_registered", handler="query_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="query_handler")

    # --- Knowledge handler (task 17) ------------------------------------
    try:
        from app.bot.handlers.knowledge_handler import register as register_knowledge
        register_knowledge(bot_app)
        log.debug("handler_registered", handler="knowledge_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="knowledge_handler")

    # --- Reminder handler (task 25) -------------------------------------
    try:
        from app.bot.handlers.reminder_handler import register as register_reminders
        register_reminders(bot_app)
        log.debug("handler_registered", handler="reminder_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="reminder_handler")

    # --- Appointment handler (task 24) ----------------------------------
    try:
        from app.bot.handlers.appointment_handler import register as register_appointments
        register_appointments(bot_app)
        log.debug("handler_registered", handler="appointment_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="appointment_handler")

    # --- Payment handler (task 27) --------------------------------------
    try:
        from app.bot.handlers.payment_handler import register as register_payment
        register_payment(bot_app)
        log.debug("handler_registered", handler="payment_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="payment_handler")

    # --- Admin handler ---------------------------------------------------
    # Always registered; the _admin_only decorator silently drops non-admin
    # users so regular users are never exposed to admin commands.
    try:
        from app.bot.handlers.admin_handler import register as register_admin
        register_admin(bot_app)
        log.debug("handler_registered", handler="admin_handler")
    except ImportError:
        log.debug("handler_not_yet_available", handler="admin_handler")

    # --- Catch-all dispatcher (group 1 — runs after ConversationHandlers) ---
    # This is the main intent-routing entry point. Any text message that is
    # NOT consumed by a ConversationHandler above falls through to dispatch().
    # Registered at group 1 so ConversationHandlers (group 0) take priority.
    try:
        from telegram.ext import MessageHandler, filters as tg_filters
        from app.bot.dispatcher import dispatch

        async def _dispatch_handler(update, context):
            await dispatch(update, context)

        bot_app.add_handler(
            MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _dispatch_handler),
            group=1,
        )
        log.debug("handler_registered", handler="dispatcher_catch_all", group=1)
    except ImportError as e:
        log.debug("handler_not_yet_available", handler="dispatcher_catch_all", error=str(e))


async def _start_admin_digest(application: Any) -> None:
    """
    PTB post_init hook: start the daily metrics digest background task.

    Fires at 02:00 UTC daily (≈07:30 IST) and sends the digest to the
    configured ADMIN_TELEGRAM_USER_ID.  Skips silently when no admin is
    configured.
    """
    if not settings.admin_telegram_user_id:
        return

    import asyncio as _asyncio
    from datetime import datetime as _dt, timezone as _tz

    log = structlog.get_logger(__name__)

    async def _digest_loop() -> None:
        while True:
            now = _dt.now(_tz.utc)
            # Next fire at 02:00 UTC
            next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if next_run <= now:
                from datetime import timedelta as _td
                next_run = next_run + _td(days=1)
            wait_secs = (next_run - now).total_seconds()
            log.info(
                "admin_digest_scheduled",
                wait_hours=round(wait_secs / 3600, 1),
                next_run=next_run.strftime("%Y-%m-%d %H:%M UTC"),
            )
            await _asyncio.sleep(wait_secs)
            try:
                from app.services.admin_service import metrics as _metrics
                digest = _metrics.daily_digest_text()
                await application.bot.send_message(
                    chat_id=settings.admin_telegram_user_id,
                    text=digest,
                    parse_mode="Markdown",
                )
                log.info("admin_daily_digest_sent")
            except Exception as exc:
                log.error("admin_daily_digest_failed", error=str(exc))

    _asyncio.create_task(_digest_loop())
    log.info("admin_digest_task_started")


def run_polling() -> None:
    """
    Start python-telegram-bot in long-polling mode.
    This is the primary run mode while the project doesn't have a public
    HTTPS endpoint. No domain or TLS certificate required.

    To run:
        python -m app.main

    PTB's ``Application.run_polling()`` is synchronous — it creates and owns
    its event loop internally.  This function must therefore be a plain
    (non-async) function called directly from ``__main__``, NOT wrapped in
    ``asyncio.run()``.  Wrapping it causes a "This event loop is already
    running" RuntimeError because PTB tries to create a second loop inside
    the one asyncio.run() already started.

    Setup:
      1. Build the PTB Application using ``RequestIdMiddleware`` as the
         application class so every update gets a structured ``request_id``.
      2. Register all handlers and middleware via ``_register_handlers()``.
      3. Start polling — PTB manages the event loop internally.
    """
    from app.bot.middleware.request_id import RequestIdMiddleware
    from telegram.ext import ApplicationBuilder

    log = structlog.get_logger(__name__)
    log.info("bot_starting", mode="polling")

    bot_app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .application_class(RequestIdMiddleware)
        .post_init(_start_admin_digest)
        .build()
    )

    _register_handlers(bot_app)

    log.info("polling_started", allowed_updates=["message", "callback_query"])
    # run_polling() is synchronous — PTB manages its own event loop.
    bot_app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,  # ignore messages sent while bot was offline
    )
if __name__ == "__main__":
    if settings.bot_mode == "polling":
        run_polling()
    else:
        # webhook mode — run via: uvicorn app.main:app --host 0.0.0.0 --port 8000
        import uvicorn
        uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
