"""
Doctor Visit Assistant — pre-appointment summary generation with PDF export and S3 delivery.

Handles:
  - Validating that the requested appointment date is in the future (Req 9.6)
  - Fetching all confirmed records since the user's last appointment anchor
    where visibility_level IN ('private', 'doctor_shared') (Req 9.4)
  - Composing a 6-section structured summary via the Reasoning tier (Req 9.2):
      Symptoms | Nutrition | Exercise | Medications | Questions | Discussion Topics
  - Identifying discussion topics: symptom types logged 3+ times OR nutrients
    below 75% RDI for 3+ days during the period (Req 9.2)
  - Generating a PDF from an HTML template via WeasyPrint (Req 9.3)
  - Uploading the PDF to S3 and returning a 7-day pre-signed URL (Req 9.3)
  - Updating user.last_appointment_anchor to the appointment date (Req 9.5)
  - Including a "no data" note when no records exist for the period (Req 9.1)

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""

from __future__ import annotations

import asyncio
import io
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.doctor_question import DoctorQuestion
from app.models.exercise import Exercise
from app.models.meal import Meal, MealNutrient, VisibilityLevel
from app.models.medication import Medication
from app.models.symptom import Symptom
from app.models.user import User

if TYPE_CHECKING:
    from app.core.llm_client import LLMClient

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Nutrition RDI constants (mirrored from nutrition_assistant for self-containment)
# ---------------------------------------------------------------------------

_RDI = {
    "protein_g": Decimal("71.0"),
    "iron_mg": Decimal("27.0"),
    "calcium_mg": Decimal("1000.0"),
    "folate_mcg": Decimal("600.0"),
    "fiber_g": Decimal("28.0"),
}

_DEFICIENCY_THRESHOLD = Decimal("0.75")  # below 75% of RDI

# ---------------------------------------------------------------------------
# LLM system prompts
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """\
You are a clinical documentation assistant for a pregnancy tracking app.
Given structured health data for a pregnant user covering the period since
their last doctor appointment, generate a comprehensive pre-appointment
summary formatted for a healthcare provider visit.

Structure the summary with exactly these 6 sections:

## 1. Symptoms
List each symptom with name, severity (out of 10), frequency (per day), and
dates logged. Group by symptom name. Note any concerning patterns.

## 2. Nutrition
Summarise nutritional intake trends. Report per-nutrient totals and
percentage of recommended daily intake (RDI) averages for the period.
Flag any persistent deficiencies (below 75% RDI).

## 3. Exercise
List exercise activities with type, duration, and dates.
Note frequency and any changes in activity level.

## 4. Medications
List all logged medications with name, dose, and dates.

## 5. Questions for the Doctor
List all questions the user has flagged for this appointment.

## 6. Suggested Discussion Topics
Based on the data, list topics that warrant discussion:
- Symptom types logged 3 or more times
- Nutrients below 75% RDI for 3 or more days

Keep the tone clinical and factual. Do not diagnose or prescribe.
The target audience is the healthcare provider, not the patient.
Length: comprehensive but concise — under 600 words total.
"""

_NO_DATA_SUMMARY_SYSTEM = """\
You are a clinical documentation assistant for a pregnancy tracking app.
The user has no logged health data for the period since their last appointment.
Generate a brief, professional pre-appointment summary noting that no data
was logged for the period and encouraging the patient to begin logging for
future appointments. Keep the tone warm and non-judgmental. Under 100 words.
"""

# ---------------------------------------------------------------------------
# HTML template for PDF generation
# ---------------------------------------------------------------------------

_PDF_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Pre-Appointment Summary — {appointment_date}</title>
  <style>
    body {{
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      font-size: 12px;
      line-height: 1.6;
      color: #2c2c2c;
      margin: 40px 50px;
    }}
    h1 {{
      font-size: 18px;
      color: #7B3F7D;
      border-bottom: 2px solid #7B3F7D;
      padding-bottom: 6px;
      margin-bottom: 4px;
    }}
    h2 {{
      font-size: 14px;
      color: #7B3F7D;
      margin-top: 20px;
      margin-bottom: 4px;
      border-left: 4px solid #D4A0D5;
      padding-left: 8px;
    }}
    .meta {{
      font-size: 11px;
      color: #666;
      margin-bottom: 20px;
    }}
    pre, p {{
      white-space: pre-wrap;
      word-wrap: break-word;
    }}
    .footer {{
      margin-top: 40px;
      font-size: 10px;
      color: #999;
      border-top: 1px solid #ddd;
      padding-top: 6px;
    }}
  </style>
</head>
<body>
  <h1>Pre-Appointment Health Summary</h1>
  <div class="meta">
    <strong>Appointment date:</strong> {appointment_date}<br/>
    <strong>Summary period:</strong> {period_start} to {period_end}<br/>
    <strong>Generated:</strong> {generated_at}
  </div>
  <div class="content">
    {summary_html}
  </div>
  <div class="footer">
    Generated by Famosi &mdash; For informational purposes only.
    Always consult a qualified healthcare provider.
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AppointmentDateInPastError(ValueError):
    """Raised when the provided appointment date is not in the future (Req 9.6)."""


async def generate_summary(
    user_id: int,
    appointment_date: date,
    db: AsyncSession,
    llm_client: "LLMClient",
    s3_client,
) -> str:
    """
    Generate a pre-appointment health summary PDF and return a pre-signed S3 URL.

    Steps
    -----
    1. Validate *appointment_date* is in the future; raise
       :class:`AppointmentDateInPastError` if it is in the past (Req 9.6).
    2. Fetch all relevant records since ``user.last_appointment_anchor``
       (or onboarding date) with ``visibility_level IN ('private', 'doctor_shared')``
       and ``user_id`` matching the requesting user (Req 9.4).
    3. Compose a 6-section structured summary via the Reasoning LLM tier
       (Req 9.2).
    4. Generate a PDF via WeasyPrint from an HTML template.
    5. Upload the PDF to ``s3://{bucket}/{user_id}/{date}.pdf`` and return a
       pre-signed URL with 7-day expiry (Req 9.3).
    6. Update ``user.last_appointment_anchor = appointment_date`` (Req 9.5).

    Args:
        user_id:          Internal user PK.
        appointment_date: The upcoming appointment date (must be in the future).
        db:               Active async SQLAlchemy session.
        llm_client:       Shared :class:`~app.core.llm_client.LLMClient` instance.
        s3_client:        A boto3 S3 client (synchronous; called in thread pool).

    Returns:
        A pre-signed HTTPS URL string for downloading the generated PDF.

    Raises:
        AppointmentDateInPastError: If *appointment_date* is not in the future.
        LookupError: If no user record is found for *user_id*.

    Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
    """
    log = logger.bind(user_id=user_id, appointment_date=str(appointment_date))

    # ── Step 1: Validate appointment date is in the future (Req 9.6) ─────────
    today_utc = datetime.now(timezone.utc).date()
    if appointment_date <= today_utc:
        log.info("appointment_date_in_past_rejected")
        raise AppointmentDateInPastError(
            f"Appointment date {appointment_date.isoformat()} is not in the future. "
            "Please provide an upcoming appointment date."
        )

    # ── Load the user record ──────────────────────────────────────────────────
    user_result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = user_result.scalar_one_or_none()
    if user is None:
        raise LookupError(f"No user found with id={user_id}")

    # ── Determine the period start anchor ─────────────────────────────────────
    # Use last_appointment_anchor if set; otherwise fall back to a distant past
    # date (returns all records, effectively from the beginning of time).
    if user.last_appointment_anchor is not None:
        period_start: datetime = user.last_appointment_anchor
    else:
        # No prior anchor: include all confirmed records (Req 9.1)
        period_start = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    period_end = datetime.now(timezone.utc)

    log.info(
        "generating_summary",
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
    )

    # ── Step 2: Fetch all relevant records (Req 9.4) ──────────────────────────
    _visible = [VisibilityLevel.private, VisibilityLevel.doctor_shared]

    symptoms = await _fetch_symptoms(user_id, period_start, period_end, _visible, db)
    meals, nutrients_by_meal = await _fetch_meals_and_nutrients(
        user_id, period_start, period_end, _visible, db
    )
    exercises = await _fetch_exercises(user_id, period_start, period_end, _visible, db)
    medications = await _fetch_medications(user_id, period_start, period_end, _visible, db)
    questions = await _fetch_questions(user_id, period_start, period_end, _visible, db)

    has_any_data = any([symptoms, meals, exercises, medications, questions])
    log.info(
        "records_fetched",
        symptoms=len(symptoms),
        meals=len(meals),
        exercises=len(exercises),
        medications=len(medications),
        questions=len(questions),
    )

    # ── Step 3: Compose summary via Reasoning LLM (Req 9.2) ──────────────────
    if not has_any_data:
        # Req 9.1 — include a note stating no logged data is available
        summary_text = await _compose_no_data_summary(llm_client)
    else:
        context_block = _build_context_block(
            symptoms=symptoms,
            meals=meals,
            nutrients_by_meal=nutrients_by_meal,
            exercises=exercises,
            medications=medications,
            questions=questions,
            period_start=period_start,
            period_end=period_end,
        )
        summary_text = await _compose_summary(context_block, llm_client)

    log.info("summary_composed", tokens=len(summary_text))

    # ── Step 4: Generate PDF ──────────────────────────────────────────────────
    pdf_bytes = _render_pdf(
        summary_text=summary_text,
        appointment_date=appointment_date,
        period_start=period_start,
        period_end=period_end,
    )
    log.info("pdf_rendered", size_bytes=len(pdf_bytes))

    # ── Step 5: Upload to S3 and get pre-signed URL (Req 9.3) ─────────────────
    s3_key = f"{user_id}/{appointment_date.isoformat()}.pdf"
    bucket = settings.s3_bucket_summaries
    presigned_url = await _upload_and_presign(
        s3_client=s3_client,
        bucket=bucket,
        key=s3_key,
        pdf_bytes=pdf_bytes,
    )
    log.info("pdf_uploaded_to_s3", bucket=bucket, key=s3_key)

    # ── Step 6: Update last_appointment_anchor (Req 9.5) ──────────────────────
    anchor_dt = datetime.combine(appointment_date, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(last_appointment_anchor=anchor_dt)
    )
    await db.flush()
    log.info("appointment_anchor_updated", new_anchor=anchor_dt.isoformat())

    return presigned_url


# ---------------------------------------------------------------------------
# Record fetchers
# ---------------------------------------------------------------------------


async def _fetch_symptoms(
    user_id: int,
    start: datetime,
    end: datetime,
    visible: list[VisibilityLevel],
    db: AsyncSession,
) -> list[Symptom]:
    stmt = (
        select(Symptom)
        .where(
            Symptom.user_id == user_id,
            Symptom.visibility_level.in_(visible),
            Symptom.logged_at > start,
            Symptom.logged_at <= end,
        )
        .order_by(Symptom.logged_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _fetch_meals_and_nutrients(
    user_id: int,
    start: datetime,
    end: datetime,
    visible: list[VisibilityLevel],
    db: AsyncSession,
) -> tuple[list[Meal], dict[int, MealNutrient]]:
    """Return meals and a mapping of meal_id → MealNutrient for the period."""
    meal_stmt = (
        select(Meal)
        .where(
            Meal.user_id == user_id,
            Meal.visibility_level.in_(visible),
            Meal.logged_at > start,
            Meal.logged_at <= end,
        )
        .order_by(Meal.logged_at.asc())
    )
    meal_result = await db.execute(meal_stmt)
    meals: list[Meal] = list(meal_result.scalars().all())

    if not meals:
        return meals, {}

    meal_ids = [m.id for m in meals]
    nutrient_stmt = select(MealNutrient).where(MealNutrient.meal_id.in_(meal_ids))
    nutrient_result = await db.execute(nutrient_stmt)
    nutrients_by_meal: dict[int, MealNutrient] = {
        n.meal_id: n for n in nutrient_result.scalars().all()
    }
    return meals, nutrients_by_meal


async def _fetch_exercises(
    user_id: int,
    start: datetime,
    end: datetime,
    visible: list[VisibilityLevel],
    db: AsyncSession,
) -> list[Exercise]:
    stmt = (
        select(Exercise)
        .where(
            Exercise.user_id == user_id,
            Exercise.visibility_level.in_(visible),
            Exercise.logged_at > start,
            Exercise.logged_at <= end,
        )
        .order_by(Exercise.logged_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _fetch_medications(
    user_id: int,
    start: datetime,
    end: datetime,
    visible: list[VisibilityLevel],
    db: AsyncSession,
) -> list[Medication]:
    stmt = (
        select(Medication)
        .where(
            Medication.user_id == user_id,
            Medication.visibility_level.in_(visible),
            Medication.logged_at > start,
            Medication.logged_at <= end,
        )
        .order_by(Medication.logged_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _fetch_questions(
    user_id: int,
    start: datetime,
    end: datetime,
    visible: list[VisibilityLevel],
    db: AsyncSession,
) -> list[DoctorQuestion]:
    stmt = (
        select(DoctorQuestion)
        .where(
            DoctorQuestion.user_id == user_id,
            DoctorQuestion.visibility_level.in_(visible),
            DoctorQuestion.doctor_visit_tagged.is_(True),
            DoctorQuestion.used_in_summary.is_(False),
            DoctorQuestion.logged_at > start,
            DoctorQuestion.logged_at <= end,
        )
        .order_by(DoctorQuestion.logged_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Context block builder
# ---------------------------------------------------------------------------


def _build_context_block(
    symptoms: list[Symptom],
    meals: list[Meal],
    nutrients_by_meal: dict[int, MealNutrient],
    exercises: list[Exercise],
    medications: list[Medication],
    questions: list[DoctorQuestion],
    period_start: datetime,
    period_end: datetime,
) -> str:
    """
    Build a structured plain-text context block for the LLM.

    Includes raw data and pre-computed discussion-topic signals (Req 9.2):
    - Symptom types logged 3+ times
    - Nutrients below 75% RDI for 3+ days
    """
    lines: list[str] = []

    lines.append(
        f"Period: {period_start.date().isoformat()} to {period_end.date().isoformat()}\n"
    )

    # ── Symptoms ──────────────────────────────────────────────────────────────
    lines.append("=== SYMPTOMS ===")
    if symptoms:
        symptom_name_counter: Counter = Counter(s.symptom_name for s in symptoms)
        for s in symptoms:
            lines.append(
                f"  {s.logged_at.date().isoformat()} | {s.symptom_name} | "
                f"severity {s.severity}/10 | frequency {s.frequency}x/day"
            )
        # Flag symptom types logged 3+ times for discussion topics
        frequent_symptoms = [name for name, cnt in symptom_name_counter.items() if cnt >= 3]
        if frequent_symptoms:
            lines.append(
                "\nSymptom types logged 3+ times (discussion topic candidates): "
                + ", ".join(sorted(frequent_symptoms))
            )
    else:
        lines.append("  No symptoms logged for this period.")

    # ── Nutrition ─────────────────────────────────────────────────────────────
    lines.append("\n=== NUTRITION ===")
    if meals:
        # Aggregate per-day nutrient totals
        daily_totals: dict[date, dict[str, Decimal]] = defaultdict(
            lambda: {k: Decimal("0") for k in _RDI}
        )
        for meal in meals:
            nutrient = nutrients_by_meal.get(meal.id)
            if nutrient:
                day = meal.logged_at.date()
                daily_totals[day]["protein_g"] += nutrient.protein_g or Decimal("0")
                daily_totals[day]["iron_mg"] += nutrient.iron_mg or Decimal("0")
                daily_totals[day]["calcium_mg"] += nutrient.calcium_mg or Decimal("0")
                daily_totals[day]["folate_mcg"] += nutrient.folate_mcg or Decimal("0")
                daily_totals[day]["fiber_g"] += nutrient.fiber_g or Decimal("0")

        lines.append(f"  Total meals logged: {len(meals)}")
        if daily_totals:
            lines.append("  Daily nutrient summary (% RDI):")
            for day in sorted(daily_totals.keys()):
                totals = daily_totals[day]
                pcts = {
                    k: float(totals[k] / _RDI[k] * 100) for k in _RDI
                }
                lines.append(
                    f"    {day.isoformat()}: "
                    f"Protein {pcts['protein_g']:.0f}% | "
                    f"Iron {pcts['iron_mg']:.0f}% | "
                    f"Calcium {pcts['calcium_mg']:.0f}% | "
                    f"Folate {pcts['folate_mcg']:.0f}% | "
                    f"Fiber {pcts['fiber_g']:.0f}%"
                )

            # Identify nutrients deficient (<75% RDI) for 3+ days
            nutrient_deficient_days: dict[str, int] = {k: 0 for k in _RDI}
            for _day, totals in daily_totals.items():
                for nutrient_key in _RDI:
                    pct = totals[nutrient_key] / _RDI[nutrient_key]
                    if pct < _DEFICIENCY_THRESHOLD:
                        nutrient_deficient_days[nutrient_key] += 1

            discussion_nutrients = [
                k for k, days in nutrient_deficient_days.items() if days >= 3
            ]
            if discussion_nutrients:
                lines.append(
                    "\nNutrients below 75% RDI for 3+ days (discussion topic candidates): "
                    + ", ".join(sorted(discussion_nutrients))
                )
        else:
            lines.append("  No nutrient data available for logged meals.")
    else:
        lines.append("  No meals logged for this period.")

    # ── Exercise ──────────────────────────────────────────────────────────────
    lines.append("\n=== EXERCISE ===")
    if exercises:
        for ex in exercises:
            lines.append(
                f"  {ex.logged_at.date().isoformat()} | {ex.activity_type} | "
                f"{ex.duration_minutes} minutes"
            )
    else:
        lines.append("  No exercise logged for this period.")

    # ── Medications ───────────────────────────────────────────────────────────
    lines.append("\n=== MEDICATIONS ===")
    if medications:
        for med in medications:
            dose_str = f" | dose: {med.dose}" if med.dose else ""
            lines.append(
                f"  {med.logged_at.date().isoformat()} | {med.medication_name}{dose_str}"
            )
    else:
        lines.append("  No medications logged for this period.")

    # ── Doctor Questions ──────────────────────────────────────────────────────
    lines.append("\n=== QUESTIONS FOR THE DOCTOR ===")
    if questions:
        for q in questions:
            lines.append(
                f"  {q.logged_at.date().isoformat()} | {q.question_text}"
            )
    else:
        lines.append("  No questions logged for this period.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM composition helpers
# ---------------------------------------------------------------------------


async def _compose_summary(context_block: str, llm_client: "LLMClient") -> str:
    """Call the Reasoning LLM tier to compose the 6-section summary."""
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": (
                "Please generate the pre-appointment summary based on the "
                "following health data:\n\n" + context_block
            ),
        },
    ]
    response = await llm_client.complete("reasoning", messages)
    logger.debug("summary_llm_complete", tokens_used=response.tokens_used)
    return response.content.strip()


async def _compose_no_data_summary(llm_client: "LLMClient") -> str:
    """Compose the no-data summary note (Req 9.1)."""
    messages = [
        {"role": "system", "content": _NO_DATA_SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": (
                "The user has no logged health data for the period since "
                "their last appointment. Generate the summary noting this."
            ),
        },
    ]
    response = await llm_client.complete("reasoning", messages)
    return response.content.strip()


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------


def _render_pdf(
    summary_text: str,
    appointment_date: date,
    period_start: datetime,
    period_end: datetime,
) -> bytes:
    """
    Render the summary text to a PDF byte string using WeasyPrint.

    The summary text (Markdown-ish) is lightly converted to HTML before
    being wrapped in the :data:`_PDF_HTML_TEMPLATE`.

    Returns:
        Raw PDF bytes.
    """
    from weasyprint import HTML  # optional dep; imported lazily

    # Convert simple Markdown headings and bullet points to HTML
    summary_html = _markdown_to_html(summary_text)

    html_content = _PDF_HTML_TEMPLATE.format(
        appointment_date=appointment_date.isoformat(),
        period_start=period_start.date().isoformat(),
        period_end=period_end.date().isoformat(),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        summary_html=summary_html,
    )

    pdf_buffer = io.BytesIO()
    HTML(string=html_content).write_pdf(pdf_buffer)
    return pdf_buffer.getvalue()


def _markdown_to_html(text: str) -> str:
    """
    Convert a subset of Markdown to HTML suitable for the PDF template.

    Handles:
    - ``## Heading`` → ``<h2>Heading</h2>``
    - ``# Heading``  → ``<h2>Heading</h2>``  (treat as same level in PDF)
    - ``- item`` / ``* item`` → ``<li>item</li>`` wrapped in ``<ul>``
    - Blank lines → ``<br/>``
    - Everything else → ``<p>line</p>``
    """
    html_lines: list[str] = []
    in_list = False

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line.startswith("## ") or line.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            heading = line.lstrip("#").strip()
            html_lines.append(f"<h2>{_escape_html(heading)}</h2>")

        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            item = line[2:].strip()
            html_lines.append(f"  <li>{_escape_html(item)}</li>")

        elif line == "":
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br/>")

        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{_escape_html(line)}</p>")

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# S3 upload and pre-signed URL
# ---------------------------------------------------------------------------


async def _upload_and_presign(
    s3_client,
    bucket: str,
    key: str,
    pdf_bytes: bytes,
    expiry_seconds: int = 7 * 24 * 3600,  # 7 days
) -> str:
    """
    Upload *pdf_bytes* to S3 at ``{bucket}/{key}`` and return a 7-day
    pre-signed URL.

    The boto3 S3 client is synchronous; we run both operations in a thread
    pool so the event loop is not blocked.

    Args:
        s3_client:       A boto3 ``S3Client`` instance.
        bucket:          Target S3 bucket name.
        key:             Object key within the bucket.
        pdf_bytes:       Raw PDF content to upload.
        expiry_seconds:  Pre-signed URL TTL in seconds (default 7 days).

    Returns:
        A pre-signed HTTPS URL string.
    """
    loop = asyncio.get_event_loop()

    # Upload the object
    await loop.run_in_executor(
        None,
        lambda: s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        ),
    )

    # Generate pre-signed URL
    presigned_url: str = await loop.run_in_executor(
        None,
        lambda: s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiry_seconds,
        ),
    )
    return presigned_url
