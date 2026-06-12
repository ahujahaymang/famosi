"""Pydantic extraction schemas for medication logging."""

from typing import Optional
from pydantic import BaseModel


class MedicationExtraction(BaseModel):
    """A medication log entry extracted from a user message."""

    medication_name: str
    dose: Optional[str] = None
