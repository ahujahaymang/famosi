"""Pydantic extraction schemas for symptom logging."""

from pydantic import BaseModel, Field


class SymptomExtraction(BaseModel):
    """A symptom extracted from a user message.

    Timestamp is intentionally omitted — it is auto-assigned at confirmation time.
    """

    symptom_name: str
    severity: int = Field(..., ge=1, le=10, description="Severity on a 1–10 scale")
    frequency: int = Field(..., ge=1, le=99, description="How many times per day/week the symptom occurs")
