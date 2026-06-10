"""Pydantic extraction schemas for water intake logging."""

from typing import Literal

from pydantic import BaseModel


class WaterExtraction(BaseModel):
    """A water intake entry extracted from a user message."""

    volume: float
    unit: Literal["ml", "oz"]
