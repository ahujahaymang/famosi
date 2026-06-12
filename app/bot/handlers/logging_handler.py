"""
Conversational logging handler — entry point for LOGGING-classified intents.

This module implements the full conversational logging flow described in
Requirements 4.1–4.8:

  1. ``handle_logging_intent`` — called by ``dispatcher.py`` for every LOGGING
     intent.  Determines the record type, calls the extraction pipeline, and
     either prompts for missing fields or presents the confirmation summary.

  2. ``_format_summary`` — builds a human-readable summary for each record type.

  3. ``handle_confirm_callback`` — PTB ``CallbackQueryHandler`` for ``confirm:*``
     callbacks (Save / Edit / Cancel).

  4. ``handle_visibility_callback`` — PTB ``CallbackQueryHandler`` for
     ``visibility:*`` callbacks (Private / Partner Shared / Doctor Shared).

  5. ``handle_edit_message`` — PTB ``MessageHandler`` that intercepts free-text
     messages when the user is mid-edit-cycle.

  6. ``get_handlers`` — returns the list of PTB handler objects to register in
     ``app/main.py``.

Privacy contract
----------------
NEVER log message text, food items, symptom names, or any health data.
Only structural fields are logged: user_id, record_type, session_id,
edit_count, telegram_user_id.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel
from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot.keyboards.confirm import (
    CONFIRM_CANCEL,
    CONFIRM_EDIT,
    CONFIRM_SAVE,
    build_confirm_keyboard,
)
from app.bot.keyboards.visibility import (
    INVALID_VISIBILITY_MESSAGE,
    VISIBILITY_DOCTOR,
    VISIBILITY_PARTNER,
    VISIBILITY_PRIVATE,
    build_visibility_keyboard,
    validate_visibility_callback,
)
from app.core.confirmation import ConfirmationSession, ConfirmationStore
from app.core.extractor import MissingFields, extract

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient
    from app.core.intent_router import RouteResult

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Keyword-based record-type heuristic
# ---------------------------------------------------------------------------

# Each tuple: (keywords, record_type)
# Order matters: more specific rules first to avoid false-positive matches.
_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    # question/doctor — explicit ask for doctor (must come before reminder/appointment)
    (("ask doctor", "question for doctor", "ask my doctor", "want to ask",
      "ask my ob", "ask the doctor", "note for doctor", "question for my doctor"), "question"),
    # preference/avoidance — check BEFORE meal so "don't eat X" → preference not meal
    (("don't eat", "do not eat", "won't eat", "can't eat", "not eating", "never eat",
      "don't like", "don't want", "prefer not", "no longer want",
      "allergy", "allergic", "avoid", "dislike", "vegetarian", "vegan",
      "hate ", "i hate", "shellfish"), "preference"),
    # reminder creation — check BEFORE appointment (remind me to... for my scan → reminder)
    (("remind me", "reminder", "set a reminder", "add a reminder", "alert me", "notify me"), "reminder"),
    # appointment scheduling
    (("appointment", "scan", "ultrasound", "ob visit", "bloodwork", "schedule a", "book a"), "appointment"),
    (("symptom", "nausea", "nauseous", "pain", "headache", "cramp", "backache",
       "dizzy", "tired", "exhausted", "fatigue", "vomit", "ache", "bloat",
       "swollen", "spotting", "bleed", "vomiting"), "symptom"),
    (("exercise", "workout", "yoga", "swim", "run"), "exercise"),
    (("walked", "walking"), "exercise"),
    (("medication", "medicine", "pill", "tablet", "supplement", "vitamin", "folic"), "medication"),
    (("weight", "weigh", "kg", "lbs", "pounds"), "weight"),
    (("water", "drink", "hydrat", "ml", "oz", "glass", "glasses", "litre", "litres", "liter", "liters", "cup", "cups"), "water"),
    (("prefer", "meat", "food preference"), "preference"),
    (("meal", "food", "ate", "eat", "breakfast", "lunch", "dinner"), "meal"),
]

# Labels for the visibility levels shown to the user
_VISIBILITY_LABELS: dict[str, str] = {
    VISIBILITY_PRIVATE: "Private 🔒",
    VISIBILITY_PARTNER: "Partner Shared 👫",
    VISIBILITY_DOCTOR: "Doctor Shared 👨‍⚕️",
}

# Map from callback data to VisibilityLevel enum value name
_VISIBILITY_CALLBACK_TO_ENUM: dict[str, str] = {
    VISIBILITY_PRIVATE: "private",
    VISIBILITY_PARTNER: "partner_shared",
    VISIBILITY_DOCTOR: "doctor_shared",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _determine_record_type(user_message: str, route_result: Any) -> str:
    """
    Determine the health record type to extract.

    First checks ``route_result.record_type`` if the attribute exists and is
    non-empty (the intent router may pass a hint).  Falls back to a simple
    keyword search over the user message.  Defaults to ``"meal"`` when no
    keyword matches.

    Parameters
    ----------
    user_message:
        The raw Telegram message text.
    route_result:
        The ``RouteResult`` object from the intent router.  May or may not
        carry a ``record_type`` attribute.

    Returns
    -------
    One of the eight supported record type strings.
    """
    # Check for a hint from the router
    hint: str | None = getattr(route_result, "record_type", None)
    if hint and isinstance(hint, str) and hint.strip():
        return hint.strip().lower()

    # Keyword scan — case-insensitive
    lower_msg = user_message.lower()
    for keywords, record_type in _KEYWORD_RULES:
        if any(kw in lower_msg for kw in keywords):
            return record_type

    return "meal"  # default


def _build_session_id(telegram_user_id: int) -> str:
    """Return the canonical session_id for a Telegram user."""
    return f"log:{telegram_user_id}"


async def _prompt_missing_fields(
    update: Update,
    missing: list[str],
) -> None:
    """
    Send a conversational prompt asking the user for each missing field.

    Formats the missing field names in a readable way and sends a single
    reply asking the user to provide the information.

    Parameters
    ----------
    update:
        Current Telegram Update (message must exist).
    missing:
        List of field name strings that were absent from the extraction.
    """
    if not update.message:
        return

    # Convert snake_case field names to readable labels
    readable = [name.replace("_", " ") for name in missing]
    if len(readable) == 1:
        fields_str = readable[0]
    elif len(readable) == 2:
        fields_str = f"{readable[0]} and {readable[1]}"
    else:
        fields_str = ", ".join(readable[:-1]) + f", and {readable[-1]}"

    await update.message.reply_text(
        f"I need a bit more information. Could you tell me the {fields_str}?"
    )


# ---------------------------------------------------------------------------
# Summary formatter
# ---------------------------------------------------------------------------


def _format_summary(record_type: str, record: BaseModel) -> str:
    """
    Build a human-readable summary of all extracted fields.

    Parameters
    ----------
    record_type:
        One of the eight supported record type strings.
    record:
        The validated Pydantic extraction model.

    Returns
    -------
    A multi-line string suitable for sending as a Telegram message.
    """
    # Avoid accessing health data at the structlog layer — only use for
    # building the user-facing message string.

    if record_type == "meal":
        from app.schemas.meal import MealExtraction
        assert isinstance(record, MealExtraction)
        lines = ["🍽️ Meal logged:"]
        for item in record.items:
            qty_str = f" {item.quantity}" if item.quantity is not None else ""
            unit_str = f" {item.unit}" if item.unit else ""
            lines.append(f"  • {item.food_name}{qty_str}{unit_str}")
        return "\n".join(lines)

    elif record_type == "symptom":
        from app.schemas.symptom import SymptomExtraction
        assert isinstance(record, SymptomExtraction)
        return (
            f"🤒 Symptom: {record.symptom_name}\n"
            f"  Severity: {record.severity}/10\n"
            f"  Frequency: {record.frequency}x per day"
        )

    elif record_type == "exercise":
        from app.schemas.exercise import ExerciseExtraction
        assert isinstance(record, ExerciseExtraction)
        return f"🏃 Exercise: {record.activity_type} for {record.duration_minutes} min"

    elif record_type == "medication":
        from app.schemas.medication import MedicationExtraction
        assert isinstance(record, MedicationExtraction)
        dose_str = f" ({record.dose})" if record.dose else ""
        return f"💊 Medication: {record.medication_name}{dose_str}"

    elif record_type == "weight":
        from app.schemas.weight import WeightExtraction
        assert isinstance(record, WeightExtraction)
        return f"⚖️ Weight: {record.value} {record.unit}"

    elif record_type == "water":
        from app.schemas.water import WaterExtraction
        assert isinstance(record, WaterExtraction)
        return f"💧 Water: {record.volume} {record.unit}"

    elif record_type == "question":
        from app.schemas.question import DoctorQuestionExtraction
        assert isinstance(record, DoctorQuestionExtraction)
        return f"❓ Doctor question: {record.question_text}"

    elif record_type == "preference":
        from app.schemas.preference import PreferenceExtraction
        assert isinstance(record, PreferenceExtraction)
        return f"🥗 Preference: {record.preference_type} - {record.food_item}"

    elif record_type == "appointment":
        from app.schemas.appointment import AppointmentExtraction
        assert isinstance(record, AppointmentExtraction)
        type_labels = {"ob_visit": "OB Visit 👩‍⚕️", "ultrasound": "Ultrasound 🔊", "bloodwork": "Bloodwork 🩸"}
        label = type_labels.get(record.appointment_type, record.appointment_type)
        lines = [f"📅 Appointment: {label}", f"   🗓 {record.datetime_str}"]
        if record.location:
            lines.append(f"   📍 {record.location}")
        if record.notes:
            lines.append(f"   📝 {record.notes}")
        return "\n".join(lines)

    elif record_type == "reminder":
        from app.schemas.reminder import ReminderExtraction
        assert isinstance(record, ReminderExtraction)
        type_labels = {"vitamin": "💊 Vitamin", "meal": "🍽️ Meal", "water": "💧 Water", "exercise": "🏃 Exercise", "appointment": "📅 Appointment"}
        label = type_labels.get(record.reminder_type, record.reminder_type)
        recurring = " (daily)" if record.is_recurring else ""
        return f"⏰ Reminder: {label}{recurring}\n   🕐 {record.datetime_str}\n   📝 {record.message}"

    else:
        # Unknown type — surface all fields generically
        try:
            field_lines = [
                f"  • {k}: {v}" for k, v in record.model_dump().items()
            ]
            return f"📝 {record_type.capitalize()}:\n" + "\n".join(field_lines)
        except Exception:
            return f"📝 {record_type.capitalize()} logged."


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _persist_record(
    record_type: str,
    record: BaseModel,
    user_id: int,
    logged_at: datetime,
) -> int | None:
    """
    Persist a confirmed record via ``personal_memory.py``.

    Visibility defaults to ``private`` (Req 6.2).  Returns the newly created
    record's database ``id``, or ``None`` when the record type does not carry
    an ``id`` (e.g. ``preference`` uses a UNIQUE constraint).

    Parameters
    ----------
    record_type:
        One of the eight supported record type strings.
    record:
        Validated Pydantic extraction model.
    user_id:
        Internal database id of the owning user.
    logged_at:
        UTC timestamp used as both ``logged_at`` and ``confirmed_at``.

    Returns
    -------
    The ``id`` of the persisted ORM object, or ``None`` on failure.
    """
    # Lazy imports to avoid module-level circular dependency
    from app.dependencies import _AsyncSessionFactory  # noqa: PLC0415
    from app.memory import personal_memory  # noqa: PLC0415
    from app.models.meal import VisibilityLevel  # noqa: PLC0415

    vis = VisibilityLevel.private

    try:
        async with _AsyncSessionFactory() as db:
            if record_type == "meal":
                from app.schemas.meal import MealExtraction  # noqa: PLC0415
                assert isinstance(record, MealExtraction)
                obj = await personal_memory.create_meal(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "symptom":
                from app.schemas.symptom import SymptomExtraction  # noqa: PLC0415
                assert isinstance(record, SymptomExtraction)
                obj = await personal_memory.create_symptom(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "exercise":
                from app.schemas.exercise import ExerciseExtraction  # noqa: PLC0415
                assert isinstance(record, ExerciseExtraction)
                obj = await personal_memory.create_exercise(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "medication":
                from app.schemas.medication import MedicationExtraction  # noqa: PLC0415
                assert isinstance(record, MedicationExtraction)
                obj = await personal_memory.create_medication(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "weight":
                from app.schemas.weight import WeightExtraction  # noqa: PLC0415
                assert isinstance(record, WeightExtraction)
                obj = await personal_memory.create_weight_log(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "water":
                from app.schemas.water import WaterExtraction  # noqa: PLC0415
                assert isinstance(record, WaterExtraction)
                obj = await personal_memory.create_water_log(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "question":
                from app.schemas.question import DoctorQuestionExtraction  # noqa: PLC0415
                assert isinstance(record, DoctorQuestionExtraction)
                obj = await personal_memory.create_doctor_question(
                    db, record, user_id, logged_at, vis
                )
            elif record_type == "preference":
                from app.schemas.preference import PreferenceExtraction  # noqa: PLC0415
                assert isinstance(record, PreferenceExtraction)
                obj = await personal_memory.create_preference(
                    db, record, user_id, logged_at
                )
            elif record_type == "appointment":
                from app.schemas.appointment import AppointmentExtraction  # noqa: PLC0415
                from app.components import appointment_tracker  # noqa: PLC0415
                from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
                assert isinstance(record, AppointmentExtraction)
                # Parse the datetime string extracted by the LLM
                appt_dt = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                    try:
                        appt_dt = _dt.strptime(record.datetime_str.strip(), fmt).replace(tzinfo=_tz.utc)
                        break
                    except ValueError:
                        continue
                if appt_dt is None:
                    logger.warning("logging_handler_appointment_datetime_parse_failed")
                    return None
                obj = await appointment_tracker.create_appointment(
                    user_id=user_id,
                    appointment_type=record.appointment_type,
                    appointment_at=appt_dt,
                    location=record.location,
                    notes=record.notes,
                    db=db,
                )
            elif record_type == "reminder":
                from app.schemas.reminder import ReminderExtraction  # noqa: PLC0415
                from app.components import reminder_system  # noqa: PLC0415
                from datetime import datetime as _dt  # noqa: PLC0415
                from app.dependencies import _AsyncSessionFactory as _SF  # noqa: PLC0415, F401
                assert isinstance(record, ReminderExtraction)
                # Parse datetime — extractor injects today's date for relative times
                rem_dt = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                    try:
                        rem_dt = _dt.strptime(record.datetime_str.strip(), fmt)
                        break
                    except ValueError:
                        continue
                if rem_dt is None:
                    logger.warning("logging_handler_reminder_datetime_parse_failed")
                    return None
                # Get timezone from user object in the outer session
                # We need to fetch it from the DB since we only have user_id here
                from sqlalchemy import select as _select  # noqa: PLC0415
                from app.models.user import User as _User  # noqa: PLC0415
                tz_result = await db.execute(_select(_User.timezone).where(_User.id == user_id))
                tz_name = tz_result.scalar_one_or_none()
                obj = await reminder_system.create_reminder(
                    user_id=user_id,
                    reminder_type=record.reminder_type,
                    local_time=rem_dt,
                    message_text=record.message,
                    db=db,
                    timezone_name=tz_name,
                )
            else:
                logger.warning(
                    "logging_handler_unknown_record_type",
                    record_type=record_type,
                    user_id=user_id,
                )
                return None

            await db.commit()
            return getattr(obj, "id", None)

    except Exception:  # noqa: BLE001
        logger.exception(
            "logging_handler_persist_failed",
            record_type=record_type,
            user_id=user_id,
        )
        return None


# ---------------------------------------------------------------------------
# Module-level ConfirmationStore (lazy Redis, falls back to in-process)
# ---------------------------------------------------------------------------

# A single shared store instance; Redis is passed as None so the store uses
# the in-process asyncio-safe fallback until a Redis client is injected at
# startup.  This is intentional per the task spec (item 8).
_store = ConfirmationStore(redis=None)


# ---------------------------------------------------------------------------
# 1. Main entry point — called by dispatcher.py
# ---------------------------------------------------------------------------


async def handle_logging_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    llm_client: "LLMClient",
    route_result: "RouteResult",
) -> dict[str, Any]:
    """
    Entry point for LOGGING intents, called by ``dispatcher.py``.

    Workflow
    --------
    1. Determine the record type (router hint → keyword heuristic → default).
    2. Extract a structured record from the user's message.
    3. If extraction returns ``None``  → reply with a clarification prompt.
    4. If extraction returns ``MissingFields`` → prompt for missing info and
       stash partial state in ``context.user_data`` for follow-up.
    5. If extraction succeeds → build confirmation summary and store a
       ``ConfirmationSession`` keyed by ``f"log:{telegram_user_id}"``.

    Parameters
    ----------
    update:
        Incoming Telegram ``Update``.
    context:
        PTB context carrying ``user_data`` for session persistence.
    llm_client:
        Pre-initialised ``LLMClient`` from the dispatcher.
    route_result:
        Classification result from ``IntentRouter.route()``.

    Returns
    -------
    ``{"model_used": str | None, "tokens_used": int | None}``
    """
    assert update.message is not None
    assert update.effective_user is not None

    telegram_user_id = update.effective_user.id
    user_message: str = (update.message.text or "").strip()

    # Resolve the record type
    record_type = _determine_record_type(user_message, route_result)

    log = logger.bind(
        telegram_user_id=telegram_user_id,
        record_type=record_type,
    )
    log.info("logging_handler_invoked")

    # --- Extraction ---
    try:
        result = await extract(record_type, user_message, llm_client)
    except Exception:  # noqa: BLE001
        log.exception("logging_handler_extract_exception")
        await update.message.reply_text(
            "I encountered an error understanding your message. "
            "Could you rephrase?"
        )
        return {"model_used": None, "tokens_used": None}

    # We need the LLMResponse metadata — re-run a minimal call to get tokens
    # Note: extract() does not surface the LLMResponse directly, so we retrieve
    # model/tokens from the nano tier config.  The dispatcher accumulates usage
    # from this return dict; we return what we can determine.
    # For now surface the nano model name from settings.
    try:
        from app.config import settings  # noqa: PLC0415
        model_used: str | None = settings.llm.router_model  # nano tier model
    except Exception:  # noqa: BLE001
        model_used = None
    tokens_used: int | None = None  # extractor doesn't expose token count

    # --- Handle None → extraction hard failure ---
    if result is None:
        log.info("logging_handler_extract_failed")
        await update.message.reply_text(
            "I couldn't understand that. Could you rephrase?"
        )
        return {"model_used": model_used, "tokens_used": tokens_used}

    # --- Handle MissingFields → conversational follow-up (Req 4.8) ---
    if isinstance(result, MissingFields):
        log.info(
            "logging_handler_missing_fields",
            missing_count=len(result.missing),
        )
        # Store partial state so handle_edit_message can re-extract later
        context.user_data["pending_logging"] = {  # type: ignore[index]
            "record_type": record_type,
            "missing": result.missing,
        }
        await _prompt_missing_fields(update, result.missing)
        return {"model_used": model_used, "tokens_used": tokens_used}

    # --- Full extraction success → show confirmation summary ---
    assert isinstance(result, BaseModel)

    summary = _format_summary(record_type, result)
    session_id = _build_session_id(telegram_user_id)

    session = ConfirmationSession(record=result)
    # Store extra metadata in the record wrapper for use by confirm handler
    # We piggyback record_type inside user_data keyed by session_id
    context.user_data[f"session_meta:{session_id}"] = {  # type: ignore[index]
        "record_type": record_type,
    }

    await _store.put(session_id, session)

    log.info("logging_handler_confirmation_presented", session_id=session_id)

    await update.message.reply_text(
        f"{summary}\n\nLooks right?",
        reply_markup=build_confirm_keyboard(),
    )

    return {"model_used": model_used, "tokens_used": tokens_used}


# ---------------------------------------------------------------------------
# 3. Confirm callback handler
# ---------------------------------------------------------------------------


async def handle_confirm_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Handle ``confirm:save``, ``confirm:edit``, and ``confirm:cancel``
    callback queries.

    Requirements: 4.3, 4.4, 4.5
    """
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id
    session_id = _build_session_id(telegram_user_id)
    callback_data: str = query.data or ""

    log = logger.bind(
        telegram_user_id=telegram_user_id,
        session_id=session_id,
        callback_data=callback_data,
    )

    # Retrieve session
    session = await _store.get(session_id)
    if session is None:
        log.info("logging_handler_confirm_session_not_found")
        await query.edit_message_text(
            "This confirmation has expired. Please try again."
        )
        return

    # Retrieve metadata stored alongside the session
    meta = context.user_data.get(f"session_meta:{session_id}", {})  # type: ignore[union-attr]
    record_type: str = meta.get("record_type", "meal")

    # --- SAVE ---
    if callback_data == CONFIRM_SAVE:
        log.info("logging_handler_confirm_save", record_type=record_type)

        # Retrieve the internal DB user_id if auth middleware stored it.
        # Auth middleware stores the User object under "current_user".
        user_id: int | None = None
        if context.bot_data:
            user_obj = context.bot_data.get("current_user")
            user_id = getattr(user_obj, "id", None)

        # Fallback: attempt to use telegram_user_id as a proxy (unlikely to be
        # needed in practice since auth middleware sets context.bot_data["user"])
        if user_id is None:
            # We cannot persist without a user_id; inform the user
            log.warning(
                "logging_handler_confirm_save_no_user_id",
                telegram_user_id=telegram_user_id,
            )
            await query.edit_message_text(
                "⚠️ Could not save: your user session is not available. "
                "Please send any message to re-authenticate and try again."
            )
            return

        logged_at = datetime.now(timezone.utc)
        record_id = await _persist_record(record_type, session.record, user_id, logged_at)

        # Clean up session
        await _store.delete(session_id)
        context.user_data.pop(f"session_meta:{session_id}", None)  # type: ignore[union-attr]

        log.info(
            "logging_handler_record_saved",
            record_type=record_type,
            record_id=record_id,
        )

        # Confirm saved, then offer visibility upgrade for health records only.
        # Appointments and reminders don't have visibility levels.
        _NO_VISIBILITY_TYPES = {"appointment", "reminder"}
        if record_type in _NO_VISIBILITY_TYPES:
            # For appointment: remind user about auto-set reminders
            if record_type == "appointment":
                await query.edit_message_text(
                    "✅ Appointment saved! Reminders set for 24h and 1h before. 🔔",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text("✅ Reminder set! 🔔")
            return

        await query.edit_message_text("✅ Saved!")

        # Store record info for the visibility callback
        if record_id is not None:
            context.user_data["pending_visibility"] = {  # type: ignore[index]
                "record_id": record_id,
                "record_type": record_type,
                "user_id": user_id,
            }
            await query.message.reply_text(  # type: ignore[union-attr]
                "Would you like to share this record? (default: Private)",
                reply_markup=build_visibility_keyboard(),
            )

    # --- EDIT ---
    elif callback_data == CONFIRM_EDIT:
        session.edit_count += 1

        if session.edit_count >= 3:
            # Third rejection — discard (Req 4.5)
            await _store.delete(session_id)
            context.user_data.pop(f"session_meta:{session_id}", None)  # type: ignore[union-attr]
            log.info(
                "logging_handler_edit_limit_reached",
                edit_count=session.edit_count,
            )
            await query.edit_message_text(
                "Too many edits. Record discarded. Please start over."
            )
            return

        # Update the session with the incremented edit_count
        await _store.put(session_id, session)

        log.info(
            "logging_handler_edit_requested",
            edit_count=session.edit_count,
        )

        # Signal the message handler to intercept the next text message
        context.user_data["pending_edit"] = session_id  # type: ignore[index]

        await query.edit_message_text(
            "Please send your corrected message:"
        )

    # --- CANCEL ---
    elif callback_data == CONFIRM_CANCEL:
        await _store.delete(session_id)
        context.user_data.pop(f"session_meta:{session_id}", None)  # type: ignore[union-attr]

        log.info("logging_handler_cancelled")
        await query.edit_message_text("❌ Record discarded.")

    else:
        log.warning("logging_handler_unknown_confirm_callback")


# ---------------------------------------------------------------------------
# 4. Visibility callback handler
# ---------------------------------------------------------------------------


async def handle_visibility_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Handle ``visibility:private``, ``visibility:partner_shared``, and
    ``visibility:doctor_shared`` callback queries.

    Validates the callback, maps it to a ``VisibilityLevel`` enum value, calls
    ``update_visibility`` in ``personal_memory.py``, and confirms to the user.

    Requirements: 6.2, 6.7
    """
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id
    callback_data: str = query.data or ""

    log = logger.bind(
        telegram_user_id=telegram_user_id,
        callback_data=callback_data,
    )

    # Validate callback payload (Req 6.7)
    if not validate_visibility_callback(callback_data):
        log.warning("logging_handler_invalid_visibility_callback")
        await query.answer(INVALID_VISIBILITY_MESSAGE, show_alert=True)
        return

    # Retrieve pending visibility info stored by handle_confirm_callback
    pending: dict | None = context.user_data.get("pending_visibility")  # type: ignore[union-attr]
    if pending is None:
        log.info("logging_handler_visibility_no_pending")
        await query.edit_message_text(
            "Visibility selection has expired. Your record remains private."
        )
        return

    record_id: int = pending["record_id"]
    record_type: str = pending["record_type"]
    user_id: int = pending["user_id"]

    # Map callback data → VisibilityLevel enum
    from app.models.meal import VisibilityLevel  # noqa: PLC0415

    enum_name = _VISIBILITY_CALLBACK_TO_ENUM[callback_data]
    new_level = VisibilityLevel(enum_name)

    label = _VISIBILITY_LABELS.get(callback_data, enum_name)

    try:
        from app.dependencies import _AsyncSessionFactory  # noqa: PLC0415
        from app.memory import personal_memory  # noqa: PLC0415

        async with _AsyncSessionFactory() as db:
            await personal_memory.update_visibility(
                db, record_id, record_type, new_level, user_id
            )
            await db.commit()

        log.info(
            "logging_handler_visibility_updated",
            record_type=record_type,
            record_id=record_id,
            new_visibility=enum_name,
        )

        # Clear pending state
        context.user_data.pop("pending_visibility", None)  # type: ignore[union-attr]

        # Confirm and remove the keyboard
        await query.edit_message_text(
            f"✅ Visibility updated to {label}."
        )

    except LookupError:
        log.warning(
            "logging_handler_visibility_record_not_found",
            record_id=record_id,
            record_type=record_type,
        )
        await query.edit_message_text(
            "⚠️ Could not find that record to update. It may have been deleted."
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "logging_handler_visibility_update_failed",
            record_id=record_id,
            record_type=record_type,
        )
        await query.edit_message_text(
            "⚠️ Could not update visibility at this time. Please try again later."
        )


# ---------------------------------------------------------------------------
# 5. Edit message handler
# ---------------------------------------------------------------------------


async def handle_edit_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Intercept free-text messages when the user is in an active edit cycle.

    If ``context.user_data["pending_edit"]`` contains a ``session_id``, this
    handler re-extracts from the corrected message, updates the session, and
    re-presents the confirmation summary.

    If no edit is pending, the message is ignored (falls through to normal
    dispatch).
    """
    assert update.message is not None
    assert update.effective_user is not None

    # Only activate during an active edit cycle
    session_id: str | None = context.user_data.get("pending_edit")  # type: ignore[union-attr]
    if not session_id:
        return

    telegram_user_id = update.effective_user.id
    user_message: str = (update.message.text or "").strip()

    log = logger.bind(
        telegram_user_id=telegram_user_id,
        session_id=session_id,
    )

    # Retrieve the existing session
    session = await _store.get(session_id)
    if session is None:
        log.info("logging_handler_edit_session_expired")
        context.user_data.pop("pending_edit", None)  # type: ignore[union-attr]
        await update.message.reply_text(
            "Your edit session has expired. Please start over."
        )
        return

    # Get record_type from the metadata stored alongside the session
    meta = context.user_data.get(f"session_meta:{session_id}", {})  # type: ignore[union-attr]
    record_type: str = meta.get("record_type", "meal")

    log.info("logging_handler_edit_re_extracting", record_type=record_type)

    # Lazy-import LLMClient to avoid module-level instantiation
    try:
        from app.core.llm_client import LLMClient  # noqa: PLC0415
        llm_client = LLMClient()
        result = await extract(record_type, user_message, llm_client)
    except Exception:  # noqa: BLE001
        log.exception("logging_handler_edit_extract_exception")
        await update.message.reply_text(
            "I had trouble understanding that. Could you try again?"
        )
        return

    # Clear pending_edit regardless of outcome
    context.user_data.pop("pending_edit", None)  # type: ignore[union-attr]

    if result is None:
        await update.message.reply_text(
            "I couldn't understand that. Could you rephrase?"
        )
        return

    if isinstance(result, MissingFields):
        log.info(
            "logging_handler_edit_missing_fields",
            missing_count=len(result.missing),
        )
        # Re-enter the pending_edit cycle so the next message is also intercepted
        context.user_data["pending_edit"] = session_id  # type: ignore[index]
        await _prompt_missing_fields(update, result.missing)
        return

    # Full extraction success → update the session record and re-present summary
    assert isinstance(result, BaseModel)
    session.record = result
    await _store.put(session_id, session)

    log.info("logging_handler_edit_updated", edit_count=session.edit_count)

    summary = _format_summary(record_type, result)
    await update.message.reply_text(
        f"{summary}\n\nLooks right?",
        reply_markup=build_confirm_keyboard(),
    )


# ---------------------------------------------------------------------------
# 6. Handler registration
# ---------------------------------------------------------------------------


def get_handlers() -> list:
    """
    Return the list of PTB handler objects ready to register in ``app/main.py``.

    Handlers
    --------
    - ``CallbackQueryHandler`` for ``confirm:*`` callbacks (Save / Edit / Cancel)
    - ``CallbackQueryHandler`` for ``visibility:*`` callbacks
    - ``MessageHandler`` for text messages during an active edit cycle

    Note: the main entry point ``handle_logging_intent`` is called directly
    by the dispatcher and does not need its own PTB handler registration.

    Returns
    -------
    A list of PTB handler instances.
    """
    return [
        # Confirmation inline keyboard callbacks
        CallbackQueryHandler(
            handle_confirm_callback,
            pattern=r"^confirm:",
        ),
        # Visibility inline keyboard callbacks
        CallbackQueryHandler(
            handle_visibility_callback,
            pattern=r"^visibility:",
        ),
        # Text messages during an active edit cycle
        # This handler fires on any non-command text; the handler itself
        # checks for a "pending_edit" key and returns early if absent, so it
        # has negligible overhead on normal messages.
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_edit_message,
        ),
    ]


def register(application: Any) -> None:
    """
    Register all logging-related handlers on *application*.

    Called from ``app.main._register_handlers``.
    Registers the confirm, visibility, and edit handlers returned by
    ``get_handlers()``.
    """
    from telegram.ext import Application as _Application  # noqa: PLC0415
    for handler in get_handlers():
        application.add_handler(handler)
    logger.debug("logging_handlers_registered")


__all__ = [
    "handle_logging_intent",
    "handle_confirm_callback",
    "handle_visibility_callback",
    "handle_edit_message",
    "get_handlers",
    "_format_summary",
    "_determine_record_type",
]
