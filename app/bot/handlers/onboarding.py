"""
Multi-step onboarding conversation for Famosi.

State machine
-------------
States flow in order for Mom:
    ROLE → DUE_DATE_OR_LMP → COUNTRY → TIMEZONE → LANGUAGE →
    FIRST_PREGNANCY → FOOD_PREFERENCE → EXERCISE_HABIT → WAKE_TIME → SLEEP_TIME → [complete]

Partner flow adds an INVITE_CODE step before ROLE and ends with:
    ... → SLEEP_TIME → SUPPORT_PREFS → SHARED_TIMELINE → [complete]

Re-onboarding (CONFIRM_RESET)
------------------------------
When a user who has already completed onboarding sends /start, they are asked
whether they want to start over.  If yes, all their data is deleted (CASCADE)
and the conversation restarts from scratch.

Family linking (invite codes)
------------------------------
When a partner selects their role they are asked to enter a 6-character invite
code that the mom generated via /invite.  The bot looks up the FamilyUnit by
code, links both users to it, and marks the code as used.  If the partner has
no code (or wants to link later) they can skip.

Partial-state persistence (Req 1.6)
-------------------------------------
After each step the collected data + current state are stored in Redis
(key ``onboarding:{telegram_user_id}``, TTL 24 h) with an in-process fallback.

Privacy contract
-----------------
- NEVER log message text, food item names, or any health data.
- Log only structural fields: telegram_user_id (integer), state name.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from datetime import date, datetime, time, timezone
from typing import Any

import structlog
from sqlalchemy import select, delete
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

from app.bot.keyboards.onboarding import (
    country_keyboard,
    exercise_habit_keyboard,
    food_preference_keyboard,
    language_keyboard,
    role_keyboard,
    sleep_time_keyboard,
    support_prefs_keyboard,
    timezone_keyboard,
    wake_time_keyboard,
    yes_no_keyboard,
)
from app.dependencies import _AsyncSessionFactory
from app.models.family_unit import FamilyUnit
from app.models.user import FoodPreference, User, UserRole
from app.services.admin_service import clear_pending

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Conversation state constants
# ---------------------------------------------------------------------------

(
    CONFIRM_RESET,       # 0 — shown when user is already onboarded
    INVITE_CODE,         # 1 — partner only: enter/skip family invite code
    ROLE,                # 2
    DUE_DATE_OR_LMP,     # 3
    COUNTRY,             # 4
    TIMEZONE,            # 5
    LANGUAGE,            # 6
    FIRST_PREGNANCY,     # 7
    FOOD_PREFERENCE,     # 8
    EXERCISE_HABIT,      # 9
    WAKE_TIME,           # 10
    SLEEP_TIME,          # 11
    SUPPORT_PREFS,       # 12
    SHARED_TIMELINE,     # 13
) = range(14)

# ---------------------------------------------------------------------------
# In-process fallback store for partial onboarding state
# Key: telegram_user_id (int) → {"step": int, "data": dict}
# ---------------------------------------------------------------------------
_FALLBACK_STORE: dict[int, dict[str, Any]] = {}
_REDIS_KEY_TPL = "onboarding:{user_id}"
_REDIS_TTL = 86_400  # 24 h


# ---------------------------------------------------------------------------
# Redis partial-state helpers
# ---------------------------------------------------------------------------

async def _save_state(user_id: int, step: int, data: dict[str, Any]) -> None:
    payload = json.dumps({"step": step, "data": data})
    key = _REDIS_KEY_TPL.format(user_id=user_id)
    try:
        import redis.asyncio as aioredis
        from app.config import settings
        if settings.redis_url:
            client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
            try:
                await client.set(key, payload, ex=_REDIS_TTL)
                return
            finally:
                await client.aclose()
    except Exception:
        pass
    _FALLBACK_STORE[user_id] = {"step": step, "data": data}


async def _load_state(user_id: int) -> dict[str, Any] | None:
    key = _REDIS_KEY_TPL.format(user_id=user_id)
    try:
        import redis.asyncio as aioredis
        from app.config import settings
        if settings.redis_url:
            client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
            try:
                raw = await client.get(key)
                if raw:
                    return json.loads(raw)
            finally:
                await client.aclose()
    except Exception:
        pass
    return _FALLBACK_STORE.get(user_id)


async def _clear_state(user_id: int) -> None:
    key = _REDIS_KEY_TPL.format(user_id=user_id)
    try:
        import redis.asyncio as aioredis
        from app.config import settings
        if settings.redis_url:
            client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
            try:
                await client.delete(key)
            finally:
                await client.aclose()
    except Exception:
        pass
    _FALLBACK_STORE.pop(user_id, None)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INVITE_RE = re.compile(r"^[A-Z0-9]{6}$")


def _parse_date(text: str) -> date | None:
    text = text.strip()
    if not _DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _is_valid_due_date(d: date) -> bool:
    delta = (d - date.today()).days
    return 1 <= delta <= 280


def _is_valid_lmp(d: date) -> bool:
    delta = (date.today() - d).days
    return 1 <= delta <= 280


def _generate_invite_code() -> str:
    """Return a random 6-char uppercase alphanumeric code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


# ---------------------------------------------------------------------------
# Context data key
# ---------------------------------------------------------------------------

_DATA_KEY = "onboarding_data"


def _get_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    if _DATA_KEY not in context.user_data:  # type: ignore[operator]
        context.user_data[_DATA_KEY] = {}  # type: ignore[index]
    return context.user_data[_DATA_KEY]  # type: ignore[index]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _delete_user_data(telegram_user_id: int) -> None:
    """
    Hard-delete the User row (all health data cascades with it).

    Called when the user confirms they want to re-do onboarding from scratch.
    Subscription, health records, reminders, appointments — everything linked
    to the user via CASCADE FK is removed in a single DELETE.
    """
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            await session.delete(user)
            await session.commit()
    logger.info("user_data_deleted_for_reset", telegram_user_id=telegram_user_id)


async def _upsert_user(telegram_user_id: int, data: dict[str, Any]) -> User:
    """Create or update the User row. Returns the persisted ORM instance."""
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        user: User | None = result.scalar_one_or_none()

        role_value    = data.get("role", UserRole.mom.value)
        due_date_raw  = data.get("due_date")
        lmp_date_raw  = data.get("lmp_date")
        country       = data.get("country", "US")
        timezone_str  = data.get("timezone", "UTC")
        language      = data.get("language", "en")
        first_preg    = data.get("first_pregnancy", True)
        food_pref_raw = data.get("food_preference")
        exercise_h    = data.get("exercise_habit")
        wake_raw      = data.get("wake_time")
        sleep_raw     = data.get("sleep_time")

        due_date  : date | None          = date.fromisoformat(due_date_raw) if due_date_raw else None
        lmp_date  : date | None          = date.fromisoformat(lmp_date_raw) if lmp_date_raw else None
        wake_time : time | None          = time.fromisoformat(wake_raw)     if wake_raw     else None
        sleep_time: time | None          = time.fromisoformat(sleep_raw)    if sleep_raw    else None
        food_pref : FoodPreference | None = FoodPreference(food_pref_raw)   if food_pref_raw else None

        if user is None:
            user = User(
                telegram_user_id=telegram_user_id,
                role=UserRole(role_value),
                due_date=due_date, lmp_date=lmp_date,
                country=country, timezone=timezone_str, language=language,
                first_pregnancy=first_preg, food_preference=food_pref,
                exercise_habit=exercise_h, wake_time=wake_time, sleep_time=sleep_time,
                onboarding_complete=True,
            )
            session.add(user)
        else:
            user.role             = UserRole(role_value)
            user.due_date         = due_date
            user.lmp_date         = lmp_date
            user.country          = country
            user.timezone         = timezone_str
            user.language         = language
            user.first_pregnancy  = first_preg
            user.food_preference  = food_pref
            user.exercise_habit   = exercise_h
            user.wake_time        = wake_time
            user.sleep_time       = sleep_time
            user.onboarding_complete = True

        await session.commit()
        await session.refresh(user)
        return user


async def _link_partner_to_family(
    partner_telegram_id: int,
    invite_code: str,
) -> tuple[bool, str]:
    """
    Look up FamilyUnit by invite_code and link the partner.

    Returns (success: bool, message: str).
    Side-effects:
    - Sets partner.family_unit_id = family_unit.id
    - Sets mom.family_unit_id = family_unit.id  (if not already set)
    - Sets family_unit.invite_used = True
    """
    code = invite_code.strip().upper()
    async with _AsyncSessionFactory() as session:
        fu_result = await session.execute(
            select(FamilyUnit).where(
                FamilyUnit.invite_code == code,
                FamilyUnit.invite_used.is_(False),
            )
        )
        family_unit = fu_result.scalar_one_or_none()

        if family_unit is None:
            return False, (
                "❌ That code is invalid or has already been used.\n"
                "Ask your partner to generate a new one with /invite."
            )

        # Load partner user row
        partner_result = await session.execute(
            select(User).where(User.telegram_user_id == partner_telegram_id)
        )
        partner = partner_result.scalar_one_or_none()
        if partner is None:
            return False, "❌ Could not find your user record. Please try again."

        # Link partner
        partner.family_unit_id = family_unit.id

        # Also link mom if she isn't linked yet
        if family_unit.mom_user_id is not None:
            mom_result = await session.execute(
                select(User).where(User.id == family_unit.mom_user_id)
            )
            mom = mom_result.scalar_one_or_none()
            if mom is not None and mom.family_unit_id is None:
                mom.family_unit_id = family_unit.id

        # Mark code as used
        family_unit.invite_used = True

        await session.commit()

    logger.info(
        "partner_linked_to_family",
        partner_telegram_id=partner_telegram_id,
        family_unit_id=family_unit.id,
    )
    return True, "✅ You're now linked to your partner's account!"


# ---------------------------------------------------------------------------
# /start entry point
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handle /start.

    - Already onboarded → ask if they want to restart (CONFIRM_RESET).
    - Partial state saved → resume from last step.
    - Fresh user → begin from ROLE.
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_user_id = update.effective_user.id

    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        existing: User | None = result.scalar_one_or_none()

    if existing is not None and existing.onboarding_complete:
        logger.info("onboarding_restart_prompt", telegram_user_id=telegram_user_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, start over", callback_data="reset:yes"),
                InlineKeyboardButton("❌ No, keep my data", callback_data="reset:no"),
            ]
        ])
        await update.message.reply_text(
            "⚠️ *You're already set up.*\n\n"
            "Starting over will *permanently delete all your data* — health logs, "
            "appointments, reminders, and your subscription.\n\n"
            "Are you sure you want to reset and begin again?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return CONFIRM_RESET

    # Check for partial state
    saved = await _load_state(telegram_user_id)
    if saved:
        logger.info("onboarding_resuming", telegram_user_id=telegram_user_id)
        context.user_data[_DATA_KEY] = saved.get("data", {})  # type: ignore[index]
        return await _send_prompt_for_state(update, context, saved.get("step", ROLE))

    # Fresh start
    logger.info("onboarding_started", telegram_user_id=telegram_user_id)
    context.user_data[_DATA_KEY] = {}  # type: ignore[index]
    await update.message.reply_text(
        "👋 Welcome to Famosi! I'll guide you through a quick setup.\n\n"
        "First, what's your role?",
        reply_markup=role_keyboard(),
    )
    return ROLE


# ---------------------------------------------------------------------------
# CONFIRM_RESET handler
# ---------------------------------------------------------------------------

async def handle_confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handle yes/no response to the reset confirmation.

    - yes → delete all user data, clear state, restart from ROLE.
    - no  → end conversation, user keeps existing setup.
    """
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = (query.data or "reset:no").split(":", 1)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    if value != "yes":
        await query.edit_message_text(
            "No problem! Your data is safe. Use /help to see everything I can do."
        )
        logger.info("onboarding_reset_declined", telegram_user_id=telegram_user_id)
        return ConversationHandler.END

    # Delete all user data
    await _delete_user_data(telegram_user_id)
    await _clear_state(telegram_user_id)

    # Also clear any pending-approval state for this user
    clear_pending(telegram_user_id)

    context.user_data[_DATA_KEY] = {}  # type: ignore[index]

    logger.info("onboarding_reset_confirmed", telegram_user_id=telegram_user_id)
    await query.edit_message_text(
        "🗑️ All your data has been deleted.\n\n"
        "Let's start fresh! What's your role?",
        reply_markup=role_keyboard(),
    )
    return ROLE


# ---------------------------------------------------------------------------
# Prompt dispatcher (used when resuming from partial state)
# ---------------------------------------------------------------------------

async def _send_prompt_for_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: int,
) -> int:
    data = _get_data(context)
    role = data.get("role", UserRole.mom.value)
    send = (
        update.message.reply_text
        if update.message
        else update.callback_query.message.reply_text  # type: ignore[union-attr]
    )

    if state == ROLE:
        await send("What's your role?", reply_markup=role_keyboard())
    elif state == INVITE_CODE:
        await send(
            "🔗 Do you have a partner invite code?\n\n"
            "If your partner already has Famosi, ask them for their 6-character "
            "invite code and enter it below. Otherwise tap *Skip*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Skip →", callback_data="invite:skip")]]
            ),
        )
    elif state == DUE_DATE_OR_LMP:
        if role == UserRole.mom.value:
            await send("📅 Please enter your due date (YYYY-MM-DD).\nIt must be 1–280 days from today.")
        else:
            await send("📅 Please enter the LMP date (YYYY-MM-DD).\nIt must be 1–280 days in the past.")
    elif state == COUNTRY:
        await send("🌍 Where are you located?", reply_markup=country_keyboard())
    elif state == TIMEZONE:
        await send("🕐 Choose your timezone:", reply_markup=timezone_keyboard(data.get("country")))
    elif state == LANGUAGE:
        await send("🌐 Choose your preferred language:", reply_markup=language_keyboard())
    elif state == FIRST_PREGNANCY:
        await send("Is this your first pregnancy?", reply_markup=yes_no_keyboard("first_preg"))
    elif state == FOOD_PREFERENCE:
        await send("🍽️ What's your dietary preference?", reply_markup=food_preference_keyboard())
    elif state == EXERCISE_HABIT:
        await send("🏃 How would you describe your current exercise habits?", reply_markup=exercise_habit_keyboard())
    elif state == WAKE_TIME:
        await send("⏰ What time do you usually wake up?", reply_markup=wake_time_keyboard())
    elif state == SLEEP_TIME:
        await send("🌙 What time do you usually go to sleep?", reply_markup=sleep_time_keyboard())
    elif state == SUPPORT_PREFS:
        await send("💙 What kind of support would you like to provide your partner?", reply_markup=support_prefs_keyboard())
    elif state == SHARED_TIMELINE:
        await send("📋 Would you like to share a pregnancy timeline with your partner?", reply_markup=yes_no_keyboard("shared_timeline"))

    return state


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

async def handle_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    data["role"] = value
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    logger.info("onboarding_role_selected", telegram_user_id=telegram_user_id)

    if value == UserRole.partner.value:
        # Partners go through invite-code step first
        await _save_state(telegram_user_id, INVITE_CODE, data)
        await query.edit_message_text(
            "🔗 *Family linking*\n\n"
            "If your partner is already on Famosi, ask them to send you their "
            "invite code (they can get it with /invite).\n\n"
            "Enter the 6-character code below, or tap *Skip* to link later.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Skip →", callback_data="invite:skip")]]
            ),
        )
        return INVITE_CODE
    else:
        await _save_state(telegram_user_id, DUE_DATE_OR_LMP, data)
        await query.edit_message_text(
            "📅 Please enter your due date (YYYY-MM-DD).\n"
            "It must be 1–280 days from today."
        )
        return DUE_DATE_OR_LMP


async def handle_invite_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handle invite code entry (text) or Skip button press (callback).

    Text path: validate the code, attempt to link now (user row may not exist
    yet — we store the validated code in data["invite_code"] and apply linking
    after _upsert_user in _complete_onboarding).

    Skip path: store data["invite_code"] = None and continue.
    """
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    # --- Skip button ---
    if update.callback_query is not None:
        await update.callback_query.answer()
        data["invite_code"] = None
        await _save_state(telegram_user_id, DUE_DATE_OR_LMP, data)
        await update.callback_query.edit_message_text(
            "No problem — you can link later.\n\n"
            "📅 Please enter the LMP date (YYYY-MM-DD).\n"
            "It must be 1–280 days in the past."
        )
        return DUE_DATE_OR_LMP

    # --- Text entry ---
    assert update.message is not None
    raw = (update.message.text or "").strip().upper()

    if not _INVITE_RE.match(raw):
        await update.message.reply_text(
            "❌ Codes are 6 characters (letters and numbers only), e.g. *AB12CD*.\n"
            "Please try again, or tap *Skip* to continue without linking.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Skip →", callback_data="invite:skip")]]
            ),
        )
        return INVITE_CODE

    # Quick DB check — validate the code exists and is unused
    async with _AsyncSessionFactory() as session:
        fu_result = await session.execute(
            select(FamilyUnit).where(
                FamilyUnit.invite_code == raw,
                FamilyUnit.invite_used.is_(False),
            )
        )
        family_unit = fu_result.scalar_one_or_none()

    if family_unit is None:
        await update.message.reply_text(
            "❌ That code is invalid or has already been used.\n"
            "Ask your partner to generate a fresh one with /invite, "
            "or tap *Skip* to continue without linking.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Skip →", callback_data="invite:skip")]]
            ),
        )
        return INVITE_CODE

    # Valid — store and continue
    data["invite_code"] = raw
    await _save_state(telegram_user_id, DUE_DATE_OR_LMP, data)
    await update.message.reply_text(
        f"✅ Code accepted! You'll be linked to your partner once setup is complete.\n\n"
        "📅 Please enter the LMP date (YYYY-MM-DD).\n"
        "It must be 1–280 days in the past."
    )
    return DUE_DATE_OR_LMP


async def handle_due_date_or_lmp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    assert update.effective_user is not None

    text = (update.message.text or "").strip()
    data = _get_data(context)
    role = data.get("role", UserRole.mom.value)
    telegram_user_id = update.effective_user.id

    parsed = _parse_date(text)
    if parsed is None:
        await update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD (e.g. 2025-10-15).")
        return DUE_DATE_OR_LMP

    if role == UserRole.mom.value:
        if not _is_valid_due_date(parsed):
            await update.message.reply_text(
                "❌ Due date must be between 1 and 280 days in the future. Please try again."
            )
            return DUE_DATE_OR_LMP
        data["due_date"] = parsed.isoformat()
    else:
        if not _is_valid_lmp(parsed):
            await update.message.reply_text(
                "❌ LMP date must be between 1 and 280 days in the past. Please try again."
            )
            return DUE_DATE_OR_LMP
        data["lmp_date"] = parsed.isoformat()

    await _save_state(telegram_user_id, COUNTRY, data)
    await update.message.reply_text("🌍 Where are you located?", reply_markup=country_keyboard())
    return COUNTRY


async def handle_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["country"] = value
    await _save_state(telegram_user_id, TIMEZONE, data)
    await query.edit_message_text("🕐 Choose your timezone:", reply_markup=timezone_keyboard(value))
    return TIMEZONE


async def handle_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, tz_value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    try:
        import pytz
        if tz_value not in pytz.all_timezones_set:
            await query.edit_message_text(
                "❌ Unrecognised timezone. Please choose from the list.",
                reply_markup=timezone_keyboard(data.get("country")),
            )
            return TIMEZONE
    except ImportError:
        pass

    data["timezone"] = tz_value
    await _save_state(telegram_user_id, LANGUAGE, data)
    await query.edit_message_text("🌐 Choose your preferred language:", reply_markup=language_keyboard())
    return LANGUAGE


async def handle_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["language"] = value
    await _save_state(telegram_user_id, FIRST_PREGNANCY, data)
    await query.edit_message_text("Is this your first pregnancy?", reply_markup=yes_no_keyboard("first_preg"))
    return FIRST_PREGNANCY


async def handle_first_pregnancy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["first_pregnancy"] = value == "yes"
    await _save_state(telegram_user_id, FOOD_PREFERENCE, data)
    await query.edit_message_text("🍽️ What's your dietary preference?", reply_markup=food_preference_keyboard())
    return FOOD_PREFERENCE


async def handle_food_preference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]
    role = data.get("role", UserRole.mom.value)

    data["food_preference"] = value

    if role == UserRole.partner.value:
        await _save_state(telegram_user_id, WAKE_TIME, data)
        await query.edit_message_text("⏰ What time do you usually wake up?", reply_markup=wake_time_keyboard())
        return WAKE_TIME
    else:
        await _save_state(telegram_user_id, EXERCISE_HABIT, data)
        await query.edit_message_text(
            "🏃 How would you describe your current exercise habits?",
            reply_markup=exercise_habit_keyboard(),
        )
        return EXERCISE_HABIT


async def handle_exercise_habit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["exercise_habit"] = value
    await _save_state(telegram_user_id, WAKE_TIME, data)

    if value == "none":
        await query.edit_message_text(
            "💡 No worries! Light movement like a 10-minute walk can do wonders "
            "during pregnancy.\n\n⏰ What time do you usually wake up?",
            reply_markup=wake_time_keyboard(),
        )
    else:
        await query.edit_message_text("⏰ What time do you usually wake up?", reply_markup=wake_time_keyboard())
    return WAKE_TIME


async def handle_wake_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["wake_time"] = value
    await _save_state(telegram_user_id, SLEEP_TIME, data)
    await query.edit_message_text("🌙 What time do you usually go to sleep?", reply_markup=sleep_time_keyboard())
    return SLEEP_TIME


async def handle_sleep_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]
    role = data.get("role", UserRole.mom.value)

    data["sleep_time"] = value

    if role == UserRole.partner.value:
        await _save_state(telegram_user_id, SUPPORT_PREFS, data)
        await query.edit_message_text(
            "💙 What kind of support would you like to provide your partner?",
            reply_markup=support_prefs_keyboard(),
        )
        return SUPPORT_PREFS
    else:
        return await _complete_onboarding(query, None, telegram_user_id, data)


async def handle_support_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["support_prefs"] = value
    await _save_state(telegram_user_id, SHARED_TIMELINE, data)
    await query.edit_message_text(
        "📋 Would you like to share a pregnancy timeline with your partner?",
        reply_markup=yes_no_keyboard("shared_timeline"),
    )
    return SHARED_TIMELINE


async def handle_shared_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["shared_timeline"] = value == "yes"
    return await _complete_onboarding(query, None, telegram_user_id, data)


# ---------------------------------------------------------------------------
# Onboarding completion
# ---------------------------------------------------------------------------

async def _complete_onboarding(
    update_or_query: Any,
    context: ContextTypes.DEFAULT_TYPE | None,
    telegram_user_id: int,
    data: dict[str, Any],
) -> int:
    """
    Persist the User record, apply family linking, notify admin, send welcome.
    """
    logger.info("onboarding_completing", telegram_user_id=telegram_user_id)

    user = await _upsert_user(telegram_user_id, data)
    await _clear_state(telegram_user_id)

    # --- Family linking for partners ----------------------------------------
    invite_code = data.get("invite_code")
    link_note = ""
    if user.role == UserRole.partner and invite_code:
        success, link_msg = await _link_partner_to_family(telegram_user_id, invite_code)
        link_note = f"\n\n{link_msg}"

    # --- Admin approval gate -------------------------------------------------
    from app.services.admin_service import is_admin, mark_pending
    from app.config import settings as _settings
    import asyncio as _asyncio

    if not is_admin(telegram_user_id) and _settings.admin_telegram_user_id != 0:
        mark_pending(
            telegram_user_id,
            user_id=user.id,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            country=user.country,
        )

        async def _notify_admin() -> None:
            try:
                from telegram import Bot
                bot = Bot(token=_settings.telegram_bot_token)
                role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
                await bot.send_message(
                    chat_id=_settings.admin_telegram_user_id,
                    text=(
                        f"🆕 *New Famosi signup awaiting approval*\n\n"
                        f"Telegram ID: `{telegram_user_id}`\n"
                        f"Role: `{role_val}`\n"
                        f"Country: `{user.country}`\n\n"
                        f"To approve (starts 7-day trial):\n"
                        f"`/approve {telegram_user_id}`\n\n"
                        f"To reject and remove:\n"
                        f"`/reject {telegram_user_id}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as _exc:
                logger.warning("admin_new_signup_notify_failed", error=str(_exc))

        _asyncio.create_task(_notify_admin())

        waiting_msg = (
            "✅ *Setup complete!*"
            + link_note
            + "\n\n"
            "Your account is pending a quick review. You'll receive a message "
            "as soon as you're approved — usually within a few hours.\n\n"
            "Thanks for joining Famosi! 🤱"
        )
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(waiting_msg, parse_mode="Markdown")
        elif hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(waiting_msg, parse_mode="Markdown")
        else:
            await update_or_query.reply_text(waiting_msg, parse_mode="Markdown")

        logger.info("onboarding_complete_pending_approval", telegram_user_id=telegram_user_id)
        return ConversationHandler.END
    # -------------------------------------------------------------------------

    # Build gestational context line
    from app.components.pregnancy_engine import calculate_gestational_age
    today = date.today()
    if user.due_date:
        weeks, days = calculate_gestational_age(user.due_date, today)
        progress = f"📍 You're currently *{weeks}w {days}d* along"
    elif user.lmp_date:
        from app.components.pregnancy_engine import lmp_to_due_date
        estimated_due = lmp_to_due_date(user.lmp_date)
        weeks, days = calculate_gestational_age(estimated_due, today)
        progress = f"📍 You're currently *~{weeks}w {days}d* along (estimated)"
    else:
        progress = ""

    role_val = user.role.value if hasattr(user.role, "value") else user.role
    role_display = "Mom 👶" if role_val == "mom" else "Partner 🤝"
    low_exercise = data.get("exercise_habit") in ("none", None)
    exercise_tip = (
        "\n\n💡 Since you mentioned you don't exercise much, try:\n"
        "*\"remind me to take a 10-minute walk every day at 5pm\"*"
        if low_exercise else ""
    )

    welcome = (
        f"✅ All set, {role_display}!"
        + link_note
        + f"\n{progress}\n\n"
        "Here's what I can do for you:\n\n"
        "🍽️ *Log & track* — meals, symptoms, weight, water, medications\n"
        "❓ *Ask anything* — \"Is it safe to eat sushi?\" or \"What's normal at week 20?\"\n"
        "📋 *Doctor prep* — generate a visit summary before your appointment\n"
        "🔔 *Reminders* — medications, appointments, daily check-ins\n"
        "📅 *Appointments* — schedule, reschedule, or cancel\n"
        "🥗 *Nutrition* — daily summary, deficiency alerts, meal ideas\n\n"
        "Just chat naturally — try something like:\n"
        "• _\"I had oatmeal with banana for breakfast\"_\n"
        "• _\"Mild nausea this morning, severity 3\"_\n"
        "• _\"What foods are rich in iron?\"_\n"
        "• _\"Remind me to take my prenatal vitamin at 9am\"_"
        f"{exercise_tip}\n\n"
        "Type /help anytime to see all commands."
    )

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(welcome, parse_mode="Markdown")
    elif hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(welcome, parse_mode="Markdown")
    else:
        await update_or_query.reply_text(welcome, parse_mode="Markdown")

    logger.info(
        "onboarding_complete",
        telegram_user_id=telegram_user_id,
        role=role_val,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /invite command — mom generates a family invite code
# ---------------------------------------------------------------------------

async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Generate (or show an existing) family invite code for this mom.

    Creates a FamilyUnit row with a fresh invite_code if one doesn't exist.
    The partner enters this code during their onboarding to link accounts.
    """
    assert update.message is not None
    assert update.effective_user is not None

    telegram_user_id = update.effective_user.id

    async with _AsyncSessionFactory() as session:
        # Load the user
        u_result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        user: User | None = u_result.scalar_one_or_none()

        if user is None or not user.onboarding_complete:
            await update.message.reply_text(
                "Please complete setup first with /start before generating an invite code."
            )
            return

        # Only mom role can generate invite codes
        if user.role != UserRole.mom:
            await update.message.reply_text(
                "Invite codes are generated by the mom account. "
                "Ask your partner to use /invite on their account."
            )
            return

        # Check if user already has a family unit with an unused code
        if user.family_unit_id is not None:
            fu_result = await session.execute(
                select(FamilyUnit).where(FamilyUnit.id == user.family_unit_id)
            )
            existing_fu = fu_result.scalar_one_or_none()
            if existing_fu is not None:
                if existing_fu.invite_used:
                    await update.message.reply_text(
                        "✅ Your partner is already linked to your account!\n\n"
                        "You're connected as a family unit."
                    )
                else:
                    await update.message.reply_text(
                        f"🔗 Your invite code is:\n\n"
                        f"*`{existing_fu.invite_code}`*\n\n"
                        f"Share this with your partner — they enter it during "
                        f"their Famosi setup to link your accounts.",
                        parse_mode="Markdown",
                    )
                return

        # Generate a unique code (retry on collision — extremely rare)
        code = None
        for _ in range(5):
            candidate = _generate_invite_code()
            exists = await session.execute(
                select(FamilyUnit).where(FamilyUnit.invite_code == candidate)
            )
            if exists.scalar_one_or_none() is None:
                code = candidate
                break

        if code is None:
            await update.message.reply_text("❌ Could not generate a code right now. Please try again.")
            return

        # Create the FamilyUnit and link mom
        family_unit = FamilyUnit(invite_code=code, mom_user_id=user.id)
        session.add(family_unit)
        await session.flush()  # get family_unit.id

        user.family_unit_id = family_unit.id
        await session.commit()

        logger.info(
            "invite_code_generated",
            telegram_user_id=telegram_user_id,
            family_unit_id=family_unit.id,
        )

    await update.message.reply_text(
        f"🔗 Your family invite code is:\n\n"
        f"*`{code}`*\n\n"
        f"Send this to your partner. When they set up Famosi, they enter this "
        f"code to link your accounts and see your shared pregnancy journey.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# ConversationHandler builder and registration
# ---------------------------------------------------------------------------

def build_onboarding_handler() -> ConversationHandler:
    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        assert update.message is not None
        assert update.effective_user is not None
        telegram_user_id = update.effective_user.id
        await _clear_state(telegram_user_id)
        context.user_data.pop(_DATA_KEY, None)  # type: ignore[union-attr]
        await update.message.reply_text(
            "Onboarding cancelled. Send /start whenever you're ready to begin."
        )
        logger.info("onboarding_cancelled", telegram_user_id=telegram_user_id)
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CONFIRM_RESET: [
                CallbackQueryHandler(handle_confirm_reset, pattern=r"^reset:(yes|no)$"),
            ],
            ROLE: [
                CallbackQueryHandler(handle_role, pattern=r"^role:(mom|partner)$"),
            ],
            INVITE_CODE: [
                # Text entry of the code
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invite_code),
                # Skip button
                CallbackQueryHandler(handle_invite_code, pattern=r"^invite:skip$"),
            ],
            DUE_DATE_OR_LMP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_due_date_or_lmp),
            ],
            COUNTRY: [
                CallbackQueryHandler(handle_country, pattern=r"^country:"),
            ],
            TIMEZONE: [
                CallbackQueryHandler(handle_timezone, pattern=r"^tz:"),
            ],
            LANGUAGE: [
                CallbackQueryHandler(handle_language, pattern=r"^lang:"),
            ],
            FIRST_PREGNANCY: [
                CallbackQueryHandler(handle_first_pregnancy, pattern=r"^first_preg:(yes|no)$"),
            ],
            FOOD_PREFERENCE: [
                CallbackQueryHandler(handle_food_preference, pattern=r"^food:"),
            ],
            EXERCISE_HABIT: [
                CallbackQueryHandler(handle_exercise_habit, pattern=r"^exercise:"),
            ],
            WAKE_TIME: [
                CallbackQueryHandler(handle_wake_time, pattern=r"^wake:"),
            ],
            SLEEP_TIME: [
                CallbackQueryHandler(handle_sleep_time, pattern=r"^sleep:"),
            ],
            SUPPORT_PREFS: [
                CallbackQueryHandler(handle_support_prefs, pattern=r"^support:"),
            ],
            SHARED_TIMELINE: [
                CallbackQueryHandler(handle_shared_timeline, pattern=r"^shared_timeline:(yes|no)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        name="onboarding",
        persistent=False,
    )


onboarding_handler: ConversationHandler = build_onboarding_handler()


_HELP_TEXT = """\
🤱 *Famosi — What I can do*

*📝 Logging* — just tell me naturally:
• _"I had oatmeal with banana for breakfast"_
• _"Mild nausea, severity 4, about 3 times today"_
• _"I weigh 68kg today"_
• _"Drank 2 glasses of water"_
• _"Taking iron 65mg tablet"_

*❓ Ask me anything*:
• _"Is it safe to eat sushi during pregnancy?"_
• _"What should I expect at week 28?"_
• _"What foods are rich in folate?"_

*📅 Appointments*:
• _"Add a doctor appointment on July 15 at 10am"_
• _"Show my upcoming appointments"_

*🔔 Reminders*:
• _"Remind me to take my vitamin at 9am daily"_
• _"Set a reminder for my scan next Monday at 3pm"_

*📊 Reports & summaries*:
• _"Show my nutrition summary for today"_
• _"Generate a doctor visit summary"_
• _"How have my symptoms been this week?"_

*👨‍👩‍👧 Family*:
• /invite — generate a code to link your partner

*Commands*:
/start  — restart setup (or reset)
/invite — link your partner
/help   — show this message
/cancel — cancel any active conversation
"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


def register(application: Application) -> None:
    application.add_handler(onboarding_handler)
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("invite", cmd_invite))
    logger.debug("onboarding_handler_registered")


__all__ = [
    "CONFIRM_RESET", "INVITE_CODE",
    "ROLE", "DUE_DATE_OR_LMP", "COUNTRY", "TIMEZONE", "LANGUAGE",
    "FIRST_PREGNANCY", "FOOD_PREFERENCE", "EXERCISE_HABIT",
    "WAKE_TIME", "SLEEP_TIME", "SUPPORT_PREFS", "SHARED_TIMELINE",
    "onboarding_handler",
    "register",
]
