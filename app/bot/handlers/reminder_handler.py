"""
Reminder handler for Famosi — python-telegram-bot 21.x ConversationHandler.

Exposes reminder CRUD through the Telegram bot:

  /reminders  — entry point; shows action menu
  Create flow — collect type → date/time → message → confirmation (Req 10.4)
  List flow   — show active reminders; inform user if none exist
  Update flow — list reminders → pick one → new date/time → confirmation (Req 10.4)
  Delete flow — list reminders → pick one → Yes/No confirmation (Req 10.5)

Conversation states
-------------------
  MAIN_MENU
  CREATE_TYPE, CREATE_DATETIME, CREATE_MESSAGE, CREATE_CONFIRM
  PICK_REMINDER, UPDATE_DATETIME, UPDATE_CONFIRM
  DELETE_CONFIRM

Public API
----------
  ``register_reminder_handler(application)`` — register all handlers on the
  PTB Application.  Also aliased as ``register`` for consistency with other
  handler modules.

Privacy contract
----------------
NEVER log reminder message text.
Log only structural fields: user_id, telegram_user_id, reminder_id,
reminder_type, action.

Requirements: 10.4, 10.5, 10.6
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
from app.components import reminder_system as rs
from app.dependencies import _AsyncSessionFactory
from app.models.reminder import ReminderType
from app.models.user import User

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Conversation state constants
# ---------------------------------------------------------------------------

(
    MAIN_MENU,
    CREATE_TYPE,
    CREATE_DATETIME,
    CREATE_MESSAGE,
    CREATE_CONFIRM,
    PICK_REMINDER,
    UPDATE_DATETIME,
    UPDATE_CONFIRM,
    DELETE_CONFIRM,
) = range(9)

# Callback data constants
_CB_ACTION_CREATE = "rem:action:create"
_CB_ACTION_LIST = "rem:action:list"
_CB_ACTION_UPDATE = "rem:action:update"
_CB_ACTION_DELETE = "rem:action:delete"

_CB_TYPE_PREFIX = "rem:type:"
_CB_PICK_PREFIX = "rem:pick:"
_CB_YES = "rem:confirm:yes"
_CB_NO = "rem:confirm:no"

# context.user_data key for pending reminder data
_DATA_KEY = "rem_data"

# ---------------------------------------------------------------------------
# Reminder type labels
# ---------------------------------------------------------------------------

_TYPE_LABELS: dict[str, str] = {
    ReminderType.vitamin.value: "💊 Vitamin",
    ReminderType.meal.value: "🍽️ Meal",
    ReminderType.water.value: "💧 Water",
    ReminderType.exercise.value: "🏃 Exercise",
    ReminderType.appointment.value: "📅 Appointment",
}

# ---------------------------------------------------------------------------
# Datetime parsing helpers
# ---------------------------------------------------------------------------

_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M",
    "%H:%M",          # time-only — interpreted as today
]


def _parse_datetime(text: str) -> datetime | None:
    """
    Parse a datetime string in common formats.

    For time-only inputs (HH:MM), the current UTC date is used.
    Returns a naive datetime (timezone will be applied by reminder_system).
    """
    text = text.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                # Attach today's date
                now = datetime.now(timezone.utc)
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            return dt
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# context.user_data helpers
# ---------------------------------------------------------------------------


def _get_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Return (or initialise) the reminder data dict from context."""
    if _DATA_KEY not in context.user_data:  # type: ignore[operator]
        context.user_data[_DATA_KEY] = {}  # type: ignore[index]
    return context.user_data[_DATA_KEY]  # type: ignore[index]


def _clear_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear transient reminder state from context."""
    context.user_data.pop(_DATA_KEY, None)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# DB user helper
# ---------------------------------------------------------------------------


async def _get_user(context: ContextTypes.DEFAULT_TYPE) -> User | None:
    """Retrieve the User ORM object from bot_data (set by auth middleware)."""
    if context.bot_data:
        return context.bot_data.get("current_user")
    return None


async def _get_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return internal DB user_id, or None if unavailable."""
    user = await _get_user(context)
    return getattr(user, "id", None) if user else None


async def _get_user_timezone(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Return the user's IANA timezone string, or None if unavailable."""
    user = await _get_user(context)
    return getattr(user, "timezone", None) if user else None


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Four-button main menu: Create, List, Update, Delete."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Create", callback_data=_CB_ACTION_CREATE),
                InlineKeyboardButton("📋 List", callback_data=_CB_ACTION_LIST),
            ],
            [
                InlineKeyboardButton("✏️ Update", callback_data=_CB_ACTION_UPDATE),
                InlineKeyboardButton("🗑️ Delete", callback_data=_CB_ACTION_DELETE),
            ],
        ]
    )


def _type_keyboard() -> InlineKeyboardMarkup:
    """Reminder type selection keyboard."""
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


def _reminders_keyboard(reminders: list) -> InlineKeyboardMarkup:
    """Build a keyboard listing active reminders for the user to pick from."""
    buttons = []
    for rem in reminders:
        label = _format_reminder_short(rem)
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{_CB_PICK_PREFIX}{rem.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Reminder formatters
# ---------------------------------------------------------------------------


def _format_reminder_short(rem: Any) -> str:
    """Return a short human-readable label for a reminder."""
    type_label = _TYPE_LABELS.get(rem.reminder_type.value, rem.reminder_type.value)
    dt_str = rem.scheduled_at.strftime("%b %d, %H:%M UTC") if rem.scheduled_at else "?"
    return f"{type_label} — {dt_str}"


def _format_reminder_summary(data: dict[str, Any]) -> str:
    """Format a pending reminder dict into a human-readable summary."""
    type_label = _TYPE_LABELS.get(data.get("type", ""), data.get("type", "Unknown"))
    dt_str = data.get("datetime_display", "Not set")
    message = data.get("message") or "Default reminder"

    return (
        f"⏰ *Reminder Summary*\n\n"
        f"Type: {type_label}\n"
        f"Time (local): {dt_str}\n"
        f"Message: {message}"
    )


def _format_reminder_list(reminders: list) -> str:
    """Format a list of active reminders for display."""
    if not reminders:
        return "You have no active reminders. ⏰"

    lines = ["📋 *Your Active Reminders*\n"]
    for i, rem in enumerate(reminders, 1):
        type_label = _TYPE_LABELS.get(rem.reminder_type.value, rem.reminder_type.value)
        dt_str = rem.scheduled_at.strftime("%B %d, %Y at %H:%M UTC")
        lines.append(f"{i}. {type_label}\n   🕐 {dt_str}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point: /reminders command
# ---------------------------------------------------------------------------


async def cmd_reminders(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle the /reminders command — show the main menu."""
    assert update.message is not None
    assert update.effective_user is not None

    telegram_user_id = update.effective_user.id
    _clear_data(context)

    logger.bind(telegram_user_id=telegram_user_id).info(
        "reminder_handler_invoked"
    )

    await update.message.reply_text(
        "⏰ *Reminder Manager*\n\nWhat would you like to do?",
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
        "reminder_main_menu_action"
    )

    if callback_data == _CB_ACTION_CREATE:
        await query.edit_message_text(
            "What type of reminder would you like to create?",
            reply_markup=_type_keyboard(),
        )
        return CREATE_TYPE

    elif callback_data == _CB_ACTION_LIST:
        return await _handle_list(query, context, telegram_user_id)

    elif callback_data == _CB_ACTION_UPDATE:
        return await _show_reminders_for_action(
            query, context, telegram_user_id, action="update"
        )

    elif callback_data == _CB_ACTION_DELETE:
        return await _show_reminders_for_action(
            query, context, telegram_user_id, action="delete"
        )

    else:
        await query.edit_message_text("Unknown action. Please use /reminders.")
        return ConversationHandler.END


# ---------------------------------------------------------------------------
# LIST flow
# ---------------------------------------------------------------------------


async def _handle_list(
    query: Any, context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int
) -> int:
    """Fetch and display active reminders."""
    user_id = await _get_user_id(context)
    if user_id is None:
        await query.edit_message_text(
            "⚠️ Could not retrieve your session. Please send any message to re-authenticate."
        )
        return ConversationHandler.END

    try:
        async with _AsyncSessionFactory() as db:
            reminders = await rs.list_reminders(user_id, db)
    except Exception:  # noqa: BLE001
        logger.exception("reminder_list_failed", telegram_user_id=telegram_user_id)
        await query.edit_message_text(
            "⚠️ Could not retrieve reminders at this time. Please try again later."
        )
        return ConversationHandler.END

    text = _format_reminder_list(reminders)
    await query.edit_message_text(text, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Shared helper: show reminder list for update/delete actions
# ---------------------------------------------------------------------------


async def _show_reminders_for_action(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    telegram_user_id: int,
    action: str,
) -> int:
    """
    Fetch active reminders and display them as a picker keyboard for
    update or delete flows.
    """
    user_id = await _get_user_id(context)
    if user_id is None:
        await query.edit_message_text(
            "⚠️ Could not retrieve your session. Please send any message to re-authenticate."
        )
        return ConversationHandler.END

    try:
        async with _AsyncSessionFactory() as db:
            reminders = await rs.list_reminders(user_id, db)
    except Exception:  # noqa: BLE001
        logger.exception(
            "reminder_list_for_action_failed",
            telegram_user_id=telegram_user_id,
            action=action,
        )
        await query.edit_message_text(
            "⚠️ Could not retrieve reminders at this time. Please try again later."
        )
        return ConversationHandler.END

    if not reminders:
        await query.edit_message_text(
            "You have no active reminders to modify. ⏰"
        )
        return ConversationHandler.END

    # Store action and reminder map for later steps
    data = _get_data(context)
    data["action"] = action
    data["reminders"] = {str(r.id): r for r in reminders}

    action_verb = "update" if action == "update" else "delete"
    await query.edit_message_text(
        f"Which reminder would you like to {action_verb}?",
        reply_markup=_reminders_keyboard(reminders),
    )
    return PICK_REMINDER


# ---------------------------------------------------------------------------
# CREATE flow
# ---------------------------------------------------------------------------


async def handle_create_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle reminder type selection during create flow."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    if not callback_data.startswith(_CB_TYPE_PREFIX):
        await query.edit_message_text(
            "Please select a valid reminder type.",
            reply_markup=_type_keyboard(),
        )
        return CREATE_TYPE

    rem_type = callback_data[len(_CB_TYPE_PREFIX):]

    try:
        ReminderType(rem_type)  # validate
    except ValueError:
        await query.edit_message_text(
            "Invalid reminder type. Please select from the list.",
            reply_markup=_type_keyboard(),
        )
        return CREATE_TYPE

    data = _get_data(context)
    data["type"] = rem_type

    await query.edit_message_text(
        "🕐 What time should the reminder fire?\n\n"
        "Please enter in format: `YYYY-MM-DD HH:MM` or just `HH:MM` for today\n"
        "Example: `2025-09-15 08:00` or `08:00`",
        parse_mode="Markdown",
    )
    return CREATE_DATETIME


async def handle_create_datetime(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle date/time text input during create flow."""
    assert update.message is not None

    text = (update.message.text or "").strip()
    dt = _parse_datetime(text)

    if dt is None:
        await update.message.reply_text(
            "❌ I couldn't understand that time. Please use:\n"
            "`YYYY-MM-DD HH:MM` or `HH:MM` (e.g. `08:00`)",
            parse_mode="Markdown",
        )
        return CREATE_DATETIME

    data = _get_data(context)
    data["datetime"] = dt.isoformat()
    data["datetime_display"] = dt.strftime("%Y-%m-%d %H:%M (local)")

    # Prompt for custom message or allow default
    type_label = _TYPE_LABELS.get(data.get("type", ""), "Reminder")
    await update.message.reply_text(
        f"📝 What should the reminder message say?\n\n"
        f"Type a message or `skip` to use the default: _{type_label} time!_",
        parse_mode="Markdown",
    )
    return CREATE_MESSAGE


async def handle_create_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle custom message text and show confirmation summary."""
    assert update.message is not None

    text = (update.message.text or "").strip()
    data = _get_data(context)

    if text.lower() == "skip" or not text:
        type_label = _TYPE_LABELS.get(data.get("type", ""), "Reminder")
        data["message"] = f"{type_label} time!"
    else:
        data["message"] = text[:500]  # reasonable max length

    summary = _format_reminder_summary(data)
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
        await query.edit_message_text("❌ Reminder creation cancelled.")
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

        tz_name = await _get_user_timezone(context)
        data = _get_data(context)

        try:
            local_dt = datetime.fromisoformat(data["datetime"])
        except (KeyError, ValueError):
            await query.edit_message_text(
                "⚠️ Invalid datetime in session. Please start again with /reminders."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            async with _AsyncSessionFactory() as db:
                reminder = await rs.create_reminder(
                    user_id=user_id,
                    reminder_type=data.get("type", "vitamin"),
                    local_time=local_dt,
                    message_text=data.get("message", "Reminder!"),
                    db=db,
                    timezone_name=tz_name,
                )
                await db.commit()

            logger.bind(
                telegram_user_id=telegram_user_id,
                reminder_id=reminder.id,
            ).info("reminder_created_via_bot")

            _clear_data(context)
            await query.edit_message_text(
                f"✅ Reminder saved!\n\n{_format_reminder_summary(data)}",
                parse_mode="Markdown",
            )

        except rs.TimezoneNotSetError as e:
            await query.edit_message_text(
                f"❌ {e}\n\n"
                "Please update your timezone via onboarding and try again."
            )
            _clear_data(context)

        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder_create_persist_failed",
                telegram_user_id=telegram_user_id,
            )
            await query.edit_message_text(
                "⚠️ Could not save reminder at this time. Please try again later."
            )
            _clear_data(context)

        return ConversationHandler.END

    # Edit: re-prompt from type selection
    await query.edit_message_text(
        "Let's start over. What type of reminder would you like to create?",
        reply_markup=_type_keyboard(),
    )
    return CREATE_TYPE


# ---------------------------------------------------------------------------
# PICK flow (shared between update and delete)
# ---------------------------------------------------------------------------


async def handle_pick_reminder(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle reminder selection for the update or delete flow."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    if not callback_data.startswith(_CB_PICK_PREFIX):
        await query.edit_message_text("Please pick a reminder from the list.")
        return PICK_REMINDER

    rem_id_str = callback_data[len(_CB_PICK_PREFIX):]
    try:
        rem_id = int(rem_id_str)
    except ValueError:
        await query.edit_message_text("Invalid selection. Please try again.")
        return PICK_REMINDER

    data = _get_data(context)
    data["selected_rem_id"] = rem_id

    action = data.get("action", "delete")
    rems = data.get("reminders", {})
    rem = rems.get(str(rem_id))
    label = _format_reminder_short(rem) if rem else f"Reminder #{rem_id}"

    if action == "delete":
        await query.edit_message_text(
            f"Are you sure you want to delete:\n*{label}*?",
            parse_mode="Markdown",
            reply_markup=_yes_no_keyboard(),
        )
        return DELETE_CONFIRM
    else:
        # update flow — ask for new time
        await query.edit_message_text(
            f"Updating: *{label}*\n\n"
            "📅 What is the new time?\n\n"
            "Please enter in format: `YYYY-MM-DD HH:MM` or `HH:MM` for today",
            parse_mode="Markdown",
        )
        return UPDATE_DATETIME


# ---------------------------------------------------------------------------
# UPDATE flow
# ---------------------------------------------------------------------------


async def handle_update_datetime(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle new date/time text input for update flow."""
    assert update.message is not None

    text = (update.message.text or "").strip()
    dt = _parse_datetime(text)

    if dt is None:
        await update.message.reply_text(
            "❌ I couldn't understand that time. Please use:\n"
            "`YYYY-MM-DD HH:MM` or `HH:MM` (e.g. `08:00`)",
            parse_mode="Markdown",
        )
        return UPDATE_DATETIME

    data = _get_data(context)
    data["new_datetime"] = dt.isoformat()
    data["new_datetime_display"] = dt.strftime("%Y-%m-%d %H:%M (local)")

    rems = data.get("reminders", {})
    rem_id = data.get("selected_rem_id")
    rem = rems.get(str(rem_id)) if rem_id else None
    label = _format_reminder_short(rem) if rem else f"Reminder #{rem_id}"

    await update.message.reply_text(
        f"Reschedule *{label}* to *{data['new_datetime_display']}*?",
        parse_mode="Markdown",
        reply_markup=_yes_no_keyboard(),
    )
    return UPDATE_CONFIRM


async def handle_update_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Yes/No confirmation for reminder reschedule."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    telegram_user_id = update.effective_user.id

    if callback_data == _CB_NO:
        _clear_data(context)
        await query.edit_message_text("Update cancelled. Reminder unchanged.")
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

        tz_name = await _get_user_timezone(context)
        data = _get_data(context)
        rem_id = data.get("selected_rem_id")
        new_dt_str = data.get("new_datetime")

        if rem_id is None or new_dt_str is None:
            await query.edit_message_text(
                "⚠️ Session data is incomplete. Please try again with /reminders."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            new_dt = datetime.fromisoformat(new_dt_str)
        except ValueError:
            await query.edit_message_text(
                "⚠️ Invalid datetime in session. Please try again with /reminders."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            async with _AsyncSessionFactory() as db:
                reminder = await rs.reschedule_reminder(
                    reminder_id=rem_id,
                    new_local_time=new_dt,
                    user_id=user_id,
                    db=db,
                    timezone_name=tz_name,
                )
                await db.commit()

            logger.bind(
                telegram_user_id=telegram_user_id,
                reminder_id=rem_id,
            ).info("reminder_rescheduled_via_bot")

            _clear_data(context)
            new_dt_display = data.get("new_datetime_display", new_dt_str)
            await query.edit_message_text(
                f"✅ Reminder updated to *{new_dt_display}*.",
                parse_mode="Markdown",
            )

        except rs.TimezoneNotSetError as e:
            await query.edit_message_text(f"❌ {e}")
            _clear_data(context)

        except rs.ReminderNotFoundError as e:
            # Req 10.6: inform user when reminder not found
            await query.edit_message_text(
                f"❌ {e}\n\nNo matching reminder was found and no changes were made."
            )
            _clear_data(context)

        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder_update_failed",
                telegram_user_id=telegram_user_id,
                reminder_id=rem_id,
            )
            await query.edit_message_text(
                "⚠️ Could not update reminder at this time. Please try again later."
            )
            _clear_data(context)

        return ConversationHandler.END

    await query.edit_message_text("Please tap Yes or No.")
    return UPDATE_CONFIRM


# ---------------------------------------------------------------------------
# DELETE flow
# ---------------------------------------------------------------------------


async def handle_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle Yes/No confirmation for reminder deletion."""
    assert update.callback_query is not None
    assert update.effective_user is not None

    query = update.callback_query
    await query.answer()

    callback_data = query.data or ""
    telegram_user_id = update.effective_user.id

    if callback_data == _CB_NO:
        _clear_data(context)
        await query.edit_message_text("Deletion cancelled. Reminder remains active.")
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
        rem_id = data.get("selected_rem_id")
        if rem_id is None:
            await query.edit_message_text(
                "⚠️ No reminder selected. Please try again with /reminders."
            )
            _clear_data(context)
            return ConversationHandler.END

        try:
            async with _AsyncSessionFactory() as db:
                await rs.cancel_reminder(rem_id, user_id, db)
                await db.commit()

            logger.bind(
                telegram_user_id=telegram_user_id,
                reminder_id=rem_id,
            ).info("reminder_deleted_via_bot")

            _clear_data(context)
            await query.edit_message_text("✅ Reminder deleted.")

        except rs.ReminderNotFoundError as e:
            # Req 10.6: inform user when reminder not found
            await query.edit_message_text(
                f"❌ {e}\n\nNo matching reminder was found and no changes were made."
            )
            _clear_data(context)

        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder_delete_failed",
                telegram_user_id=telegram_user_id,
                reminder_id=rem_id,
            )
            await query.edit_message_text(
                "⚠️ Could not delete reminder at this time. Please try again later."
            )
            _clear_data(context)

        return ConversationHandler.END

    await query.edit_message_text("Please tap Yes or No.")
    return DELETE_CONFIRM


# ---------------------------------------------------------------------------
# ConversationHandler registration
# ---------------------------------------------------------------------------


def register_reminder_handler(application: Application) -> None:  # type: ignore[type-arg]
    """
    Build and register the reminder ConversationHandler on *application*.

    All flows are encapsulated in a single :class:`~telegram.ext.ConversationHandler`
    with a per-user ``per_user=True`` scope so simultaneous sessions don't
    interfere.
    """
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("reminders", cmd_reminders)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(handle_main_menu),
            ],
            CREATE_TYPE: [
                CallbackQueryHandler(
                    handle_create_type, pattern=f"^{_CB_TYPE_PREFIX}"
                ),
            ],
            CREATE_DATETIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_datetime),
            ],
            CREATE_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_message),
            ],
            CREATE_CONFIRM: [
                CallbackQueryHandler(handle_create_confirm),
            ],
            PICK_REMINDER: [
                CallbackQueryHandler(
                    handle_pick_reminder, pattern=f"^{_CB_PICK_PREFIX}"
                ),
            ],
            UPDATE_DATETIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update_datetime),
            ],
            UPDATE_CONFIRM: [
                CallbackQueryHandler(
                    handle_update_confirm,
                    pattern=f"^({_CB_YES}|{_CB_NO})$",
                ),
            ],
            DELETE_CONFIRM: [
                CallbackQueryHandler(
                    handle_delete_confirm,
                    pattern=f"^({_CB_YES}|{_CB_NO})$",
                ),
            ],
        },
        fallbacks=[CommandHandler("reminders", cmd_reminders)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    application.add_handler(conv_handler)


# Alias for consistency with other handler modules
register = register_reminder_handler


__all__ = [
    "register_reminder_handler",
    "register",
]
