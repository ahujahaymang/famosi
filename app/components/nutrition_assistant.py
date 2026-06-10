"""
Nutrition Assistant — nutrient estimation, daily/weekly summaries, deficiency
alerts, and personalised meal suggestions.

Handles:
  - Estimating nutrient content for a confirmed meal via Mini LLM (Req 7.1)
  - Daily nutritional summary with % RDI per nutrient (Req 7.2)
  - Weekly nutritional trend report over the preceding 7 calendar days (Req 7.3)
  - Proactive deficiency alerts when any nutrient falls below 75 % RDI for
    3+ consecutive days (Req 7.4)
  - Personalised meal suggestions, filtered by stored preferences, grounded in
    the Knowledge_Base; graceful fallback when the KB is unavailable (Req 7.5,
    7.6, 7.7)

RDI values used (pregnancy-specific, per day):
  - Protein : 71 g   (WHO/IOM)
  - Iron    : 27 mg  (IOM)
  - Calcium : 1000 mg (IOM)
  - Folate  : 600 mcg (IOM)
  - Fiber   : 28 g   (Academy of Nutrition and Dietetics)

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meal import Meal, MealNutrient
from app.models.user import User
from app.schemas.meal import MealExtraction

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient
    from telegram import Bot

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pregnancy RDI constants
# ---------------------------------------------------------------------------

#: Recommended Daily Intake values for pregnant women (per day).
RDI = {
    "protein_g": Decimal("71.0"),     # g
    "iron_mg": Decimal("27.0"),        # mg
    "calcium_mg": Decimal("1000.0"),   # mg
    "folate_mcg": Decimal("600.0"),    # mcg
    "fiber_g": Decimal("28.0"),        # g
}

#: Threshold below which a nutrient is considered deficient (75 % of RDI).
DEFICIENCY_THRESHOLD = Decimal("0.75")

#: Number of consecutive days below threshold before a deficiency alert fires.
DEFICIENCY_DAYS_THRESHOLD = 3

# ---------------------------------------------------------------------------
# Internal data class for aggregated nutrient totals
# ---------------------------------------------------------------------------


@dataclass
class _NutrientTotals:
    """Per-nutrient sums for a given period."""

    protein_g: Decimal = Decimal("0")
    iron_mg: Decimal = Decimal("0")
    calcium_mg: Decimal = Decimal("0")
    folate_mcg: Decimal = Decimal("0")
    fiber_g: Decimal = Decimal("0")

    def add(self, record: MealNutrient) -> None:
        """Accumulate values from *record*, treating NULL columns as zero."""
        self.protein_g += record.protein_g or Decimal("0")
        self.iron_mg += record.iron_mg or Decimal("0")
        self.calcium_mg += record.calcium_mg or Decimal("0")
        self.folate_mcg += record.folate_mcg or Decimal("0")
        self.fiber_g += record.fiber_g or Decimal("0")

    def pct_rdi(self) -> dict[str, float]:
        """Return percentage of RDI for each nutrient (0–100+)."""
        return {
            "protein_g": float(self.protein_g / RDI["protein_g"] * 100),
            "iron_mg": float(self.iron_mg / RDI["iron_mg"] * 100),
            "calcium_mg": float(self.calcium_mg / RDI["calcium_mg"] * 100),
            "folate_mcg": float(self.folate_mcg / RDI["folate_mcg"] * 100),
            "fiber_g": float(self.fiber_g / RDI["fiber_g"] * 100),
        }

    def deficient_nutrients(self) -> list[str]:
        """Return names of nutrients that are below 75 % of RDI."""
        pct = self.pct_rdi()
        threshold = float(DEFICIENCY_THRESHOLD * 100)
        return [name for name, value in pct.items() if value < threshold]


# ---------------------------------------------------------------------------
# LLM system prompts
# ---------------------------------------------------------------------------

_NUTRIENT_ESTIMATION_SYSTEM = """\
You are a clinical nutrition assistant for a pregnancy tracking app.
Given a list of food items with estimated quantities, estimate the nutritional
content of the meal and return a JSON object with exactly these fields:
  protein_g, iron_mg, calcium_mg, folate_mcg, fiber_g
All values must be numbers (float or int). Use 0 if a nutrient is negligible.
Base estimates on standard nutritional databases. Do not include explanations.
"""

_DAILY_SUMMARY_SYSTEM = """\
You are a helpful pregnancy nutrition assistant.
Given per-nutrient totals for the day and their percentage of recommended
daily intake (RDI) for pregnancy, compose a concise, warm, and encouraging
daily nutritional summary. Highlight nutrients that are well-covered and
gently note any that are below 75 % RDI. Keep the response under 200 words.
Do not use alarming language. Do not include medical advice.
"""

_WEEKLY_TREND_SYSTEM = """\
You are a helpful pregnancy nutrition assistant.
Given daily per-nutrient totals and % RDI across 7 days, write a concise weekly
nutritional trend report. Note improving and declining trends, highlight days
where intake was strong, and gently flag any persistent gaps. Keep the tone
encouraging and the length under 250 words. Do not include medical advice.
"""

_DEFICIENCY_RECOMMENDATION_SYSTEM = """\
You are a pregnancy nutrition assistant.
Given a deficient nutrient name, suggest at least one pregnancy-safe food or
supplement adjustment that can help increase this nutrient.
The user's dietary preference is: {food_preference}.
Return a single, concise recommendation sentence (under 60 words).
"""

_MEAL_SUGGESTION_SYSTEM = """\
You are a helpful pregnancy nutrition assistant.
Using the nutrition knowledge context provided and the user's stored dietary
preferences, generate 3 balanced, pregnancy-safe meal suggestions that address
any noted nutrient gaps. Apply all listed food preferences, dietary restrictions,
and allergies. Keep the response under 200 words and present each suggestion
as a brief bullet point.
"""

_MEAL_SUGGESTION_NO_KB_SYSTEM = """\
You are a helpful pregnancy nutrition assistant.
The nutrition knowledge base is currently unavailable.
Using only the user's confirmed meal history and stored dietary preferences,
generate 3 balanced, pregnancy-safe meal suggestions. Apply all listed food
preferences, dietary restrictions, and allergies. Keep the response under 200
words. Begin with a brief note that the response is based on personal history
only, as the knowledge base is temporarily unavailable.
"""

# ---------------------------------------------------------------------------
# Public: estimate_nutrients
# ---------------------------------------------------------------------------


async def estimate_nutrients(
    meal: Meal,
    meal_extraction: MealExtraction,
    db: AsyncSession,
    llm_client: "LLMClient",
) -> MealNutrient:
    """
    Estimate and persist the nutritional content of a confirmed meal.

    Calls ``LLMClient.complete("mini", ...)`` with the food items from
    *meal_extraction* and expects a JSON response with protein_g, iron_mg,
    calcium_mg, folate_mcg, and fiber_g.  The resulting ``MealNutrient`` row
    is linked to *meal* and flushed to the session.

    Args:
        meal:           The persisted ``Meal`` ORM object (id must be set).
        meal_extraction: The validated ``MealExtraction`` used to create *meal*.
        db:             Active async DB session.
        llm_client:     Shared ``LLMClient`` instance.

    Returns:
        The persisted ``MealNutrient`` ORM object.

    Requirements: 7.1
    """
    log = logger.bind(meal_id=meal.id, user_id=meal.user_id)

    # Build a human-readable food list for the prompt
    food_lines = []
    for item in meal_extraction.items:
        if item.quantity and item.unit:
            food_lines.append(f"- {item.food_name}: {item.quantity} {item.unit}")
        elif item.quantity:
            food_lines.append(f"- {item.food_name}: {item.quantity}")
        else:
            food_lines.append(f"- {item.food_name}")
    food_list = "\n".join(food_lines)

    messages = [
        {"role": "system", "content": _NUTRIENT_ESTIMATION_SYSTEM},
        {
            "role": "user",
            "content": f"Estimate the nutrients for this meal:\n{food_list}",
        },
    ]

    response = await llm_client.complete(
        "mini",
        messages,
        response_format={"type": "json_object"},
    )

    log.info("nutrient_estimation_complete", tokens_used=response.tokens_used)

    # Parse and validate the JSON response
    try:
        data = json.loads(response.content)
    except json.JSONDecodeError:
        log.warning("nutrient_estimation_json_parse_failed")
        data = {}

    def _safe_decimal(value: object) -> Decimal | None:
        """Convert *value* to Decimal, returning None on failure."""
        try:
            return Decimal(str(value)) if value is not None else None
        except Exception:
            return None

    nutrient = MealNutrient(
        meal_id=meal.id,
        protein_g=_safe_decimal(data.get("protein_g")),
        iron_mg=_safe_decimal(data.get("iron_mg")),
        calcium_mg=_safe_decimal(data.get("calcium_mg")),
        folate_mcg=_safe_decimal(data.get("folate_mcg")),
        fiber_g=_safe_decimal(data.get("fiber_g")),
    )
    db.add(nutrient)
    await db.flush()

    log.info("meal_nutrient_persisted", nutrient_id=nutrient.id)
    return nutrient


# ---------------------------------------------------------------------------
# Public: daily_summary
# ---------------------------------------------------------------------------


async def daily_summary(
    user_id: int,
    summary_date: date,
    db: AsyncSession,
    llm_client: "LLMClient",
) -> str:
    """
    Aggregate ``MealNutrient`` records for *summary_date* and return a
    formatted daily nutritional summary with % RDI per nutrient.

    The summary is formatted via the Mini LLM tier for a warm,
    conversational presentation.

    Args:
        user_id:      Internal user PK.
        summary_date: The calendar day to summarise (UTC).
        db:           Active async DB session.
        llm_client:   Shared ``LLMClient`` instance.

    Returns:
        A formatted summary string.

    Requirements: 7.2
    """
    day_start = datetime(
        summary_date.year, summary_date.month, summary_date.day,
        0, 0, 0, tzinfo=timezone.utc,
    )
    day_end = datetime(
        summary_date.year, summary_date.month, summary_date.day,
        23, 59, 59, tzinfo=timezone.utc,
    )

    totals = await _aggregate_nutrients(user_id, day_start, day_end, db)
    pct = totals.pct_rdi()

    log = logger.bind(user_id=user_id, date=str(summary_date))
    log.info("daily_summary_aggregated")

    context = (
        f"Date: {summary_date.isoformat()}\n\n"
        f"Nutrient totals and % of recommended daily intake (RDI):\n"
        f"  Protein  : {float(totals.protein_g):.1f} g  ({pct['protein_g']:.0f}% RDI)\n"
        f"  Iron     : {float(totals.iron_mg):.1f} mg  ({pct['iron_mg']:.0f}% RDI)\n"
        f"  Calcium  : {float(totals.calcium_mg):.0f} mg  ({pct['calcium_mg']:.0f}% RDI)\n"
        f"  Folate   : {float(totals.folate_mcg):.0f} mcg  ({pct['folate_mcg']:.0f}% RDI)\n"
        f"  Fiber    : {float(totals.fiber_g):.1f} g  ({pct['fiber_g']:.0f}% RDI)\n"
    )

    messages = [
        {"role": "system", "content": _DAILY_SUMMARY_SYSTEM},
        {"role": "user", "content": context},
    ]

    response = await llm_client.complete("mini", messages)
    log.info("daily_summary_formatted", tokens_used=response.tokens_used)
    return response.content.strip()


# ---------------------------------------------------------------------------
# Public: weekly_trend
# ---------------------------------------------------------------------------


async def weekly_trend(
    user_id: int,
    db: AsyncSession,
    llm_client: "LLMClient",
) -> str:
    """
    Aggregate ``MealNutrient`` records over the preceding 7 calendar days
    (relative to today UTC) and return a formatted weekly trend report.

    Args:
        user_id:    Internal user PK.
        db:         Active async DB session.
        llm_client: Shared ``LLMClient`` instance.

    Returns:
        A formatted weekly trend string.

    Requirements: 7.3
    """
    today_utc = datetime.now(timezone.utc).date()
    log = logger.bind(user_id=user_id, anchor_date=str(today_utc))

    # Build per-day aggregations for the 7-day window
    day_lines: list[str] = []
    for offset in range(6, -1, -1):  # 6 days ago … today
        day = today_utc - timedelta(days=offset)
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
        totals = await _aggregate_nutrients(user_id, day_start, day_end, db)
        pct = totals.pct_rdi()
        day_lines.append(
            f"{day.isoformat()}: "
            f"Protein {pct['protein_g']:.0f}% | "
            f"Iron {pct['iron_mg']:.0f}% | "
            f"Calcium {pct['calcium_mg']:.0f}% | "
            f"Folate {pct['folate_mcg']:.0f}% | "
            f"Fiber {pct['fiber_g']:.0f}%"
        )

    weekly_context = "Daily % RDI over the past 7 days:\n" + "\n".join(day_lines)

    messages = [
        {"role": "system", "content": _WEEKLY_TREND_SYSTEM},
        {"role": "user", "content": weekly_context},
    ]

    response = await llm_client.complete("mini", messages)
    log.info("weekly_trend_formatted", tokens_used=response.tokens_used)
    return response.content.strip()


# ---------------------------------------------------------------------------
# Public: check_deficiency_alert
# ---------------------------------------------------------------------------


async def check_deficiency_alert(
    user_id: int,
    db: AsyncSession,
    llm_client: "LLMClient",
    bot: "Bot",
) -> None:
    """
    Detect nutrients below 75 % RDI for 3+ consecutive calendar days ending
    today and send a proactive Telegram message if any are found.

    The alert includes the deficient nutrient name and at least one food or
    supplement recommendation, personalised by the user's food preference.

    Args:
        user_id:    Internal user PK.
        db:         Active async DB session.
        llm_client: Shared ``LLMClient`` instance.
        bot:        The ``telegram.Bot`` instance used to send the alert.

    Requirements: 7.4
    """
    today_utc = datetime.now(timezone.utc).date()
    log = logger.bind(user_id=user_id)

    # ── Retrieve the user for telegram_user_id and food_preference ─────────
    user_result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = user_result.scalar_one_or_none()
    if user is None:
        log.warning("deficiency_check_user_not_found")
        return

    # ── Check each of the last DEFICIENCY_DAYS_THRESHOLD days ──────────────
    # Collect per-day deficient nutrient sets
    daily_deficient: list[set[str]] = []
    for offset in range(DEFICIENCY_DAYS_THRESHOLD - 1, -1, -1):
        day = today_utc - timedelta(days=offset)
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
        totals = await _aggregate_nutrients(user_id, day_start, day_end, db)
        daily_deficient.append(set(totals.deficient_nutrients()))

    # A nutrient qualifies for an alert only if it was deficient on EVERY
    # one of the checked days (i.e. the intersection of all per-day sets).
    if not daily_deficient:
        return

    persistently_deficient: set[str] = daily_deficient[0]
    for day_set in daily_deficient[1:]:
        persistently_deficient &= day_set

    if not persistently_deficient:
        log.debug("no_persistent_deficiency_detected")
        return

    log.info(
        "deficiency_alert_triggered",
        deficient_nutrients=sorted(persistently_deficient),
        consecutive_days=DEFICIENCY_DAYS_THRESHOLD,
    )

    # ── Generate one recommendation per deficient nutrient ─────────────────
    food_pref_str = (
        user.food_preference.value if user.food_preference else "no specific preference"
    )
    _NUTRIENT_LABELS = {
        "protein_g": "Protein",
        "iron_mg": "Iron",
        "calcium_mg": "Calcium",
        "folate_mcg": "Folate",
        "fiber_g": "Fiber",
    }

    alert_parts: list[str] = [
        "🌟 *Nutrition check-in*\n\n"
        f"For the past {DEFICIENCY_DAYS_THRESHOLD} days, the following "
        f"nutrient{'s' if len(persistently_deficient) > 1 else ''} "
        f"{'have' if len(persistently_deficient) > 1 else 'has'} been "
        "below the recommended daily intake:"
    ]

    for nutrient_key in sorted(persistently_deficient):
        label = _NUTRIENT_LABELS.get(nutrient_key, nutrient_key)
        system_prompt = _DEFICIENCY_RECOMMENDATION_SYSTEM.format(
            food_preference=food_pref_str
        )
        rec_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"The user is deficient in {label}. "
                    "Suggest one pregnancy-safe food or supplement to help."
                ),
            },
        ]
        rec_response = await llm_client.complete("mini", rec_messages)
        recommendation = rec_response.content.strip()
        alert_parts.append(f"\n• *{label}*: {recommendation}")

    alert_parts.append(
        "\n\nKeep logging your meals so I can continue tracking your nutrition. "
        "If you have concerns, speak with your healthcare provider."
    )

    alert_text = "\n".join(alert_parts)

    # ── Send the proactive Telegram message ─────────────────────────────────
    try:
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=alert_text,
            parse_mode="Markdown",
        )
        log.info(
            "deficiency_alert_sent",
            deficient_count=len(persistently_deficient),
        )
    except Exception:
        log.exception("deficiency_alert_send_failed")


# ---------------------------------------------------------------------------
# Public: get_meal_suggestions
# ---------------------------------------------------------------------------


async def get_meal_suggestions(
    user_id: int,
    db: AsyncSession,
    llm_client: "LLMClient",
) -> str:
    """
    Generate personalised meal suggestions grounded in the Knowledge_Base,
    filtered by the user's stored preferences and allergies.

    If the Knowledge_Base is unavailable (retriever raises an exception or
    returns no chunks), the function falls back to suggestions based on the
    user's confirmed meal history and stored preferences alone, notifying the
    user of the temporary knowledge-base unavailability (Req 7.7).

    Args:
        user_id:    Internal user PK.
        db:         Active async DB session.
        llm_client: Shared ``LLMClient`` instance.

    Returns:
        A formatted meal suggestion string.

    Requirements: 7.5, 7.6, 7.7
    """
    from app.components.preference_engine import get_active_preferences
    from app.knowledge.retriever import retrieve
    from app.models.knowledge_document import KnowledgeCategory

    log = logger.bind(user_id=user_id)

    # ── Retrieve confirmed preferences (Req 7.5) ────────────────────────────
    preferences = await get_active_preferences(user_id, db)

    pref_lines: list[str] = []
    for pref in preferences:
        pref_lines.append(f"- {pref.preference_type.value}: {pref.food_item}")

    pref_block = (
        "User dietary preferences and restrictions:\n" + "\n".join(pref_lines)
        if pref_lines
        else "No specific dietary preferences stored."
    )

    # ── Retrieve nutrition knowledge chunks (Req 7.6) ───────────────────────
    knowledge_available = True
    knowledge_context = ""

    try:
        chunks = await retrieve(
            query_text="pregnancy nutrition balanced meal suggestions",
            db=db,
            llm_client=llm_client,
            top_k=5,
            category=KnowledgeCategory.nutrition,
        )
        if chunks:
            knowledge_context = "\n\n".join(c.content for c in chunks)
        else:
            knowledge_available = False
            log.info("meal_suggestions_kb_empty")
    except Exception:
        knowledge_available = False
        log.warning("meal_suggestions_kb_unavailable")

    # ── Compose suggestions (Req 7.7 fallback if KB unavailable) ────────────
    if knowledge_available and knowledge_context:
        system_prompt = _MEAL_SUGGESTION_SYSTEM
        user_content = (
            f"Knowledge base context:\n{knowledge_context}\n\n"
            f"{pref_block}\n\n"
            "Please suggest 3 balanced, pregnancy-safe meals."
        )
    else:
        # Fallback: use meal history without Knowledge_Base (Req 7.7)
        system_prompt = _MEAL_SUGGESTION_NO_KB_SYSTEM
        recent_meals = await _get_recent_meal_items(user_id, db, days=7)
        meal_history = (
            "Recent foods logged by the user:\n" + "\n".join(f"- {m}" for m in recent_meals)
            if recent_meals
            else "No recent meal history available."
        )
        user_content = f"{meal_history}\n\n{pref_block}\n\nPlease suggest 3 balanced meals."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response = await llm_client.complete("mini", messages)
    log.info(
        "meal_suggestions_generated",
        knowledge_available=knowledge_available,
        tokens_used=response.tokens_used,
    )
    return response.content.strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _aggregate_nutrients(
    user_id: int,
    start: datetime,
    end: datetime,
    db: AsyncSession,
) -> _NutrientTotals:
    """
    Sum all ``MealNutrient`` rows linked to *user_id* within [start, end].

    Joins ``meal_nutrients`` through ``meals`` to filter on
    ``meals.user_id`` and ``meals.logged_at``.

    Args:
        user_id: Internal user PK.
        start:   Inclusive window start (UTC).
        end:     Inclusive window end (UTC).
        db:      Active async DB session.

    Returns:
        A :class:`_NutrientTotals` instance with accumulated sums.
    """
    stmt = (
        select(MealNutrient)
        .join(Meal, MealNutrient.meal_id == Meal.id)
        .where(
            Meal.user_id == user_id,
            Meal.logged_at >= start,
            Meal.logged_at <= end,
        )
    )
    result = await db.execute(stmt)
    records: list[MealNutrient] = list(result.scalars().all())

    totals = _NutrientTotals()
    for record in records:
        totals.add(record)

    return totals


async def _get_recent_meal_items(
    user_id: int,
    db: AsyncSession,
    days: int = 7,
) -> list[str]:
    """
    Fetch distinct food names from ``meal_items`` for the last *days* days.

    Used as fallback context when the Knowledge_Base is unavailable (Req 7.7).

    Args:
        user_id: Internal user PK.
        db:      Active async DB session.
        days:    Look-back window in calendar days.

    Returns:
        A deduplicated list of food name strings (may be empty).
    """
    from app.models.meal import MealItem

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = (
        select(MealItem.food_name)
        .join(Meal, MealItem.meal_id == Meal.id)
        .where(
            Meal.user_id == user_id,
            Meal.logged_at >= cutoff,
        )
        .distinct()
        .limit(30)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
