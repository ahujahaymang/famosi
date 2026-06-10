"""Pydantic extraction schemas for exercise logging."""

from pydantic import BaseModel


class ExerciseExtraction(BaseModel):
    """An exercise session extracted from a user message."""

    activity_type: str
    duration_minutes: int
