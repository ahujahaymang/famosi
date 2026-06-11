"""
Extraction pipeline for structured record parsing from user messages.

Given a record type and a user's free-text message, this module calls the
LLM (nano tier) with the appropriate Pydantic schema embedded in the system
prompt, then validates the response against that schema.

Return contract:
  - BaseModel subclass (e.g. MealExtraction) on full success
  - MissingFields on partial parse where required fields are absent
  - None on hard validation failure (bad JSON, wrong types, out-of-range values)

Requirements: 4.2, 4.8
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal, Type

import structlog
from pydantic import BaseModel, ValidationError

from app.core.llm_client import LLMClient
from app.schemas.appointment import AppointmentExtraction
from app.schemas.exercise import ExerciseExtraction
from app.schemas.meal import MealExtraction
from app.schemas.medication import MedicationExtraction
from app.schemas.preference import PreferenceExtraction
from app.schemas.question import DoctorQuestionExtraction
from app.schemas.reminder import ReminderExtraction
from app.schemas.symptom import SymptomExtraction
from app.schemas.water import WaterExtraction
from app.schemas.weight import WeightExtraction

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Supported record types
# ---------------------------------------------------------------------------

RecordType = Literal[
    "meal",
    "symptom",
    "exercise",
    "medication",
    "weight",
    "water",
    "question",
    "preference",
    "appointment",
    "reminder",
]

# Map each record type to its Pydantic extraction schema class
_SCHEMA_MAP: dict[str, Type[BaseModel]] = {
    "meal": MealExtraction,
    "symptom": SymptomExtraction,
    "exercise": ExerciseExtraction,
    "medication": MedicationExtraction,
    "weight": WeightExtraction,
    "water": WaterExtraction,
    "question": DoctorQuestionExtraction,
    "preference": PreferenceExtraction,
    "appointment": AppointmentExtraction,
    "reminder": ReminderExtraction,
}

# ---------------------------------------------------------------------------
# MissingFields result type
# ---------------------------------------------------------------------------


@dataclass
class MissingFields:
    """
    Returned when the LLM response is valid JSON but one or more required
    fields of the target schema are absent.

    Attributes:
        record_type: The record type that was being extracted.
        missing: List of field names that could not be resolved from the
                 user message and require conversational follow-up.
    """

    record_type: str
    missing: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """You are a precise data extraction assistant for a pregnancy health tracking app.

Extract the following structured information from the user's message.

Today's date (UTC): {today}

Target schema (JSON Schema):
{json_schema}

Rules:
- Respond with a single valid JSON object that matches the schema exactly.
- Do NOT include any explanation, markdown formatting, or extra text outside the JSON object.
- If a required field cannot be determined from the message, omit it from the response (do not guess or fabricate values).
- Use metric or stated units exactly as given by the user.
- All string values must be non-empty.
- For date/time fields: interpret relative terms (tomorrow, next Friday, this evening) relative to today's date shown above.
"""


def _build_system_prompt(schema_class: Type[BaseModel]) -> str:
    """Return a system prompt with the target schema's JSON Schema and today's date embedded."""
    from datetime import date as _date  # noqa: PLC0415
    json_schema = json.dumps(schema_class.model_json_schema(), indent=2)
    today = _date.today().strftime("%Y-%m-%d (%A)")
    return _SYSTEM_PROMPT_TEMPLATE.format(json_schema=json_schema, today=today)


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------


async def extract(
    record_type: str,
    user_message: str,
    llm_client: LLMClient,
) -> BaseModel | MissingFields | None:
    """
    Extract a structured record from a natural-language user message.

    Args:
        record_type: One of the supported record type strings (e.g. "meal",
                     "symptom", "exercise", etc.).
        user_message: The raw message text sent by the user.
        llm_client:  An initialised LLMClient instance used to call the nano
                     tier for structured extraction.

    Returns:
        - A validated Pydantic model instance (subclass of BaseModel) when
          all required fields are present and pass validation.
        - A MissingFields instance when the LLM response is valid JSON but
          one or more required fields of the schema are absent.
        - None when the LLM response cannot be parsed as JSON or fails Pydantic
          validation (e.g. out-of-range severity, wrong unit literal).

    Raises:
        ValueError: If record_type is not one of the supported types.
    """
    schema_class = _SCHEMA_MAP.get(record_type)
    if schema_class is None:
        raise ValueError(
            f"Unsupported record_type '{record_type}'. "
            f"Valid types: {', '.join(_SCHEMA_MAP)}."
        )

    log = logger.bind(record_type=record_type)

    system_prompt = _build_system_prompt(schema_class)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Call the nano tier with JSON mode so the model returns a JSON object
    llm_response = await llm_client.complete(
        "nano",
        messages,
        response_format={"type": "json_object"},
    )

    raw_content = llm_response.content.strip()

    # --- Step 1: Parse JSON ---
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        log.warning(
            "extractor_json_parse_failed",
            error=str(exc),
        )
        return None

    if not isinstance(parsed, dict):
        log.warning("extractor_unexpected_json_type", actual_type=type(parsed).__name__)
        return None

    # --- Step 2: Detect missing required fields before Pydantic validation ---
    missing_fields = _find_missing_required_fields(schema_class, parsed)
    if missing_fields:
        log.info(
            "extractor_missing_required_fields",
            missing=missing_fields,
        )
        return MissingFields(record_type=record_type, missing=missing_fields)

    # --- Step 3: Full Pydantic validation (type coercion + field constraints) ---
    try:
        model_instance = schema_class.model_validate(parsed)
    except ValidationError as exc:
        log.warning(
            "extractor_validation_failed",
            error_count=exc.error_count(),
        )
        return None

    log.debug("extractor_success", model=schema_class.__name__)
    return model_instance


# ---------------------------------------------------------------------------
# Required-field detection helper
# ---------------------------------------------------------------------------


def _find_missing_required_fields(
    schema_class: Type[BaseModel],
    data: dict,
) -> list[str]:
    """
    Return the names of required fields that are absent from *data*.

    A field is considered required when it has no default value and is not
    marked Optional (i.e. it is required in the Pydantic model's JSON Schema).

    This check is intentionally lenient about nested models — it only inspects
    the top-level fields of the schema.  Nested required fields (e.g. items
    inside MealExtraction) are handled implicitly by Pydantic validation.

    Args:
        schema_class: The Pydantic model class to inspect.
        data: The parsed JSON dict from the LLM response.

    Returns:
        A list of field names that are required but absent from data.
    """
    missing: list[str] = []

    for field_name, field_info in schema_class.model_fields.items():
        # A field is required if it has no default and is not Optional
        if field_info.is_required():
            if field_name not in data:
                missing.append(field_name)

    return missing
