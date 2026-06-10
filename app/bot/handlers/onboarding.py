"""
Multi-step onboarding conversation for Famosi.

State machine
-------------
The :class:`telegram.ext.ConversationHandler` steps through the following
states in order.  Partner users skip ``FOOD_PREFERENCE``, ``EXERCISE_HABIT``,
``WAKE_TIME``, and ``SLEEP_TIME`` (health-tracking fields) but still collect
``SUPPORT_PREFS`` and ``SHARED_TIMELINE``.

States (module-level integer constants)::

    ROLE, DUE_DATE_OR_LMP, COUNTRY, TIMEZONE, LANGUAGE, FIRST_PREGNANCY,
    FOOD_PREFERENCE, EXERCISE_HABIT, WAKE_TIME, SLEEP_TIME,
    SUPPORT_PREFS, SHARED_TIMELINE = range(12)

Partial-state persistence (Req 1.6)
------------------------------------
After each step the collected data + current state are stored in Redis
(key ``onboarding:{telegram_user_id}``, TTL 24 h).  If Redis is unavailable
the data is stored in-process (module-level dict) as a graceful fallback.
On ``/start`` any saved partial state is restored and the conversation
resumes from the last completed step.

Gate (Req 1.7)
--------------
If ``user.onboarding_complete is True`` the handler replies with an
"already set up" message and returns ``ConversationHandler.END``.

Privacy contract
----------------
- NEVER log message text, food item names, or any health data.
- Log only structural fields: ``telegram_user_id`` (numeric), state name,
  step names.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from typing import Any

import structlog
from sqlalchemy import select
from telegram import Update
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
from app.models.user import FoodPreference, User, UserRole

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Conversation state constants
# ---------------------------------------------------------------------------

(
    ROLE,
    DUE_DATE_OR_LMP,
    COUNTRY,
    TIMEZONE,
    LANGUAGE,
    FIRST_PREGNANCY,
    FOOD_PREFERENCE,
    EXERCISE_HABIT,
    WAKE_TIME,
    SLEEP_TIME,
    SUPPORT_PREFS,
    SHARED_TIMELINE,
) = range(12)

_STATE_NAMES: dict[int, str] = {
    ROLE: "ROLE",
    DUE_DATE_OR_LMP: "DUE_DATE_OR_LMP",
    COUNTRY: "COUNTRY",
    TIMEZONE: "TIMEZONE",
    LANGUAGE: "LANGUAGE",
    FIRST_PREGNANCY: "FIRST_PREGNANCY",
    FOOD_PREFERENCE: "FOOD_PREFERENCE",
    EXERCISE_HABIT: "EXERCISE_HABIT",
    WAKE_TIME: "WAKE_TIME",
    SLEEP_TIME: "SLEEP_TIME",
    SUPPORT_PREFS: "SUPPORT_PREFS",
    SHARED_TIMELINE: "SHARED_TIMELINE",
}

# ---------------------------------------------------------------------------
# In-process fallback store for partial onboarding state
# Key: telegram_user_id (int) → {"step": int, "data": dict}
# ---------------------------------------------------------------------------
_FALLBACK_STORE: dict[int, dict[str, Any]] = {}

# Redis key template and TTL
_REDIS_KEY_TPL = "onboarding:{user_id}"
_REDIS_TTL = 86_400  # 24 hours in seconds


# ---------------------------------------------------------------------------
# Partial state helpers
# ---------------------------------------------------------------------------

async def _save_state(user_id: int, step: int, data: dict[str, Any]) -> None:
    """Persist partial onboarding state (Redis preferred, in-process fallback)."""
    payload = json.dumps({"step": step, "data": data})
    key = _REDIS_KEY_TPL.format(user_id=user_id)

    try:
        import redis.asyncio as aioredis  # type: ignore[import]
        from app.config import settings

        if settings.redis_url:
            client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            try:
                await client.set(key, payload, ex=_REDIS_TTL)
                return
            finally:
                await client.aclose()
    except Exception:
        # Redis unavailable — fall through to in-process store
        pass

    _FALLBACK_STORE[user_id] = {"step": step, "data": data}


async def _load_state(user_id: int) -> dict[str, Any] | None:
    """Load partial state saved by :func:`_save_state`.  Returns ``None`` if absent."""
    key = _REDIS_KEY_TPL.format(user_id=user_id)

    try:
        import redis.asyncio as aioredis  # type: ignore[import]
        from app.config import settings

        if settings.redis_url:
            client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
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
    """Remove partial onboarding state after completion or cancellation."""
    key = _REDIS_KEY_TPL.format(user_id=user_id)

    try:
        import redis.asyncio as aioredis  # type: ignore[import]
        from app.config import settings

        if settings.redis_url:
            client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
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


def _parse_date(text: str) -> date | None:
    """Parse ISO-8601 date string; return ``None`` on failure."""
    text = text.strip()
    if not _DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_time(text: str) -> time | None:
    """Parse HH:MM time string; return ``None`` on failure."""
    text = text.strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
        return parsed.time()
    except ValueError:
        return None


def _is_valid_due_date(d: date) -> bool:
    """Due date must be 1–280 days in the future."""
    today = date.today()
    delta = (d - today).days
    return 1 <= delta <= 280


def _is_valid_lmp(d: date) -> bool:
    """Last menstrual period must be 1–280 days in the past."""
    today = date.today()
    delta = (today - d).days
    return 1 <= delta <= 280


# ---------------------------------------------------------------------------
# Step helpers: next state for each role
# ---------------------------------------------------------------------------

def _next_state_after(current: int, role: str) -> int:
    """Return the next conversation state for the given role."""
    is_partner = role == UserRole.partner.value

    # Full flow for mom
    mom_flow = [
        ROLE, DUE_DATE_OR_LMP, COUNTRY, TIMEZONE, LANGUAGE,
        FIRST_PREGNANCY, FOOD_PREFERENCE, EXERCISE_HABIT, WAKE_TIME, SLEEP_TIME,
    ]
    # Partner flow: skip health-specific steps (FOOD_PREFERENCE, EXERCISE_HABIT,
    # WAKE_TIME, SLEEP_TIME for health tracking) but add SUPPORT_PREFS, SHARED_TIMELINE
    partner_flow = [
        ROLE, DUE_DATE_OR_LMP, COUNTRY, TIMEZONE, LANGUAGE,
        FIRST_PREGNANCY, FOOD_PREFERENCE, WAKE_TIME, SLEEP_TIME,
        SUPPORT_PREFS, SHARED_TIMELINE,
    ]
    # Note: per Req 1.4, partners still collect food pref, wake/sleep for reminders;
    # they skip only symptom/exercise health tracking and add support_prefs + shared_timeline.

    flow = partner_flow if is_partner else mom_flow
    try:
        idx = flow.index(current)
        return flow[idx + 1]
    except (ValueError, IndexError):
        return ConversationHandler.END  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DB helper: upsert User
# ---------------------------------------------------------------------------

async def _upsert_user(telegram_user_id: int, data: dict[str, Any]) -> User:
    """
    Create or update the ``User`` row for *telegram_user_id*.

    Uses a simple select-then-insert/update pattern rather than a raw
    ``INSERT … ON CONFLICT`` to stay compatible with the async ORM session.
    """
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        user: User | None = result.scalar_one_or_none()

        role_value = data.get("role", UserRole.mom.value)
        due_date_raw = data.get("due_date")
        lmp_date_raw = data.get("lmp_date")
        country = data.get("country", "US")
        timezone_str = data.get("timezone", "UTC")
        language = data.get("language", "en")
        first_pregnancy = data.get("first_pregnancy", True)
        food_pref_raw = data.get("food_preference")
        exercise_habit = data.get("exercise_habit")
        wake_raw = data.get("wake_time")
        sleep_raw = data.get("sleep_time")

        # Parse stored ISO strings back to Python objects
        due_date: date | None = date.fromisoformat(due_date_raw) if due_date_raw else None
        lmp_date: date | None = date.fromisoformat(lmp_date_raw) if lmp_date_raw else None
        wake_time: time | None = time.fromisoformat(wake_raw) if wake_raw else None
        sleep_time: time | None = time.fromisoformat(sleep_raw) if sleep_raw else None
        food_pref: FoodPreference | None = FoodPreference(food_pref_raw) if food_pref_raw else None

        if user is None:
            user = User(
                telegram_user_id=telegram_user_id,
                role=UserRole(role_value),
                due_date=due_date,
                lmp_date=lmp_date,
                country=country,
                timezone=timezone_str,
                language=language,
                first_pregnancy=first_pregnancy,
                food_preference=food_pref,
                exercise_habit=exercise_habit,
                wake_time=wake_time,
                sleep_time=sleep_time,
                onboarding_complete=True,
            )
            session.add(user)
        else:
            user.role = UserRole(role_value)
            user.due_date = due_date
            user.lmp_date = lmp_date
            user.country = country
            user.timezone = timezone_str
            user.language = language
            user.first_pregnancy = first_pregnancy
            user.food_preference = food_pref
            user.exercise_habit = exercise_habit
            user.wake_time = wake_time
            user.sleep_time = sleep_time
            user.onboarding_complete = True

        await session.commit()
        await session.refresh(user)
        return user


# ---------------------------------------------------------------------------
# Context data key for collected onboarding fields
# ---------------------------------------------------------------------------
_DATA_KEY = "onboarding_data"


def _get_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Return (or initialise) the onboarding data dict from context."""
    if _DATA_KEY not in context.user_data:  # type: ignore[operator]
        context.user_data[_DATA_KEY] = {}  # type: ignore[index]
    return context.user_data[_DATA_KEY]  # type: ignore[index]


# ---------------------------------------------------------------------------
# /start entry point
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handle ``/start``.

    1. If the user already completed onboarding → send gate message and end.
    2. If a partial state exists (Redis or fallback) → restore and resume.
    3. Otherwise → begin from the ROLE step.
    """
    assert update.effective_user is not None
    assert update.message is not None

    telegram_user_id = update.effective_user.id

    # Gate (Req 1.7): check existing DB record
    async with _AsyncSessionFactory() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        existing: User | None = result.scalar_one_or_none()

    if existing is not None and existing.onboarding_complete:
        logger.info(
            "onboarding_gate_already_complete",
            telegram_user_id=telegram_user_id,
        )
        await update.message.reply_text(
            "You're already set up! Use /help to see what I can do."
        )
        return ConversationHandler.END

    # Check for partial state (Req 1.6)
    saved = await _load_state(telegram_user_id)
    if saved:
        logger.info(
            "onboarding_resuming",
            telegram_user_id=telegram_user_id,
            step=saved.get("step"),
        )
        context.user_data[_DATA_KEY] = saved.get("data", {})  # type: ignore[index]
        resume_step: int = saved.get("step", ROLE)
        return await _send_prompt_for_state(update, context, resume_step)

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
# Prompt dispatcher (used when resuming)
# ---------------------------------------------------------------------------

async def _send_prompt_for_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: int,
) -> int:
    """Send the appropriate prompt for *state* and return that state."""
    data = _get_data(context)
    role = data.get("role", UserRole.mom.value)

    assert update.message is not None or update.callback_query is not None
    send = update.message.reply_text if update.message else update.callback_query.message.reply_text  # type: ignore[union-attr]

    if state == ROLE:
        await send("What's your role?", reply_markup=role_keyboard())
    elif state == DUE_DATE_OR_LMP:
        if role == UserRole.mom.value:
            await send(
                "📅 Please enter your due date (YYYY-MM-DD).\n"
                "It must be 1–280 days from today."
            )
        else:
            await send(
                "📅 Please enter the last menstrual period (LMP) date (YYYY-MM-DD).\n"
                "It must be 1–280 days in the past."
            )
    elif state == COUNTRY:
        await send("🌍 Where are you located?", reply_markup=country_keyboard())
    elif state == TIMEZONE:
        country = data.get("country")
        await send("🕐 Choose your timezone:", reply_markup=timezone_keyboard(country))
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
        await send(
            "💙 What kind of support would you like to provide your partner?",
            reply_markup=support_prefs_keyboard(),
        )
    elif state == SHARED_TIMELINE:
        await send(
            "📋 Would you like to share a pregnancy timeline with your partner?",
            reply_markup=yes_no_keyboard("shared_timeline"),
        )

    return state


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

async def handle_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback query handler for role selection."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    data["role"] = value

    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]
    await _save_state(telegram_user_id, DUE_DATE_OR_LMP, data)

    logger.info("onboarding_role_selected", telegram_user_id=telegram_user_id)

    if value == UserRole.mom.value:
        prompt = (
            "📅 Please enter your due date (YYYY-MM-DD).\n"
            "It must be 1–280 days from today."
        )
    else:
        prompt = (
            "📅 Please enter the last menstrual period (LMP) date (YYYY-MM-DD).\n"
            "It must be 1–280 days in the past."
        )

    await query.edit_message_text(prompt)
    return DUE_DATE_OR_LMP


async def handle_due_date_or_lmp(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Text message handler for due date (Mom) or LMP (Partner)."""
    assert update.message is not None
    assert update.effective_user is not None

    text = (update.message.text or "").strip()
    data = _get_data(context)
    role = data.get("role", UserRole.mom.value)
    telegram_user_id = update.effective_user.id

    parsed = _parse_date(text)
    if parsed is None:
        await update.message.reply_text(
            "❌ Invalid date format. Please use YYYY-MM-DD (e.g. 2025-10-15)."
        )
        return DUE_DATE_OR_LMP

    if role == UserRole.mom.value:
        if not _is_valid_due_date(parsed):
            await update.message.reply_text(
                "❌ Due date must be between 1 and 280 days in the future. "
                "Please try again."
            )
            return DUE_DATE_OR_LMP
        data["due_date"] = parsed.isoformat()
    else:
        if not _is_valid_lmp(parsed):
            await update.message.reply_text(
                "❌ LMP date must be between 1 and 280 days in the past. "
                "Please try again."
            )
            return DUE_DATE_OR_LMP
        data["lmp_date"] = parsed.isoformat()

    await _save_state(telegram_user_id, COUNTRY, data)

    await update.message.reply_text(
        "🌍 Where are you located?",
        reply_markup=country_keyboard(),
    )
    return COUNTRY


async def handle_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback query handler for country selection."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["country"] = value
    await _save_state(telegram_user_id, TIMEZONE, data)

    await query.edit_message_text(
        "🕐 Choose your timezone:",
        reply_markup=timezone_keyboard(value),
    )
    return TIMEZONE


async def handle_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback query handler for timezone selection (inline keyboard)."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, tz_value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    # Validate using pytz
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

    await query.edit_message_text(
        "🌐 Choose your preferred language:",
        reply_markup=language_keyboard(),
    )
    return LANGUAGE


async def handle_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback query handler for language selection."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["language"] = value
    await _save_state(telegram_user_id, FIRST_PREGNANCY, data)

    await query.edit_message_text(
        "Is this your first pregnancy?",
        reply_markup=yes_no_keyboard("first_preg"),
    )
    return FIRST_PREGNANCY


async def handle_first_pregnancy(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback query handler for first-pregnancy flag."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["first_pregnancy"] = value == "yes"
    role = data.get("role", UserRole.mom.value)

    await _save_state(telegram_user_id, FOOD_PREFERENCE, data)

    await query.edit_message_text(
        "🍽️ What's your dietary preference?",
        reply_markup=food_preference_keyboard(),
    )
    return FOOD_PREFERENCE


async def handle_food_preference(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback query handler for dietary preference."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]
    role = data.get("role", UserRole.mom.value)

    data["food_preference"] = value

    if role == UserRole.partner.value:
        # Partner: skip exercise_habit, go to wake time
        await _save_state(telegram_user_id, WAKE_TIME, data)
        await query.edit_message_text(
            "⏰ What time do you usually wake up? (HH:MM, 24-hour format)"
        )
        return WAKE_TIME
    else:
        await _save_state(telegram_user_id, EXERCISE_HABIT, data)
        await query.edit_message_text(
            "🏃 How would you describe your current exercise habits?",
            reply_markup=exercise_habit_keyboard(),
        )
        return EXERCISE_HABIT


async def handle_exercise_habit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback query handler for exercise habits (Mom only)."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["exercise_habit"] = value
    await _save_state(telegram_user_id, WAKE_TIME, data)

    # If user selected "none", offer a light exercise reminder
    if value == "none":
        await query.edit_message_text(
            "💡 No worries! Light movement like a 10-minute walk can do wonders "
            "during pregnancy. I can send you a gentle daily reminder to move — "
            "we can set that up after onboarding.\n\n"
            "⏰ What time do you usually wake up?",
            reply_markup=wake_time_keyboard(),
        )
    else:
        await query.edit_message_text(
            "⏰ What time do you usually wake up?",
            reply_markup=wake_time_keyboard(),
        )
    return WAKE_TIME


async def handle_wake_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback query handler for wake time."""
    assert update.callback_query is not None
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)  # type: ignore[union-attr]
    data = _get_data(context)
    telegram_user_id = update.effective_user.id  # type: ignore[union-attr]

    data["wake_time"] = value
    await _save_state(telegram_user_id, SLEEP_TIME, data)

    await query.edit_message_text(
        "🌙 What time do you usually go to sleep?",
        reply_markup=sleep_time_keyboard(),
    )
    return SLEEP_TIME


async def handle_sleep_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback query handler for sleep time."""
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


async def handle_support_prefs(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback query handler for partner support preferences."""
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


async def handle_shared_timeline(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback query handler for shared timeline preference (Partner only)."""
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
    Persist the ``User`` record and send a confirmation message.

    Called from the last step of both Mom and Partner flows.
    """
    logger.info(
        "onboarding_completing",
        telegram_user_id=telegram_user_id,
    )

    user = await _upsert_user(telegram_user_id, data)
    await _clear_state(telegram_user_id)

    # --- Admin approval gate -------------------------------------------------
    # Mark this user as pending and notify the admin.
    # The user is informed they're awaiting approval; the admin then uses
    # /approve <telegram_user_id> to activate their trial.
    from app.services.admin_service import is_admin, mark_pending
    from app.config import settings as _settings

    if not is_admin(telegram_user_id) and _settings.admin_telegram_user_id != 0:
        mark_pending(
            telegram_user_id,
            user_id=user.id,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            country=user.country,
        )

        # Notify the admin via Telegram (fire-and-forget, non-blocking)
        import asyncio as _asyncio

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
            "✅ *Setup complete!*\n\n"
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

        logger.info(
            "onboarding_complete_pending_approval",
            telegram_user_id=telegram_user_id,
        )
        return ConversationHandler.END
    # -------------------------------------------------------------------------

    # Build gestational context line
    from app.components.pregnancy_engine import calculate_gestational_age
    import pytz
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

    # Detect low exercise for the reminder nudge
    low_exercise = data.get("exercise_habit") in ("none", None)
    exercise_tip = (
        "\n\n💡 Since you mentioned you don't exercise much, try sending me:\n"
        "*\"remind me to take a 10-minute walk every day at 5pm\"*"
        if low_exercise else ""
    )

    welcome = (
        f"✅ All set, {role_display}!\n"
        f"{progress}\n\n"
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

    # Send the confirmation — handle both message and callback_query contexts
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(welcome, parse_mode="Markdown")
    elif hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(welcome, parse_mode="Markdown")
    else:
        await update_or_query.reply_text(welcome, parse_mode="Markdown")

    logger.info(
        "onboarding_complete",
        telegram_user_id=telegram_user_id,
        role=user.role.value if hasattr(user.role, "value") else user.role,
    )

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler builder and registration
# ---------------------------------------------------------------------------

def build_onboarding_handler() -> ConversationHandler:
    """
    Construct the :class:`ConversationHandler` for onboarding.

    Registered at group 0 so it runs after the AuthMiddleware (group -1).
    The ``/cancel`` command can abort the conversation from any state.
    """

    async def cmd_cancel(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
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
            ROLE: [
                CallbackQueryHandler(handle_role, pattern=r"^role:(mom|partner)$"),
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
                CallbackQueryHandler(
                    handle_first_pregnancy, pattern=r"^first_preg:(yes|no)$"
                ),
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
                CallbackQueryHandler(
                    handle_shared_timeline, pattern=r"^shared_timeline:(yes|no)$"
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        name="onboarding",
        persistent=False,
    )


# Module-level handler instance for direct import
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

*🍽️ Nutrition & cravings*:
• _"Is it okay to eat pineapple?"_
• _"Give me some iron-rich meal ideas"_

*Commands*:
/start — restart or re-run setup
/help — show this message
/cancel — cancel any active conversation
"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — send a capabilities overview."""
    assert update.message is not None
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


def register(application: Application) -> None:
    """
    Register the onboarding :class:`ConversationHandler` on *application*.

    Called from :func:`app.main._register_handlers`::

        from app.bot.handlers.onboarding import register as register_onboarding
        register_onboarding(bot_app)
    """
    application.add_handler(onboarding_handler)
    application.add_handler(CommandHandler("help", cmd_help))
    logger.debug("onboarding_handler_registered")


__all__ = [
    # State constants
    "ROLE",
    "DUE_DATE_OR_LMP",
    "COUNTRY",
    "TIMEZONE",
    "LANGUAGE",
    "FIRST_PREGNANCY",
    "FOOD_PREFERENCE",
    "EXERCISE_HABIT",
    "WAKE_TIME",
    "SLEEP_TIME",
    "SUPPORT_PREFS",
    "SHARED_TIMELINE",
    # Handler / registration
    "onboarding_handler",
    "register",
]
