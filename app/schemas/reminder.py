"""Pydantic extraction schema for reminder creation from natural language."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ReminderExtraction(BaseModel):
    """
    Extracted from messages like:
      "Remind me to take my iron tablet every morning at 9am"
      "Set a reminder for my prenatal vitamin at 8:30"
      "Alert me to drink water every 2 hours"
    """
    reminder_type: str = Field(
        description=(
            "Type of reminder. One of: vitamin, meal, water, exercise, appointment. "
            "Infer from context: vitamin/prenatal/iron/supplement/folate → 'vitamin', "
            "meal/eat/food → 'meal', water/hydrate/drink → 'water', "
            "exercise/walk/workout → 'exercise', appointment/scan/ultrasound → 'appointment'."
        )
    )
    datetime_str: str = Field(
        description=(
            "Date and time for the reminder in format YYYY-MM-DD HH:MM (24-hour). "
            "Interpret relative dates relative to today. "
            "If user says 'every morning at 9am', use tomorrow's date at 09:00. "
            "Convert 9am → 09:00, 8:30pm → 20:30."
        )
    )
    message: str = Field(
        description=(
            "Short, warm reminder message to send the user. "
            "E.g. 'Time to take your iron tablet! 💊' or 'Drink a glass of water 💧'. "
            "Keep it under 80 characters."
        )
    )
    is_recurring: Optional[bool] = Field(
        default=False,
        description="True if the user said 'every day', 'daily', 'every morning', etc."
    )
