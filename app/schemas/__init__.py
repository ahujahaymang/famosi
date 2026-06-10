"""Pydantic extraction schemas for LLM output validation."""

from app.schemas.exercise import ExerciseExtraction
from app.schemas.meal import MealExtraction, MealItemExtraction
from app.schemas.medication import MedicationExtraction
from app.schemas.preference import PreferenceExtraction
from app.schemas.question import DoctorQuestionExtraction
from app.schemas.symptom import SymptomExtraction
from app.schemas.water import WaterExtraction
from app.schemas.weight import WeightExtraction

__all__ = [
    "ExerciseExtraction",
    "MealExtraction",
    "MealItemExtraction",
    "MedicationExtraction",
    "PreferenceExtraction",
    "DoctorQuestionExtraction",
    "SymptomExtraction",
    "WaterExtraction",
    "WeightExtraction",
]
