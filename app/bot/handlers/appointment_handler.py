"""
Appointment handler for Famosi — python-telegram-bot 21.x ConversationHandler.

Exposes appointment CRUD through the Telegram bot:

  /appointments  — entry point; shows action menu
  Create flow    — collect type → date/time → location → notes → confirmation
  List flow      — show upcoming appointments; inform user if none exist
  Cancel flow    — list appointments → pick one → Yes/No confirmation
  Reschedule flow — list appointments → pick one → new date/time → confirmation

Conversation states
-------------------
  CREATE_TYPE, CREATE_DATETIME, CREATE_LOCATION, CREATE_NOTES, CREATE_CONFIRM
  CANCEL_PICK, CANCEL_CONFIRM
  RESCHEDULE_PICK, RESCHEDULE_DATETIME, RESCHEDULE_CONFIRM

Public API
----------
  ``register_appointment_handler(application)`` — register all handlers on the
  PTB Application.  Also aliased as ``register`` for consistency with other
  handler modules.

Privacy contract
----------------
NEVER log appointment location, notes, or free-text content.
Log only structural fields: user_id, telegram_user_id, appointment_id,
appointment_type, action.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot.keyboards.confirm import CONFIRM_CANCEL, CONFIRM_SAVE, build_confirm_keyboard
from app.components import appointment_tracker
from app.dependencies import _AsyncSessionFactory
from app.models.appointment import AppointmentType

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Conversation state constants
# ---------------------------------------------------------------------------

(
    MAIN_MENU,
    CREATE_TYPE,
    CREATE_DATETIME,
    CREATE_LOCATION,
    CREATE_NOTES,
    CREATE_CONFIRM,
    CANCEL_PICK,
    CANCEL_CONFIRM,
    RESCHEDULE_PICK,
    RESCHEDULE_DATETIME,
    RESCHEDULE_CONFIRM,
) = range(11)

# Callback data constants
_CB_ACTION_CREATE = "appt:action:create"
_CB_ACTION_LIST = "appt:action:list"
_CB_ACTION_CANCEL = "appt:action:cancel"
_CB_ACTION_RESCHEDULE = "appt:action:reschedule"

_CB_TYPE_PREFIX = "appt:type:"
_CB_APPT_PREFIX = "appt:pick:"
_CB_YES = "appt:confirm:yes"
_CB_NO = "appt:confirm:no"

# context.user_data key for pending appointment data
_DATA_KEY = "appt_data"

# ---------------------------------------------------------------------------
# Appointment type labels
# ---------------------------------------------------------------------------

_TYPE_LABELS: dict[str, str] = {
    AppointmentType.ob_visit.value: "OB Visit 👩‍⚕️",
    AppointmentType.ultrasound.value: "Ultrasound 🔊",
    AppointmentType.bloodwork.value: "Bloodwork 🩸",
}

# ---------------------------------------------------------------------------
# Datetime parsing helpers
# ---------------------------------------------------------------------------

_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M",
]


def _parse_datetime(text: str) -> datetime | None:
    """
    Parse a datetime string in common formats.  Returns a timezone-aware UTC
    datetime, or ``None`` on failure.
    """
    text = text.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# context.user_data helpers
# ---------------------------------------------------------------------------


def _get_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Return (or initialise) the appointment data dict from context."""
    if _DATA_KEY not in context.user_data:  # type: ignore[operator]
        context.user_data[_DATA_KEY] = {}  # type: ignore[index]
    return context.user_data[_DATA_KEY]  # type: ignore[index]


def _clear_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear transient appointment state from context."""
    context.user_data.pop(_DATA_KEY, None)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# DB user_id helper
# ---------------------------------------------------------------------------


async def _get_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """
    Retrieve the internal DB user_id from context.bot_data (set by
    auth middleware under the key "current_user").
    Returns None if not available.
    """
    if context.bot_data:
        user_obj = context.bot_data.get("current_user")
        return getattr(user_obj, "id", None)
    return None


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Four-button main menu: Create, List, Cancel, Reschedule."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 Create", callback_data=_CB_ACTION_CREATE),
                InlineKeyboardButton("📋 List", callback_data=_CB_ACTION_LIST),
            ],
            [
                InlineKeyboardButton("❌ Cancel appt", callback_data=_CB_ACTION_CANCEL),
                InlineKeyboardButton("🔄 Reschedule", callback_data=_CB_ACTION_RESCHEDULE),
            ],
        ]
    )


def _type_keyboard() -> InlineKeyboardMarkup:
    """Appointment type selection keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label, callback_data=f"{_CB_TYPE_PREFIX}{value}"
                )
            ]
            for value, label in _TYPE_LABELS.items()
        ]
    )


def _yes_no_keyboard() -> InlineKeyboardMarkup:
    """Simple Yes / No confirmation keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes", callback_data=_CB_YES),
                InlineKeyboardButton("❌ No", callback_data=_CB_NO),
            ]
        ]
    )


def _appointments_keyboard(appointments: list) -> InlineKeyboardMarkup:
    """
    Build a keyboard listing appointments for the user to pick from.
    Each button shows a short description and encodes the appointment id.
    """
    buttons = []
    for appt in appointments:
        label = _type_labels_short(appt)
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{_CB_APPT_PREFIX}{appt.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(buttons)


def _type_labels_short(appt: Any) -> str:
    """Return a short human-readable label for an appointment."""
    type_label = _TYPE_LABELS.get(appt.appointment_type.value, appt.appointment_type.value)
    dt_str = appt.appointment_at.strftime("%b %d, %H:%M UTC") if appt.appointment_at else "?"
    return f"{type_label} — {dt_str}"


# ---------------------------------------------------------------------------
# Appointment summary formatter
# ---------------------------------------------------------------------------


def _format_appointment_summary(data: dict[str, Any]) -> str:
    """Format a pending appointment dict into a human-readable summary."""
    type_label = _TYPE_LABELS.get(data.get("type", ""), data.get("type", "Unknown"))
    dt_str = data.get("datetime_display", "Not set")
    location = data.get("location") or "Not specified"
    notes = data.get("notes") or "None"

    return (
        f"📅 *Appointment Summary*\n\n"
        f"Type: {type_label}\n"
        f"Date/Time (UTC): {dt_str}\n"
        f"Location: {location}\n"
        f"Notes: {notes}"
    )


def _format_appointment_list(appointments: list) -> str:
    """Format a list of appointments for display."""
    if not appointments:
        return "You have no upcoming appointments. 🗓️"

    lines = ["📋 *Upcoming Appointments*\n"]
    for i, appt in enumerate(appointments, 1):
        type_label = _TYPE_LABELS.get(appt.appointment_type.value, appt.appointment_type.value)
        dt_str = appt.appointment_at.strftime("%B %d, %Y at %H:%M UTC")
        location = f"\n   📍 {appt.location}" if appt.location else ""
        notes = f"\n   📝 {appt.notes}" if appt.notes else ""
        lines.append(f"{i}. {type_label}\n   🗓 {dt_str}{location}{notes}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point: /appointments command
# ---------------------------------------------------------------------------


async def cmd_appointments(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle the /appointments command — show the main menu."""
    assert update.message is not None
    assert update.effective_user is not None

    telegram_user_id = update.effective_user.id
    _clear_data(context)

    logger.bind(telegram_user_id=telegram_user_id).info(
        "appointment_handler_invoked"
    )

    await update.message.reply_text(
        "📅 *Appointment Tracker*\n\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )
    return MAIN_MENU


# ---------------------------------------------------------------------------
# MAIN_MENU dispatch
# ---------------------------------------------------------------------------


async def handle_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Dispatch from the main menu to the selected flow."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    logger.bind(telegram_user_id=telegram_user_id, callback=callback_data).info(
        "appointment_main_menu_action"
    )

    if callback_data == _CB_ACTION_CREATE:
        await query.edit_message_text(
            "What type of appointment is this?",
            reply_markup=_type_keyboard(),
        )
        return CREATE_TYPE

    elif callback_data == _CB_ACTION_LIST:
        return await _handle_list(query, context, telegram_user_id)

    elif callback_data == _CB_ACTION_CANCEL:
        return await _show_appointments_for_action(
            query, context, telegram_user_id, action="cancel"
        )

    elif callback_data == _CB_ACTION_RESCHEDULE:
        return await _show_appointments_for_action(
            query, context, telegram_user_id, action="reschedule"
        )

    else:
        await query.edit_message_text("Unknown action. Please use /appointments.")
        return ConversationHandler.END


# ---------------------------------------------------------------------------
# LIST flow
# ---------------------------------------------------------------------------


async def _handle_list(
    query: Any, context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int
) -> int:
    """Fetch and display upcoming appointments."""
    user_id = await _get_user_id(context)
    if user_id is None:
        await query.edit_message_text(
            "⚠️ Could not retrieve your session. Please send any message to re-authenticate."
        )
        return ConversationHandler.END

    try:
        async with _AsyncSessionFactory() as db:
            appointments = await appointment_tracker.list_upcoming(user_id, db)
    except Exception:  # noqa: BLE001
        logger.exception("appointment_list_failed", telegram_user_id=telegram_user_id)
        await query.edit_message_text(
            "⚠️ Could not retrieve appointments at this time. Please try again later."
        )
        return ConversationHandler.END

    text = _format_appointment_list(appointments)
    await query.edit_message_text(text, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Shared helper: show appointment list for cancel/reschedule actions
# ---------------------------------------------------------------------------


async def _show_appointments_for_action(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    telegram_user_id: int,
    action: str,
) -> int:
    """
    Fetch upcoming appointments and display them as a picker keyboard for
    cancel or reschedule flows.
    """
    user_id = await _get_user_id(context)
    if user_id is None:
        await query.edit_message_text(
            "⚠️ Could not retrieve your session. Please send any message to re-authenticate."
        )
        return ConversationHandler.END

    try:
        async with _AsyncSessionFactory() as db:
            appointments = await appointment_tracker.list_upcoming(user_id, db)
    except Exception:  # noqa: BLE001
        logger.exception(
            "appointment_list_for_action_failed",
            telegram_user_id=telegram_user_id,
            action=action,
        )
        await query.edit_message_text(
            "⚠️ Could not retrieve appointments at this time. Please try again later."
        )
        return ConversationHandler.END

    if not appointments:
        await query.edit_message_text(
            "You have no upcoming appointments to modify. 🗓️"
        )
        return ConversationHandler.END

    # Store action so later steps know what to do
    _get_data(context)["action"] = action
    _get_data(context)["appointments"] = {str(a.id): a for a in appointments}

    action_verb = "cancel" if action == "cancel" else "reschedule"
    await query.edit_message_text(
        f"Which appointment would you like to {action_verb}?",
        reply_markup=_appointments_keyboard(appointments),
    )
    return CANCEL_PICK if action == "cancel" else RESCHEDULE_PICK


# ---------------------------------------------------------------------------
# CREATE flow
# ---------------------------------------------------------------------------


async def handle_create_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle appointment type selection during create flow."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    if not callback_data.startswith(_CB_TYPE_PREFIX):
        await query.edit_message_text(
            "Please select a valid appointment type.",
            reply_markup=_type_keyboard(),
        )
        return CREATE_TYPE

    appt_type = callback_data[len(_CB_TYPE_PREFIX):]

    try:
        AppointmentType(appt_type)  # validate
    except ValueError:
        await query.edit_message_text(
            "Invalid appointment type. Please select from the list.",
            reply_markup=_type_keyboard(),
        )
        return CREATE_TYPE

    data = _get_data(context)
    data["type"] = appt_type

    await query.edit_message_text(
        "📅 What date and time is the appointment?\n\n"
        "Please enter in format: `YYYY-MM-DD HH:MM` (24-hour, UTC)\n"
        "Example: `2025-09-15 14:30`",
        parse_mode="Markdown",
    )
    return CREATE_DATETIME


async def handle_create_datetime(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle date/time text input during create flow."""
    assert update.message is not None
    assert update.effective_user is not None

    text = (update.message.text or "").strip()
    dt = _parse_datetime(text)

    if dt is None:
        await update.message.reply_text(
            "❌ I couldn't understand that date/time. Please use the format:\n"
            "`YYYY-MM-DD HH:MM` (e.g. `2025-09-15 14:30`)",
            parse_mode="Markdown",
        )
        return CREATE_DATETIME

    # Validate it's in the future (Req 12.6)
    if dt <= datetime.now(timezone.utc):
        await update.message.reply_text(
            "❌ The appointment date/time must be in the future. Please try again."
        )
        return CREATE_DATETIME

    data = _get_data(context)
    data["datetime"] = dt.isoformat()
    data["datetime_display"] = dt.strftime("%Y-%m-%d %H:%M UTC")

    await update.message.reply_text(
        "📍 Where is the appointment? (e.g. 'City General Hospital, Room 4')\n\n"
        "Type `skip` to leave blank.",
        parse_mode="Markdown",
    )
    return CREATE_LOCATION


async def handle_create_location(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle location text input during create flow."""
    assert update.message is not None

    text = (update.message.text or "").strip()
    data = _get_data(context)

    if text.lower() == "skip" or not text:
        data["location"] = None
    else:
        data["location"] = text[:200]  # enforce model max length

    await update.message.reply_text(
        "📝 Any notes? (e.g. 'Bring blood test results')\n\n"
        "Type `skip` to leave blank.",
        parse_mode="Markdown",
    )
    return CREATE_NOTES


async def handle_create_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle notes text input and show confirmation summary."""
    assert update.message is not None

    text = (update.message.text or "").strip()
    data = _get_data(context)

    if text.lower() == "skip" or not text:
        data["notes"] = None
    else:
        data["notes"] = text[:1000]  # enforce model max length

    # Show confirmation summary
    summary = _format_appointment_summary(data)
    await update.message.reply_text(
        f"{summary}\n\nLooks right?",
        parse_mode="Markdown",
        reply_markup=build_confirm_keyboard(),
    )
    return CREATE_CONFIRM


async def handle_create_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Save/Cancel inline keyboard buttons during create confirmation."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    telegram_user_id = update.effective_user.id

    if callback_data == CONFIRM_CANCEL:
        _clear_data(context)
        await query.edit_message_text("❌ Appointment creation cancelled.")
        return ConversationHandler.END

    if callback_data == CONFIRM_SAVE:
        user_id = await _get_user_id(context)
        if user_id is None:
            await query.edit_message_text(
                "⚠️ Could not save: your session is unavailable. "
                "Please send any message to re-authenticate and try again."
            )
            _clear_data(context)
            return ConversationHandler.END

        data = _get_data(context)

        try:
            dt = datetime.fromisoformat(data["datetime"])
        except (KeyError, ValueError):
            await query.edit_message_text(
                "⚠️ Invalid datetime in session. Please start again with /appointments."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            async with _AsyncSessionFactory() as db:
                appointment = await appointment_tracker.create_appointment(
                    user_id=user_id,
                    appointment_type=data.get("type", "ob_visit"),
                    appointment_at=dt,
                    location=data.get("location"),
                    notes=data.get("notes"),
                    db=db,
                )
                await db.commit()

            logger.bind(
                telegram_user_id=telegram_user_id,
                appointment_id=appointment.id,
            ).info("appointment_created_via_bot")

            _clear_data(context)
            await query.edit_message_text(
                f"✅ Appointment saved!\n\n{_format_appointment_summary(data)}",
                parse_mode="Markdown",
            )

        except ValueError as e:
            # e.g. datetime in the past (Req 12.6)
            await query.edit_message_text(
                f"❌ Could not save: {e}\n\nPlease try again with /appointments."
            )
            _clear_data(context)

        except Exception:  # noqa: BLE001
            logger.exception(
                "appointment_create_persist_failed",
                telegram_user_id=telegram_user_id,
            )
            await query.edit_message_text(
                "⚠️ Could not save appointment at this time. Please try again later."
            )
            _clear_data(context)

        return ConversationHandler.END

    # Edit: re-prompt from type selection (simplest re-entry path)
    await query.edit_message_text(
        "Let's start over. What type of appointment is this?",
        reply_markup=_type_keyboard(),
    )
    return CREATE_TYPE


# ---------------------------------------------------------------------------
# CANCEL flow
# ---------------------------------------------------------------------------


async def handle_cancel_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle appointment selection for the cancel flow."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    if not callback_data.startswith(_CB_APPT_PREFIX):
        await query.edit_message_text("Please pick an appointment from the list.")
        return CANCEL_PICK

    appt_id_str = callback_data[len(_CB_APPT_PREFIX):]
    try:
        appt_id = int(appt_id_str)
    except ValueError:
        await query.edit_message_text("Invalid selection. Please try again.")
        return CANCEL_PICK

    data = _get_data(context)
    data["selected_appt_id"] = appt_id

    # Look up the appointment details for display
    appts = data.get("appointments", {})
    appt = appts.get(str(appt_id))
    label = _type_labels_short(appt) if appt else f"Appointment #{appt_id}"

    await query.edit_message_text(
        f"Are you sure you want to cancel:\n*{label}*?",
        parse_mode="Markdown",
        reply_markup=_yes_no_keyboard(),
    )
    return CANCEL_CONFIRM


async def handle_cancel_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Yes/No confirmation for appointment cancellation."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    telegram_user_id = update.effective_user.id

    if callback_data == _CB_NO:
        _clear_data(context)
        await query.edit_message_text("Cancellation aborted. Your appointment remains active.")
        return ConversationHandler.END

    if callback_data == _CB_YES:
        user_id = await _get_user_id(context)
        if user_id is None:
            await query.edit_message_text(
                "⚠️ Could not process: your session is unavailable. "
                "Please send any message to re-authenticate."
            )
            _clear_data(context)
            return ConversationHandler.END

        data = _get_data(context)
        appt_id = data.get("selected_appt_id")
        if appt_id is None:
            await query.edit_message_text(
                "⚠️ No appointment selected. Please try again with /appointments."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            async with _AsyncSessionFactory() as db:
                await appointment_tracker.cancel_appointment(appt_id, user_id, db)
                await db.commit()

            logger.bind(
                telegram_user_id=telegram_user_id,
                appointment_id=appt_id,
            ).info("appointment_cancelled_via_bot")

            _clear_data(context)
            await query.edit_message_text(
                "✅ Appointment cancelled and reminders deactivated."
            )

        except ValueError as e:
            await query.edit_message_text(f"❌ Could not cancel: {e}")
            _clear_data(context)

        except Exception:  # noqa: BLE001
            logger.exception(
                "appointment_cancel_failed",
                telegram_user_id=telegram_user_id,
                appointment_id=appt_id,
            )
            await query.edit_message_text(
                "⚠️ Could not cancel appointment at this time. Please try again later."
            )
            _clear_data(context)

        return ConversationHandler.END

    await query.edit_message_text("Please tap Yes or No.")
    return CANCEL_CONFIRM


# ---------------------------------------------------------------------------
# RESCHEDULE flow
# ---------------------------------------------------------------------------


async def handle_reschedule_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle appointment selection for the reschedule flow."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    if not callback_data.startswith(_CB_APPT_PREFIX):
        await query.edit_message_text("Please pick an appointment from the list.")
        return RESCHEDULE_PICK

    appt_id_str = callback_data[len(_CB_APPT_PREFIX):]
    try:
        appt_id = int(appt_id_str)
    except ValueError:
        await query.edit_message_text("Invalid selection. Please try again.")
        return RESCHEDULE_PICK

    data = _get_data(context)
    data["selected_appt_id"] = appt_id

    await query.edit_message_text(
        "📅 What is the new date and time?\n\n"
        "Please enter in format: `YYYY-MM-DD HH:MM` (24-hour, UTC)\n"
        "Example: `2025-10-20 09:00`",
        parse_mode="Markdown",
    )
    return RESCHEDULE_DATETIME


async def handle_reschedule_datetime(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle new date/time text input for reschedule flow."""
    assert update.message is not None

    text = (update.message.text or "").strip()
    dt = _parse_datetime(text)

    if dt is None:
        await update.message.reply_text(
            "❌ I couldn't understand that date/time. Please use the format:\n"
            "`YYYY-MM-DD HH:MM` (e.g. `2025-10-20 09:00`)",
            parse_mode="Markdown",
        )
        return RESCHEDULE_DATETIME

    # Validate it's in the future (Req 12.5 → Req 12.6)
    if dt <= datetime.now(timezone.utc):
        await update.message.reply_text(
            "❌ The new appointment date/time must be in the future. Please try again."
        )
        return RESCHEDULE_DATETIME

    data = _get_data(context)
    data["new_datetime"] = dt.isoformat()
    data["new_datetime_display"] = dt.strftime("%Y-%m-%d %H:%M UTC")

    appts = data.get("appointments", {})
    appt_id = data.get("selected_appt_id")
    appt = appts.get(str(appt_id)) if appt_id else None
    label = _type_labels_short(appt) if appt else f"Appointment #{appt_id}"

    await update.message.reply_text(
        f"Reschedule *{label}* to *{data['new_datetime_display']}*?",
        parse_mode="Markdown",
        reply_markup=_yes_no_keyboard(),
    )
    return RESCHEDULE_CONFIRM


async def handle_reschedule_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Yes/No confirmation for reschedule."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    telegram_user_id = update.effective_user.id

    if callback_data == _CB_NO:
        _clear_data(context)
        await query.edit_message_text("Reschedule cancelled. Appointment unchanged.")
        return ConversationHandler.END

    if callback_data == _CB_YES:
        user_id = await _get_user_id(context)
        if user_id is None:
            await query.edit_message_text(
                "⚠️ Could not process: your session is unavailable. "
                "Please send any message to re-authenticate."
            )
            _clear_data(context)
            return ConversationHandler.END

        data = _get_data(context)
        appt_id = data.get("selected_appt_id")
        new_dt_str = data.get("new_datetime")

        if appt_id is None or new_dt_str is None:
            await query.edit_message_text(
                "⚠️ Session data is incomplete. Please try again with /appointments."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            new_dt = datetime.fromisoformat(new_dt_str)
        except ValueError:
            await query.edit_message_text(
                "⚠️ Invalid datetime in session. Please try again with /appointments."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            async with _AsyncSessionFactory() as db:
                await appointment_tracker.reschedule_appointment(
                    appointment_id=appt_id,
                    new_dt=new_dt,
                    user_id=user_id,
                    db=db,
                )
                await db.commit()

            logger.bind(
                telegram_user_id=telegram_user_id,
                appointment_id=appt_id,
            ).info("appointment_rescheduled_via_bot")

            _clear_data(context)
            new_dt_display = data.get("new_datetime_display", new_dt_str)
            await query.edit_message_text(
                f"✅ Appointment rescheduled to *{new_dt_display}*. "
                f"Reminders have been updated.",
                parse_mode="Markdown",
            )

        except ValueError as e:
            await query.edit_message_text(f"❌ Could not reschedule: {e}")
            _clear_data(context)

        except Exception:  # noqa: BLE001
            logger.exception(
                "appointment_reschedule_failed",
                telegram_user_id=telegram_user_id,
                appointment_id=appt_id,
            )
            await query.edit_message_text(
                "⚠️ Could not reschedule at this time. Please try again later."
            )
            _clear_data(context)

        return ConversationHandler.END

    await query.edit_message_text("Please tap Yes or No.")
    return RESCHEDULE_CONFIRM


# ---------------------------------------------------------------------------
# Cancel fallback handler
# ---------------------------------------------------------------------------


async def _handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle /cancel inside the appointment conversation."""
    if update.message:
        await update.message.reply_text(
            "Appointment flow cancelled. Use /appointments to start again."
        )
    _clear_data(context)
    if update.effective_user:
        logger.bind(telegram_user_id=update.effective_user.id).info(
            "appointment_conversation_cancelled"
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler builder
# ---------------------------------------------------------------------------


def build_appointment_handler() -> ConversationHandler:
    """
    Construct and return the PTB :class:`ConversationHandler` for the
    full appointment CRUD flow.

    Entry point: ``/appointments`` command.

    All inline keyboard callbacks use unique ``appt:`` prefixes to avoid
    clashing with other handlers.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("appointments", cmd_appointments),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(
                    handle_main_menu,
                    pattern=r"^appt:action:",
                ),
            ],
            CREATE_TYPE: [
                CallbackQueryHandler(
                    handle_create_type,
                    pattern=r"^appt:type:",
                ),
            ],
            CREATE_DATETIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_datetime),
            ],
            CREATE_LOCATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_location),
            ],
            CREATE_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_notes),
            ],
            CREATE_CONFIRM: [
                CallbackQueryHandler(
                    handle_create_confirm,
                    pattern=r"^confirm:(save|cancel|edit)$",
                ),
            ],
            CANCEL_PICK: [
                CallbackQueryHandler(
                    handle_cancel_pick,
                    pattern=r"^appt:pick:",
                ),
            ],
            CANCEL_CONFIRM: [
                CallbackQueryHandler(
                    handle_cancel_confirm,
                    pattern=r"^appt:confirm:(yes|no)$",
                ),
            ],
            RESCHEDULE_PICK: [
                CallbackQueryHandler(
                    handle_reschedule_pick,
                    pattern=r"^appt:pick:",
                ),
            ],
            RESCHEDULE_DATETIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handle_reschedule_datetime
                ),
            ],
            RESCHEDULE_CONFIRM: [
                CallbackQueryHandler(
                    handle_reschedule_confirm,
                    pattern=r"^appt:confirm:(yes|no)$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _handle_cancel),
            CommandHandler("appointments", cmd_appointments),  # allow re-entry
        ],
        allow_reentry=True,
        name="appointment_conversation",
        persistent=False,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_appointment_handler(application: Application) -> None:
    """
    Register the appointment :class:`ConversationHandler` on *application*.

    Called from :func:`app.main._register_handlers`.
    """
    handler = build_appointment_handler()
    application.add_handler(handler)
    logger.debug("appointment_handler_registered")


# Alias used by main.py
register = register_appointment_handler


__all__ = [
    # State constants
    "MAIN_MENU",
    "CREATE_TYPE",
    "CREATE_DATETIME",
    "CREATE_LOCATION",
    "CREATE_NOTES",
    "CREATE_CONFIRM",
    "CANCEL_PICK",
    "CANCEL_CONFIRM",
    "RESCHEDULE_PICK",
    "RESCHEDULE_DATETIME",
    "RESCHEDULE_CONFIRM",
    # Handler/registration
    "register_appointment_handler",
    "register",
    "build_appointment_handler",
]
