"""
Test harness — routes messages through the real Famosi pipeline and captures responses.

Design
------
The harness uses mock Telegram Update/Context objects and calls the real
handler functions directly. This avoids PTB Application complexity while
exercising the full business logic (LLM, DB, extractors, handlers).

Handler routing:
  - Commands (/start, /consent, /invite, /appointments): routed to the
    correct command handler directly.
  - CONFIRM_* buttons: routed to handle_confirm_callback directly.
  - Free-text messages: routed through dispatch() (intent router → handler).

DB state:
  - Test users are committed to the real DB before the scenario runs,
    so query handlers in new sessions see the data.
  - Unique telegram_user_ids (200000 + scenario_id) prevent cross-run
    collisions. Stale data is cleaned at setup start AND after the run.
  - The in-process ConfirmationStore is cleared per-user before each
    scenario to prevent cross-scenario state contamination.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

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
# Mock Update / Context builders
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
# Response collector
# ---------------------------------------------------------------------------

def _collect_responses(update: MagicMock) -> list[str]:
    """Extract all text sent to the user from a mock update, including inline keyboard button labels."""
    responses = []

    def _extract_keyboard_text(call_kwargs: dict) -> str:
        """Extract button labels from reply_markup if present."""
        markup = call_kwargs.get("reply_markup")
        if markup is None:
            return ""
        # InlineKeyboardMarkup has an inline_keyboard attribute (list of rows)
        try:
            rows = getattr(markup, "inline_keyboard", None)
            if rows:
                labels = []
                for row in rows:
                    for btn in row:
                        label = getattr(btn, "text", None)
                        if label:
                            labels.append(label)
                if labels:
                    return " | ".join(labels)
        except Exception:
            pass
        return ""

    def _extract_from_call(call) -> None:
        args = call.args or (None,)
        text = args[0] if args else call.kwargs.get("text", "")
        if text:
            responses.append(str(text))
            # Append button labels on the same logical message
            kb_text = _extract_keyboard_text(call.kwargs)
            if kb_text:
                responses.append(f"[Buttons: {kb_text}]")

    if getattr(update, "message", None) and update.message is not None:
        for call in update.message.reply_text.call_args_list:
            _extract_from_call(call)
        ret = update.message.reply_text.return_value
        if ret and hasattr(ret, "edit_text"):
            for call in ret.edit_text.call_args_list:
                _extract_from_call(call)

    if getattr(update, "callback_query", None) and update.callback_query is not None:
        for call in update.callback_query.edit_message_text.call_args_list:
            _extract_from_call(call)
        if hasattr(update.callback_query, "message") and update.callback_query.message is not None:
            for call in update.callback_query.message.reply_text.call_args_list:
                _extract_from_call(call)

    return responses


# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------

async def _route_command(text: str, update: MagicMock, context: MagicMock,
                         user: Optional[User]) -> None:
    """Route commands to the correct handler function."""
    cmd = text.split()[0].lower()

    if cmd == "/start":
        from app.bot.handlers.onboarding import cmd_start
        await cmd_start(update, context)

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
        from app.bot.dispatcher import dispatch
        from app.core.llm_client import LLMClient
        await dispatch(update, context, llm_client=LLMClient())


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

async def setup_user(db: AsyncSession, setup: dict) -> tuple[Optional[User], Optional[FamilyUnit]]:
    """
    Create test user + pre-logged records.
    Cleans up stale data first, then commits so query handlers see the data.
    """
    role_str = setup.get("role", "mom")
    if role_str is None:
        return None, None

    base_tg_id = setup.get("_run_telegram_user_id", setup.get("telegram_user_id", 100001))

    # Clean up stale data from prior runs before inserting
    from sqlalchemy import select as _sa_select
    from app.models.user import User as _User
    from app.models.family_unit import FamilyUnit as _FU

    for tg_id in (base_tg_id, base_tg_id + 10000):
        existing = await db.execute(_sa_select(_User).where(_User.telegram_user_id == tg_id))
        u = existing.scalar_one_or_none()
        if u:
            await db.delete(u)
    invite_code_val = f"E{base_tg_id % 100000:05d}"[:6]
    existing_fu = await db.execute(_sa_select(_FU).where(_FU.invite_code == invite_code_val))
    fu = existing_fu.scalar_one_or_none()
    if fu:
        await db.delete(fu)
    await db.flush()

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
        mom_tg_id = base_tg_id + 10000
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

        family_unit = FamilyUnit(
            invite_code=invite_code_val,
            invite_used=True,
            mom_user_id=mom_user.id,
        )
        db.add(family_unit)
        await db.flush()

        user.family_unit_id = family_unit.id
        mom_user.family_unit_id = family_unit.id
        await db.flush()

        setup["_mom_user_id"] = mom_user.id
        setup["_mom_family_unit_id"] = family_unit.id

        for log_entry in setup.get("partner_pre_log", []):
            await _insert_record(db, mom_user.id, log_entry)

    for log_entry in setup.get("pre_log", []):
        await _insert_record(db, user.id, log_entry)

    await db.commit()
    await db.refresh(user)

    return user, family_unit


async def _insert_record(db: AsyncSession, user_id: int, log: dict) -> None:
    """Insert a pre-logged health record."""
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
# Main run function
# ---------------------------------------------------------------------------

async def run_scenario(scenario: dict) -> dict:
    """
    Run a single scenario and return results.

    Routes each message through the correct handler:
    - Commands → command handler function
    - CONFIRM_* → confirm callback handler
    - Free text → dispatch() (intent router → handler pipeline)
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

    # DB setup
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

    # Clear stale ConfirmationStore sessions to prevent cross-scenario contamination
    try:
        from app.bot.handlers import logging_handler as _lh
        await _lh._store.delete(f"log:{setup['_run_telegram_user_id']}")
    except Exception:
        pass

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
            all_responses.append(f"[ERROR: {exc}]")
            break

        all_responses.extend(_collect_responses(update))

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
    from sqlalchemy import select as sa_select
    from app.models.user import User as _User
    from app.models.family_unit import FamilyUnit as _FU

    for tg_id in (telegram_user_id, telegram_user_id + 10000):
        try:
            async with _AsyncSessionFactory() as db:
                result = await db.execute(sa_select(_User).where(_User.telegram_user_id == tg_id))
                u = result.scalar_one_or_none()
                if u:
                    await db.delete(u)
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass

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
