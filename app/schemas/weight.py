"""Pydantic extraction schemas for weight logging."""

from typing import Literal

from pydantic import BaseModel


class WeightExtraction(BaseModel):
    """A weight measurement extracted from a user message."""

    value: float
    unit: Literal["kg", "lbs"]
