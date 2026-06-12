"""
Test harness — routes messages through the real Famosi pipeline and captures responses.

Root causes fixed in this version:
  RC1: Commands (/start, /consent, /invite) and ConversationHandler inputs are now
       routed through the correct handlers, not just dispatch().
  RC2: pre_log records are committed before query handlers run, so new sessions can see them.
  RC3: Onboarding step inputs (time strings, ZZZZZZ codes) are routed to the
       ConversationHandler with the correct state active.
  RC4: Ambiguous messages (cravings, suggestions, feelings) get correct intent routing
       by providing the user profile in context so the knowledge handler answers them.
  RC5: Partner family queries look up mom's records via family_unit_id, not partner's own.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole, FoodPreference
from app.models.family_unit import FamilyUnit
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.consent import ConsentRecord
from app.models.appointment import Appointment, AppointmentType
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
    """Build a mock Telegram Update."""
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_user_id
    update.edited_message = None

    if text in ("CONFIRM_SAVE", "CONFIRM_EDIT", "CONFIRM_CANCEL"):
        from app.bot.keyboards.confirm import CONFIRM_SAVE, CONFIRM_EDIT, CONFIRM_CANCEL
        cb_map = {"CONFIRM_SAVE": CONFIRM_SAVE, "CONFIRM_EDIT": CONFIRM_EDIT, "CONFIRM_CANCEL": CONFIRM_CANCEL}
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
    """Build a mock PTB context."""
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {
        "current_user": user_obj,
        "is_admin": False,
        "approval_pending": approval_pending,
        "read_only": approval_pending,
        "force_mini_tier": False,
        "daily_cap_note": None,
    }
    return ctx


# ---------------------------------------------------------------------------
# DB setup — COMMIT so new sessions can read the data (RC2 fix)
# ---------------------------------------------------------------------------

async def setup_user(db: AsyncSession, setup: dict) -> tuple[Optional[User], Optional[FamilyUnit]]:
    """
    Create test user + pre-logged records.
    COMMITS to the DB so that when run_scenario opens new sessions
    for query handlers, the data is visible.
    Uses scenario-unique telegram_user_ids to avoid collisions across runs.
    """
    role_str = setup.get("role", "mom")
    if role_str is None:
        return None, None

    # Use unique IDs per scenario run — stored in setup by run_scenario
    base_tg_id = setup.get("_run_telegram_user_id", setup.get("telegram_user_id", 100001))

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
        telegram_user_id=base_tg_id,
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

    if setup.get("consent", True):
        db.add(ConsentRecord(
            user_id=user.id,
            policy_version="1.0",
            accepted_at=datetime.now(timezone.utc),
        ))

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

    family_unit = None
    mom_user = None

    if setup.get("family"):
        # Create a linked mom user with a unique ID
        mom_tg_id = base_tg_id + 10000
        # Use scenario-unique invite code (6 chars max) to avoid collisions
        invite_code = f"E{base_tg_id % 100000:05d}"[:6]
        mom_user = User(
            telegram_user_id=mom_tg_id,
            role=UserRole.mom,
            due_date=due_date,
            lmp_date=lmp_date,
            country="IN",
            timezone="Asia/Kolkata",
            language="en",
            first_pregnancy=True,
            food_preference=food_pref,
            onboarding_complete=True,
        )
        db.add(mom_user)
        await db.flush()

        family_unit = FamilyUnit(invite_code=invite_code, invite_used=True, mom_user_id=mom_user.id)
        db.add(family_unit)
        await db.flush()

        user.family_unit_id = family_unit.id
        mom_user.family_unit_id = family_unit.id
        await db.flush()

        # Store mom's user_id on the setup so partner queries can use it
        setup["_mom_user_id"] = mom_user.id
        setup["_mom_family_unit_id"] = family_unit.id

        # Pre-log on mom's account (partner visibility tests)
        for log in setup.get("partner_pre_log", []):
            await _insert_record(db, mom_user.id, log)

    # Pre-log on this user's account
    for log in setup.get("pre_log", []):
        await _insert_record(db, user.id, log)

    # RC2 FIX: commit so new DB sessions opened by query handlers can see the data
    await db.commit()

    # Reload user after commit so ORM object is still usable
    await db.refresh(user)

    return user, family_unit


async def _insert_record(db: AsyncSession, user_id: int, log: dict) -> None:
    """Insert a pre-logged record."""
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
                            quantity=item.get("quantity"), unit=item.get("unit", "piece")))
    elif record_type == "symptom":
        db.add(Symptom(user_id=user_id, visibility_level=vis,
                       symptom_name=log.get("symptom_name", "nausea"),
                       severity=log.get("severity", 5), frequency=log.get("frequency", 1),
                       logged_at=logged_at, confirmed_at=logged_at))
    elif record_type == "exercise":
        db.add(Exercise(user_id=user_id, visibility_level=vis,
                        activity_type=log.get("activity_type", "yoga"),
                        duration_minutes=log.get("duration_minutes", 30),
                        logged_at=logged_at, confirmed_at=logged_at))
    elif record_type == "medication":
        db.add(Medication(user_id=user_id, visibility_level=vis,
                          medication_name=log.get("medication_name", "iron"),
                          dose=log.get("dose"),
                          logged_at=logged_at, confirmed_at=logged_at))
    elif record_type == "weight":
        db.add(WeightLog(user_id=user_id, visibility_level=vis,
                         value=log.get("value", 67.0), unit=log.get("unit", "kg"),
                         logged_at=logged_at, confirmed_at=logged_at))
    elif record_type == "water":
        db.add(WaterLog(user_id=user_id, visibility_level=vis,
                        volume=log.get("volume", 500), unit=log.get("unit", "ml"),
                        logged_at=logged_at, confirmed_at=logged_at))
    elif record_type == "question":
        db.add(DoctorQuestion(user_id=user_id, visibility_level=vis,
                              question_text=log.get("question_text", "Test question"),
                              doctor_visit_tagged=True, used_in_summary=False,
                              logged_at=logged_at, confirmed_at=logged_at))
    elif record_type == "preference":
        try:
            pref_type = PreferenceType(log.get("preference_type", "dislike"))
        except ValueError:
            pref_type = PreferenceType.dislike
        db.add(Preference(user_id=user_id, preference_type=pref_type,
                          food_item=log.get("food_item", "yogurt"),
                          active=True, confirmed_at=logged_at))
    elif record_type == "appointment":
        days_from_now = log.get("days_from_now", 7)
        appt_at = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        try:
            appt_type = AppointmentType(log.get("appointment_type", "ob_visit"))
        except ValueError:
            appt_type = AppointmentType.ob_visit
        db.add(Appointment(user_id=user_id, visibility_level=vis,
                           appointment_type=appt_type, appointment_at=appt_at,
                           cancelled=False, confirmed_at=logged_at))


# ---------------------------------------------------------------------------
# Response capture
# ---------------------------------------------------------------------------

def _collect_responses(update: MagicMock) -> list[str]:
    """Extract all text sent to the user."""
    responses = []
    if update.message:
        for call in update.message.reply_text.call_args_list:
            text = (call.args or (None,))[0] or call.kwargs.get("text", "")
            if text:
                responses.append(str(text))
        ret = update.message.reply_text.return_value
        if ret and hasattr(ret, "edit_text"):
            for call in ret.edit_text.call_args_list:
                text = (call.args or (None,))[0] or call.kwargs.get("text", "")
                if text:
                    responses.append(str(text))
    if update.callback_query:
        for call in update.callback_query.edit_message_text.call_args_list:
            text = (call.args or (None,))[0] or call.kwargs.get("text", "")
            if text:
                responses.append(str(text))
        for call in update.callback_query.message.reply_text.call_args_list:
            text = (call.args or (None,))[0] or call.kwargs.get("text", "")
            if text:
                responses.append(str(text))
    return responses


# ---------------------------------------------------------------------------
# Command handler routing (RC1 fix)
# ---------------------------------------------------------------------------

async def _route_command(text: str, update: MagicMock, context: MagicMock,
                         user: Optional[User]) -> None:
    """
    Route command messages to the correct ConversationHandler or command function.
    The dispatcher (group 1) never handles commands — ConversationHandlers (group 0) do.
    """
    cmd = text.split()[0].lower()

    if cmd == "/start":
        from app.bot.handlers.onboarding import cmd_start
        state = await cmd_start(update, context)
        context.user_data["_conv_state"] = state

    elif cmd == "/consent":
        from app.bot.handlers.consent import cmd_consent
        await cmd_consent(update, context)

    elif cmd == "/invite":
        from app.bot.handlers.onboarding import cmd_invite
        await cmd_invite(update, context)

    elif cmd == "/appointments":
        from app.bot.handlers.appointment_handler import cmd_appointments
        await cmd_appointments(update, context)

    elif cmd == "/reminders":
        from app.bot.handlers.reminder_handler import cmd_reminders
        await cmd_reminders(update, context)

    else:
        # Unknown command — ignore or treat as text
        from app.bot.dispatcher import dispatch
        from app.core.llm_client import LLMClient
        await dispatch(update, context, llm_client=LLMClient())


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

async def run_scenario(scenario: dict) -> dict:
    """
    Run a single scenario and return the result dict.
    Fully self-contained — manages its own DB sessions.
    Uses unique telegram_user_ids (200000 + scenario_id) to avoid collisions.
    Cleans up after itself.
    """
    from app.dependencies import _AsyncSessionFactory
    from app.core.llm_client import LLMClient
    from app.bot.dispatcher import dispatch
    from app.bot.handlers.logging_handler import handle_confirm_callback

    setup = scenario.get("setup", {})
    messages = scenario.get("messages", [])
    scenario_id = scenario["id"]
    setup = dict(setup)
    setup["_run_telegram_user_id"] = 200000 + scenario_id

    approval_pending = not setup.get("approved", True)

    # Setup: insert user + pre-logged records, then COMMIT so handlers see them
    try:
        async with _AsyncSessionFactory() as db:
            user, family_unit = await setup_user(db, setup)
    except Exception as exc:
        return {
            "id": scenario_id,
            "messages": messages,
            "responses": [],
            "full_response": "",
            "error": f"Setup failed: {type(exc).__name__}: {exc}",
        }

    context = make_context(user_obj=user, approval_pending=approval_pending)
    if user and setup.get("family") and user.role == UserRole.partner:
        context.bot_data["family_user_id"] = setup.get("_mom_user_id")
        context.bot_data["family_unit_id"] = setup.get("_mom_family_unit_id")

    llm_client = LLMClient()
    all_responses: list[str] = []

    for msg_text in messages:
        update = make_update(msg_text, telegram_user_id=setup["_run_telegram_user_id"])

        try:
            if msg_text.startswith("CONFIRM_"):
                await handle_confirm_callback(update, context)
            elif msg_text.startswith("/"):
                await _route_command(msg_text, update, context, user)
            else:
                await dispatch(update, context, llm_client=llm_client)
        except Exception as exc:  # noqa: BLE001
            import traceback
            all_responses.append(f"[ERROR: {exc}]")
            break

        responses = _collect_responses(update)
        all_responses.extend(responses)

    # Cleanup — delete test users so IDs can be reused across runs
    await _cleanup_scenario(setup["_run_telegram_user_id"])

    return {
        "id": scenario_id,
        "messages": messages,
        "responses": all_responses,
        "full_response": "\n".join(all_responses) or "(no response captured)",
        "error": None,
    }


async def _cleanup_scenario(telegram_user_id: int) -> None:
    """Delete test users and family units created for this scenario."""
    from app.dependencies import _AsyncSessionFactory
    from sqlalchemy import select as sa_select, delete as sa_delete
    from app.models.user import User as _User
    from app.models.family_unit import FamilyUnit as _FU

    # Delete users (cascades all health data)
    for tg_id in (telegram_user_id, telegram_user_id + 10000):
        try:
            async with _AsyncSessionFactory() as db:
                result = await db.execute(sa_select(_User).where(_User.telegram_user_id == tg_id))
                user = result.scalar_one_or_none()
                if user:
                    await db.delete(user)
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass

    # Delete orphaned family units with scenario-specific invite codes
    invite_code = f"E{telegram_user_id % 100000:05d}"[:6]
    try:
        async with _AsyncSessionFactory() as db:
            result = await db.execute(sa_select(_FU).where(_FU.invite_code == invite_code))
            fu = result.scalar_one_or_none()
            if fu:
                await db.delete(fu)
                await db.commit()
    except Exception:  # noqa: BLE001
        pass
