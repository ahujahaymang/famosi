"""Pydantic extraction schemas for meal logging."""

from pydantic import BaseModel


class MealItemExtraction(BaseModel):
    """A single food item extracted from a meal log message."""

    food_name: str
    quantity: float
    unit: str


class MealExtraction(BaseModel):
    """A full meal log containing one or more food items."""

    items: list[MealItemExtraction]
