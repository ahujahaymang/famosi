"""Pydantic extraction schema for appointment scheduling from natural language."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class AppointmentExtraction(BaseModel):
    """
    Extracted from messages like:
      "My scan is tomorrow at 10:30am"
      "Schedule an OB visit for June 25 at 11am"
      "Bloodwork appointment next Monday at 9"
    """
    appointment_type: str = Field(
        description=(
            "Type of appointment. One of: ob_visit, ultrasound, bloodwork. "
            "Infer from context: scan/ultrasound → 'ultrasound', "
            "blood/bloodwork → 'bloodwork', otherwise → 'ob_visit'."
        )
    )
    datetime_str: str = Field(
        description=(
            "Date and time in format YYYY-MM-DD HH:MM (24-hour, UTC). "
            "Interpret relative dates (tomorrow, next Friday) relative to today. "
            "Convert 10:30am → 10:30, 2pm → 14:00."
        )
    )
    location: Optional[str] = Field(
        default=None,
        description="Location or clinic name if mentioned, otherwise null."
    )
    notes: Optional[str] = Field(
        default=None,
        description="Any additional notes the user mentioned, otherwise null."
    )
