"""
Test harness — routes messages through the real Famosi pipeline and captures responses.

Uses the real DB, real LLM (nano/mini tiers), but mocks Telegram updates so no
actual Telegram API calls are needed.

For each scenario:
  1. Set up DB state (user, pre-logged records, family unit)
  2. Route each message through the dispatcher or relevant handler
  3. Capture the bot's response text
  4. Tear down (rollback transaction)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings
from app.models.base import Base
from app.models.user import User, UserRole, FoodPreference
from app.models.family_unit import FamilyUnit
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.consent import ConsentRecord
from app.models.appointment import Appointment, AppointmentType
from app.models.reminder import Reminder, ReminderType
from app.models.symptom import Symptom
from app.models.meal import Meal, MealItem, VisibilityLevel
from app.models.exercise import Exercise
from app.models.medication import Medication
from app.models.weight_log import WeightLog
from app.models.water_log import WaterLog
from app.models.doctor_question import DoctorQuestion
from app.models.preference import Preference, PreferenceType


# ---------------------------------------------------------------------------
# Fake Update builder
# ---------------------------------------------------------------------------

def make_update(text: str, telegram_user_id: int = 100001) -> MagicMock:
    """Build a mock Telegram Update that looks real enough for the dispatcher."""
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id
    update.edited_message = None

    if text.startswith("/"):
        # Command update
        update.message = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock(return_value=MagicMock(
            edit_text=AsyncMock(), delete=AsyncMock()
        ))
        update.callback_query = None
    elif text in ("CONFIRM_SAVE", "CONFIRM_EDIT", "CONFIRM_CANCEL"):
        # Simulate inline keyboard button press
        from app.bot.keyboards.confirm import CONFIRM_SAVE, CONFIRM_EDIT, CONFIRM_CANCEL
        cb_map = {
            "CONFIRM_SAVE": CONFIRM_SAVE,
            "CONFIRM_EDIT": CONFIRM_EDIT,
            "CONFIRM_CANCEL": CONFIRM_CANCEL,
        }
        update.message = None
        update.callback_query = MagicMock()
        update.callback_query.data = cb_map[text]
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message = MagicMock()
        update.callback_query.message.reply_text = AsyncMock()
    else:
        update.message = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock(return_value=MagicMock(
            edit_text=AsyncMock(), delete=AsyncMock()
        ))
        update.callback_query = None

    return update


def make_context(user_obj: Optional[User] = None,
                 approval_pending: bool = False) -> MagicMock:
    """Build a mock PTB context with bot_data and user_data."""
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {
        "current_user": user_obj,
        "is_admin": False,
        "approval_pending": approval_pending,
        "read_only": user_obj is None,
        "force_mini_tier": False,
        "daily_cap_note": None,
    }
    return ctx


# ---------------------------------------------------------------------------
# DB setup helpers
# ---------------------------------------------------------------------------

async def setup_user(db: AsyncSession, setup: dict) -> tuple[User, Optional[FamilyUnit]]:
    """Create a test user with the given setup config. Returns (user, family_unit)."""
    role_str = setup.get("role", "mom")
    if role_str is None:
        return None, None

    lmp_days = setup.get("lmp_days_ago", 60)
    lmp_date = (datetime.now(timezone.utc) - timedelta(days=lmp_days)).date()
    due_days = setup.get("due_days_from_now")
    due_date = (datetime.now(timezone.utc) + timedelta(days=due_days)).date() if due_days else None
    if due_date is None and lmp_days:
        due_date = (datetime.now(timezone.utc) + timedelta(days=(280 - lmp_days))).date()

    food_pref_str = setup.get("food_preference", "vegetarian")
    try:
        food_pref = FoodPreference(food_pref_str)
    except ValueError:
        food_pref = None

    user = User(
        telegram_user_id=setup.get("telegram_user_id", 100001),
        role=UserRole(role_str),
        due_date=due_date,
        lmp_date=lmp_date,
        country=setup.get("country", "IN"),
        timezone=setup.get("timezone", "Asia/Kolkata"),
        language=setup.get("language", "en"),
        first_pregnancy=setup.get("first_pregnancy", True),
        food_preference=food_pref,
        onboarding_complete=True,
    )
    db.add(user)
    await db.flush()

    # Consent
    if setup.get("consent", True):
        db.add(ConsentRecord(
            user_id=user.id,
            policy_version="1.0",
            accepted_at=datetime.now(timezone.utc),
        ))

    # Subscription
    now = datetime.now(timezone.utc)
    db.add(Subscription(
        user_id=user.id,
        subscription_status=SubscriptionStatus.trial,
        payment_status="none",
        trial_start=now,
        trial_end=now + timedelta(days=7),
        payment_retry_count=0,
    ))
    await db.flush()

    # Family unit if partner
    family_unit = None
    if setup.get("family"):
        # Also create a mom user and link
        mom_user = User(
            telegram_user_id=setup.get("mom_telegram_id", 100002),
            role=UserRole.mom,
            due_date=due_date,
            lmp_date=lmp_date,
            country="IN",
            timezone="Asia/Kolkata",
            language="en",
            first_pregnancy=True,
            onboarding_complete=True,
        )
        db.add(mom_user)
        await db.flush()

        family_unit = FamilyUnit(invite_code="EVAL01", invite_used=True, mom_user_id=mom_user.id)
        db.add(family_unit)
        await db.flush()

        user.family_unit_id = family_unit.id
        mom_user.family_unit_id = family_unit.id
        await db.flush()

        # Pre-log on mom's account for partner visibility tests
        partner_logs = setup.get("partner_pre_log", [])
        for log in partner_logs:
            await _insert_record(db, mom_user.id, log)

    # Pre-log on this user's account
    pre_logs = setup.get("pre_log", [])
    for log in pre_logs:
        await _insert_record(db, user.id, log)

    await db.flush()
    return user, family_unit


async def _insert_record(db: AsyncSession, user_id: int, log: dict) -> None:
    """Insert a pre-logged record for setup state."""
    record_type = log.get("type")
    days_ago = log.get("days_ago", 1)
    logged_at = datetime.now(timezone.utc) - timedelta(days=days_ago)

    vis_str = log.get("visibility", "private")
    try:
        vis = VisibilityLevel(vis_str)
    except ValueError:
        vis = VisibilityLevel.private

    if record_type == "meal":
        meal = Meal(user_id=user_id, visibility_level=vis, logged_at=logged_at, confirmed_at=logged_at)
        db.add(meal)
        await db.flush()
        for item in log.get("items", []):
            db.add(MealItem(meal_id=meal.id, food_name=item["food_name"],
                            quantity=item.get("quantity"), unit=item.get("unit")))

    elif record_type == "symptom":
        db.add(Symptom(
            user_id=user_id,
            visibility_level=vis,
            symptom_name=log.get("symptom_name", "nausea"),
            severity=log.get("severity", 5),
            frequency=log.get("frequency", 1),
            logged_at=logged_at,
            confirmed_at=logged_at,
        ))

    elif record_type == "exercise":
        db.add(Exercise(
            user_id=user_id,
            visibility_level=vis,
            activity_type=log.get("activity_type", "yoga"),
            duration_minutes=log.get("duration_minutes", 30),
            logged_at=logged_at,
            confirmed_at=logged_at,
        ))

    elif record_type == "medication":
        db.add(Medication(
            user_id=user_id,
            visibility_level=vis,
            medication_name=log.get("medication_name", "iron"),
            dose=log.get("dose"),
            logged_at=logged_at,
            confirmed_at=logged_at,
        ))

    elif record_type == "weight":
        db.add(WeightLog(
            user_id=user_id,
            visibility_level=vis,
            value=log.get("value", 67.0),
            unit=log.get("unit", "kg"),
            logged_at=logged_at,
            confirmed_at=logged_at,
        ))

    elif record_type == "water":
        db.add(WaterLog(
            user_id=user_id,
            visibility_level=vis,
            volume=log.get("volume", 500),
            unit=log.get("unit", "ml"),
            logged_at=logged_at,
            confirmed_at=logged_at,
        ))

    elif record_type == "question":
        db.add(DoctorQuestion(
            user_id=user_id,
            visibility_level=vis,
            question_text=log.get("question_text", "Test question"),
            doctor_visit_tagged=True,
            used_in_summary=False,
            logged_at=logged_at,
            confirmed_at=logged_at,
        ))

    elif record_type == "preference":
        try:
            pref_type = PreferenceType(log.get("preference_type", "dislike"))
        except ValueError:
            pref_type = PreferenceType.dislike
        db.add(Preference(
            user_id=user_id,
            preference_type=pref_type,
            food_item=log.get("food_item", "yogurt"),
            active=True,
            confirmed_at=logged_at,
        ))

    elif record_type == "appointment":
        days_from_now = log.get("days_from_now", 7)
        appt_at = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        appt_type_str = log.get("appointment_type", "ob_visit")
        try:
            appt_type = AppointmentType(appt_type_str)
        except ValueError:
            appt_type = AppointmentType.ob_visit
        db.add(Appointment(
            user_id=user_id,
            visibility_level=vis,
            appointment_type=appt_type,
            appointment_at=appt_at,
            cancelled=False,
            confirmed_at=logged_at,
        ))


# ---------------------------------------------------------------------------
# Response capture
# ---------------------------------------------------------------------------

def _collect_responses(update: MagicMock) -> list[str]:
    """Extract all text sent to the user from a processed update."""
    responses = []

    if update.message:
        # Collect from reply_text calls
        for call in update.message.reply_text.call_args_list:
            args = call.args or ()
            kwargs = call.kwargs or {}
            text = args[0] if args else kwargs.get("text", "")
            if text:
                responses.append(str(text))

        # Collect from edit calls on the thinking message
        ret = update.message.reply_text.return_value
        if ret and hasattr(ret, "edit_text"):
            for call in ret.edit_text.call_args_list:
                args = call.args or ()
                kwargs = call.kwargs or {}
                text = args[0] if args else kwargs.get("text", "")
                if text:
                    responses.append(str(text))

    if update.callback_query:
        for call in update.callback_query.edit_message_text.call_args_list:
            args = call.args or ()
            kwargs = call.kwargs or {}
            text = args[0] if args else kwargs.get("text", "")
            if text:
                responses.append(str(text))
        for call in update.callback_query.message.reply_text.call_args_list:
            args = call.args or ()
            kwargs = call.kwargs or {}
            text = args[0] if args else kwargs.get("text", "")
            if text:
                responses.append(str(text))

    return responses


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

async def run_scenario(scenario: dict, db: AsyncSession) -> dict:
    """
    Run a single scenario against the real app pipeline.

    Returns:
        {
            "id": int,
            "messages": list[str],
            "responses": list[str],   # all text sent to user
            "full_response": str,      # joined responses for judge
            "error": str | None,
        }
    """
    from app.core.llm_client import LLMClient
    from app.core.intent_router import IntentRouter
    from app.bot.dispatcher import dispatch
    from app.bot.handlers.logging_handler import handle_confirm_callback

    setup = scenario.get("setup", {})
    messages = scenario.get("messages", [])

    # ── Setup DB state ──────────────────────────────────────────────────
    approval_pending = not setup.get("approved", True)
    user, family_unit = await setup_user(db, setup)

    llm_client = LLMClient()
    all_responses: list[str] = []

    context = make_context(user_obj=user, approval_pending=approval_pending)

    for msg_text in messages:
        update = make_update(msg_text, telegram_user_id=setup.get("telegram_user_id", 100001))

        try:
            if msg_text.startswith("CONFIRM_"):
                # Handle confirm callbacks through the logging handler
                await handle_confirm_callback(update, context)
            else:
                # Route through the full dispatcher
                await dispatch(update, context, llm_client=llm_client)
        except Exception as exc:  # noqa: BLE001
            return {
                "id": scenario["id"],
                "messages": messages,
                "responses": all_responses,
                "full_response": "\n".join(all_responses),
                "error": f"{type(exc).__name__}: {exc}",
            }

        responses = _collect_responses(update)
        all_responses.extend(responses)

    return {
        "id": scenario["id"],
        "messages": messages,
        "responses": all_responses,
        "full_response": "\n".join(all_responses) or "(no response captured)",
        "error": None,
    }
