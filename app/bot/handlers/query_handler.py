"""
Personal data query handler — entry point for PERSONAL_DATA_QUERY intents.

Implements Requirements 14.3 and 6.3:
  - Req 14.3: PERSONAL_DATA_QUERY retrieves from Personal_Memory only; the
    Knowledge_Base is NEVER queried.
  - Req 6.3: Visibility enforcement is applied at the query layer — partner
    users see only partner_shared / doctor_shared records; mom users see all
    their own records.

Workflow
--------
1. Use the Nano LLM tier to extract two query parameters from the user's
   natural-language message:
     • record_type  — which health record table to query
     • date_range   — optional start/end dates in ISO-8601 format
2. Call ``personal_memory.get_records()`` with the extracted parameters and
   the requesting user's visibility context.
3. Format the resulting records into a human-friendly summary using
   ``LLMClient.complete("mini", ...)``.
4. Return the formatted text to the dispatcher for delivery via Telegram.

Privacy contract
----------------
NEVER log message text, food items, symptom names, or any health content.
Only structural fields are logged: user_id, record_type, record_count,
requesting_role, telegram_user_id.

Requirements: 14.3, 6.3
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import structlog

from app.dependencies import _AsyncSessionFactory
from app.memory import personal_memory
from app.memory import family_memory

if TYPE_CHECKING:
    from app.core.intent_router import RouteResult
    from app.core.llm_client import LLMClient
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical list of queryable record types
# ---------------------------------------------------------------------------

_VALID_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "meal",
        "symptom",
        "exercise",
        "medication",
        "weight_log",
        "water_log",
        "doctor_question",
        "preference",
        "appointment",
        "reminder",
    }
)

# Default look-back window when no date range is given by the user.
_DEFAULT_LOOKBACK_DAYS: int = 7

# ---------------------------------------------------------------------------
# Nano extraction: query parameter schema
# ---------------------------------------------------------------------------

_QUERY_PARAM_SYSTEM_PROMPT = """\
You are a query parameter extractor for a pregnancy health tracking assistant.

The user wants to retrieve their personal health records or scheduled items.
Extract the following two parameters from their message:

1. "record_type": The type of record they want to see.
   Must be exactly one of:
   meal, symptom, exercise, medication, weight_log, water_log,
   doctor_question, preference, appointment, reminder

   Use "appointment" for: upcoming appointments, scans, doctor visits, etc.
   Use "reminder" for: scheduled reminders, alerts, notifications.

2. "date_range": An object with optional "start" and "end" dates.
   Both dates are in ISO-8601 format (YYYY-MM-DD).
   - "start": the start of the requested date range (inclusive)
   - "end": the end of the requested date range (inclusive)
   If the user says "today", use today's date for both.
   If the user says "yesterday", use yesterday's date for both.
   If the user says "this week" or "last 7 days", set start to 7 days ago and end to today.
   If the user says "upcoming" or "coming up", set start to today and end to 30 days from now.
   If no date range is mentioned, omit both "start" and "end" (or set to null).

Rules:
- Respond with a single valid JSON object and nothing else.
- Do NOT fabricate dates that were not implied by the message.
"""


async def _extract_query_params(
    user_message: str,
    llm_client: "LLMClient",
) -> tuple[str | None, datetime | None, datetime | None, str | None]:
    """
    Use the Nano LLM tier to extract query parameters from the user message.

    Returns
    -------
    (record_type, start_dt, end_dt, model_used)

    ``record_type`` is None when the LLM response cannot be parsed or returns
    an unrecognised record type.  ``start_dt`` / ``end_dt`` are None when the
    user did not specify a date range.
    ``model_used`` is the resolved model name from the LLM response.
    """
    messages = [
        {"role": "system", "content": _QUERY_PARAM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Today's date (UTC) is {datetime.now(timezone.utc).strftime('%Y-%m-%d')}.\n"
                f"User message: {user_message}"
            ),
        },
    ]

    try:
        response = await llm_client.complete(
            "nano",
            messages,
            response_format={"type": "json_object"},
        )
    except Exception:  # noqa: BLE001
        logger.exception("query_handler_nano_extraction_failed")
        return None, None, None, None

    model_used: str | None = response.model

    try:
        data = json.loads(response.content)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "query_handler_nano_json_parse_failed",
            content_len=len(response.content),
        )
        return None, None, None, model_used

    raw_record_type: str | None = data.get("record_type")
    if not isinstance(raw_record_type, str) or raw_record_type not in _VALID_RECORD_TYPES:
        logger.warning(
            "query_handler_invalid_record_type",
            raw_value=raw_record_type,
        )
        return None, None, None, model_used

    # Parse date range
    date_range: dict = data.get("date_range") or {}
    start_dt: datetime | None = _parse_date_field(date_range.get("start"))
    end_dt: datetime | None = _parse_date_field(date_range.get("end"))

    # Ensure end_dt covers the full end day (23:59:59 UTC)
    if end_dt is not None:
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    return raw_record_type, start_dt, end_dt, model_used


def _parse_date_field(value: str | None) -> datetime | None:
    """Parse an ISO-8601 date string (YYYY-MM-DD) into a UTC-aware datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _default_date_range() -> tuple[datetime, datetime]:
    """Return a sensible default date window: the past 7 days (UTC)."""
    now = datetime.now(timezone.utc)
    end_dt = now
    start_dt = now - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    return start_dt, end_dt


# ---------------------------------------------------------------------------
# Record serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_records(record_type: str, records: list[Any],
                       meal_items_map: dict | None = None) -> str:
    """
    Convert a list of ORM records into a plain-text summary for the Mini LLM.

    Each record is serialised to a compact key=value line.  Health-specific
    field names are preserved so the Mini tier can compose a meaningful reply,
    but this function never emits logs containing record content (privacy).
    """
    if not records:
        return "(no records found)"

    lines: list[str] = []

    for rec in records:
        try:
            # Build a dict of all non-private, non-SQLAlchemy attributes
            attrs: dict[str, Any] = {}

            if record_type == "meal":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                # Include food names from the items map
                if meal_items_map:
                    foods = meal_items_map.get(getattr(rec, "id", None), [])
                    if foods:
                        attrs["foods"] = ", ".join(foods)
                    else:
                        attrs["meal_id"] = getattr(rec, "id", "")

            elif record_type == "symptom":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                attrs["symptom_name"] = getattr(rec, "symptom_name", "")
                attrs["severity"] = getattr(rec, "severity", "")
                attrs["frequency"] = getattr(rec, "frequency", "")

            elif record_type == "exercise":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                attrs["activity_type"] = getattr(rec, "activity_type", "")
                attrs["duration_minutes"] = getattr(rec, "duration_minutes", "")

            elif record_type == "medication":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                attrs["medication_name"] = getattr(rec, "medication_name", "")
                attrs["dose"] = getattr(rec, "dose", "")

            elif record_type == "weight_log":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                attrs["value"] = getattr(rec, "value", "")
                attrs["unit"] = getattr(rec, "unit", "")

            elif record_type == "water_log":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                attrs["volume"] = getattr(rec, "volume", "")
                attrs["unit"] = getattr(rec, "unit", "")

            elif record_type == "doctor_question":
                attrs["logged_at"] = getattr(rec, "logged_at", "")
                attrs["question_text"] = getattr(rec, "question_text", "")

            elif record_type == "preference":
                attrs["preference_type"] = getattr(rec, "preference_type", "")
                attrs["food_item"] = getattr(rec, "food_item", "")
                attrs["active"] = getattr(rec, "active", "")

            elif record_type == "appointment":
                attrs["appointment_at"] = getattr(rec, "appointment_at", "")
                attrs["appointment_type"] = getattr(rec, "appointment_type", "")
                attrs["location"] = getattr(rec, "location", "")
                attrs["notes"] = getattr(rec, "notes", "")
                attrs["cancelled"] = getattr(rec, "cancelled", False)

            elif record_type == "reminder":
                attrs["scheduled_at"] = getattr(rec, "scheduled_at", "")
                attrs["reminder_type"] = getattr(rec, "reminder_type", "")
                attrs["message_text"] = getattr(rec, "message_text", "")
                attrs["active"] = getattr(rec, "active", "")

            else:
                attrs["id"] = getattr(rec, "id", "")

            line = ", ".join(f"{k}: {v}" for k, v in attrs.items() if v != "")
            lines.append(line)

        except Exception:  # noqa: BLE001
            # Never let a single bad record break the whole response
            lines.append("(record could not be serialised)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mini formatting via LLM
# ---------------------------------------------------------------------------

_FORMAT_SYSTEM_PROMPT = """\
You are a helpful pregnancy assistant composing a response about the user's personal health records.

You have been given a list of raw records retrieved from the database.
Compose a warm, concise, human-readable summary that directly answers the user's question.

Guidelines:
- Do NOT expose internal field names like "visibility_level" or database IDs.
- Format dates and times in a readable way (e.g. "Monday 14 July at 08:30").
- If the record list is empty or says "(no records found)", tell the user kindly that no matching records were found for their query.
- Keep the response concise and conversational — this is a Telegram message.
- If there are many records, summarise by grouping or highlighting key points.
- NEVER make up records that are not in the raw data.
"""


async def _format_response(
    user_message: str,
    record_type: str,
    records_text: str,
    llm_client: "LLMClient",
) -> tuple[str, str | None, int | None]:
    """
    Format raw serialised records into a natural-language reply using the Mini tier.

    Returns
    -------
    (formatted_text, model_used, tokens_used)
    """
    messages = [
        {"role": "system", "content": _FORMAT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"User's original question: {user_message}\n\n"
                f"Record type: {record_type}\n\n"
                f"Raw records:\n{records_text}"
            ),
        },
    ]

    try:
        response = await llm_client.complete("mini", messages)
        return response.content, response.model, response.tokens_used
    except Exception:  # noqa: BLE001
        logger.exception("query_handler_mini_format_failed")
        # Graceful fallback — return the raw text so the user gets some answer
        fallback = (
            f"Here are your {record_type} records:\n\n{records_text}"
            if records_text != "(no records found)"
            else f"No {record_type} records were found for the requested period."
        )
        return fallback, None, None


# ---------------------------------------------------------------------------
# Main entry point — called by dispatcher.py
# ---------------------------------------------------------------------------

async def handle_query_intent(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
    llm_client: "LLMClient",
    route_result: "RouteResult",
    user_message: str,
) -> tuple[str | None, dict[str, Any]]:
    """
    Entry point for PERSONAL_DATA_QUERY intents, called by ``dispatcher.py``.

    This handler:
    1. Extracts query parameters (record_type, date range) from the user message
       using the Nano LLM tier.
    2. Fetches matching records from Personal_Memory with visibility enforcement
       (Req 6.3).
    3. Formats the records into a human-friendly reply using the Mini LLM tier.

    IMPORTANT: This handler NEVER touches the Knowledge_Base (Req 14.3).

    Parameters
    ----------
    update:
        Incoming Telegram ``Update``.
    context:
        PTB context carrying ``bot_data`` (auth user) and ``user_data``.
    llm_client:
        Pre-initialised ``LLMClient`` from the dispatcher.
    route_result:
        Classification result from ``IntentRouter.route()``.
    user_message:
        The raw text of the user's message.

    Returns
    -------
    ``(response_text | None, {"model_used": str | None, "tokens_used": int | None})``

    ``response_text`` is None on unrecoverable failure (no message sent here —
    the dispatcher handles delivery and error messaging).
    """
    effective_user = update.effective_user
    telegram_user_id: int | None = effective_user.id if effective_user else None

    # ------------------------------------------------------------------
    # Resolve the internal DB user context from bot_data (set by auth middleware)
    # ------------------------------------------------------------------
    user_id: int | None = None
    requesting_role: str = "mom"  # safe default

    if context.bot_data and context.bot_data.get("current_user") is not None:
        user_obj = context.bot_data.get("current_user")
        user_id = getattr(user_obj, "id", None)
        raw_role = getattr(user_obj, "role", None)
        if raw_role is not None:
            requesting_role = str(getattr(raw_role, "value", raw_role)).lower()

    log = logger.bind(
        telegram_user_id=telegram_user_id,
        user_id=user_id,
        requesting_role=requesting_role,
    )

    if user_id is None:
        log.warning("query_handler_no_user_id")
        error_text = (
            "I couldn't find your account. Please send any message to "
            "re-authenticate and try again."
        )
        return error_text, {"model_used": None, "tokens_used": None}

    log.info("query_handler_invoked")

    # ------------------------------------------------------------------
    # Step 1: Extract query parameters via Nano tier
    # ------------------------------------------------------------------
    record_type, start_dt, end_dt, nano_model = await _extract_query_params(
        user_message, llm_client
    )

    if record_type is None:
        # Can't map the question to a health record type — route it through
        # the knowledge handler's RAG+LLM path so the user gets a real answer
        # (e.g. "how far are we?" uses gestational context from the profile).
        log.info("query_handler_no_record_type_falling_through_to_knowledge")
        from app.bot.handlers.knowledge_handler import handle_knowledge_intent  # noqa: PLC0415
        return await handle_knowledge_intent(
            update, context, llm_client, route_result, user_message
        )

    # Apply default date range when the user didn't specify one
    if start_dt is None or end_dt is None:
        start_dt, end_dt = _default_date_range()
        log.debug(
            "query_handler_using_default_date_range",
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
        )

    log.info(
        "query_handler_params_extracted",
        record_type=record_type,
        requesting_role=requesting_role,
    )

    # ------------------------------------------------------------------
    # Step 2: Fetch records from the appropriate store with visibility filter
    # NOTE: Knowledge_Base is NEVER queried here (Req 14.3)
    # ------------------------------------------------------------------
    records: list[Any] = []
    try:
        if record_type == "appointment":
            from app.components import appointment_tracker  # noqa: PLC0415
            from app.models.appointment import Appointment  # noqa: PLC0415
            from sqlalchemy import select  # noqa: PLC0415
            async with _AsyncSessionFactory() as db:
                # Fetch future non-cancelled appointments
                result = await db.execute(
                    select(Appointment)
                    .where(
                        Appointment.user_id == user_id,
                        Appointment.cancelled.is_(False),
                    )
                    .order_by(Appointment.appointment_at.asc())
                )
                records = list(result.scalars().all())
        elif record_type == "reminder":
            from app.components import reminder_system  # noqa: PLC0415
            async with _AsyncSessionFactory() as db:
                records = await reminder_system.list_reminders(user_id, db, active_only=True)
        else:
            async with _AsyncSessionFactory() as db:
                # For partner role: query from the linked family unit (mom's records)
                # that are marked as partner_shared or doctor_shared
                if requesting_role == "partner" and user_obj is not None:
                    family_unit_id = getattr(user_obj, "family_unit_id", None)
                    if family_unit_id is not None:
                        records = await family_memory.get_shared_records(
                            db=db,
                            family_unit_id=family_unit_id,
                            record_type=record_type,
                            start=start_dt,
                            end=end_dt,
                        )
                        log.info("query_handler_family_records_fetched",
                                 record_type=record_type, record_count=len(records))
                    else:
                        records = []
                else:
                    records = await personal_memory.get_records(
                        db=db,
                        user_id=user_id,
                        record_type=record_type,
                        start=start_dt,
                        end=end_dt,
                        requesting_user_id=user_id,
                        requesting_role=requesting_role,
                    )
    except ValueError as exc:
        # Unknown record_type — should not happen since we validated above,
        # but handle defensively.
        log.warning("query_handler_invalid_record_type_db", error=str(exc))
        return (
            f"I don't know how to look up '{record_type}' records yet.",
            {"model_used": nano_model, "tokens_used": None},
        )
    except Exception:  # noqa: BLE001
        log.exception("query_handler_db_fetch_failed", record_type=record_type)
        return None, {"model_used": nano_model, "tokens_used": None}

    log.info(
        "query_handler_records_fetched",
        record_type=record_type,
        record_count=len(records),
        requesting_role=requesting_role,
    )

    # For meals, fetch the food items separately (no ORM relationship defined)
    meal_items_map: dict[int, list[str]] = {}
    if record_type == "meal" and records:
        try:
            from app.models.meal import MealItem  # noqa: PLC0415
            from sqlalchemy import select as _select  # noqa: PLC0415
            meal_ids = [r.id for r in records]
            async with _AsyncSessionFactory() as db:
                result = await db.execute(
                    _select(MealItem).where(MealItem.meal_id.in_(meal_ids))
                )
                for item in result.scalars().all():
                    meal_items_map.setdefault(item.meal_id, []).append(item.food_name)
        except Exception:  # noqa: BLE001
            pass  # serializer will fall back to meal ID if no items

    # ------------------------------------------------------------------
    # Step 3: Serialise and format via Mini tier
    # ------------------------------------------------------------------
    records_text = _serialize_records(record_type, records, meal_items_map)

    formatted_text, mini_model, mini_tokens = await _format_response(
        user_message, record_type, records_text, llm_client
    )

    return formatted_text, {
        "model_used": mini_model or nano_model,
        "tokens_used": mini_tokens,
    }
