"""Pydantic extraction schemas for food preference logging."""

from typing import Literal

from pydantic import BaseModel


class PreferenceExtraction(BaseModel):
    """A food preference, allergy, or dietary restriction extracted from a user message.

    preference_type aligns with the PreferenceType SQL enum in app/models/preference.py.
    """

    preference_type: Literal["like", "dislike", "allergy", "dietary"]
    food_item: str
