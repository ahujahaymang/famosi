"""
SQLAlchemy ORM models for Famosi.

Import every model here so that:
  1. Alembic autogenerate picks up all tables when it imports `app.models`.
  2. Application code can do `from app.models import User, Meal, ...` cleanly.
"""

from app.models.base import Base, TimestampMixin
from app.models.family_unit import FamilyUnit
from app.models.user import User, UserRole, FoodPreference
from app.models.consent import ConsentRecord
from app.models.meal import Meal, MealItem, MealNutrient, VisibilityLevel
from app.models.symptom import Symptom
from app.models.exercise import Exercise
from app.models.medication import Medication
from app.models.weight_log import WeightLog
from app.models.water_log import WaterLog
from app.models.doctor_question import DoctorQuestion
from app.models.preference import Preference, PreferenceType
from app.models.appointment import Appointment, AppointmentType
from app.models.reminder import Reminder, ReminderType
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.knowledge_document import KnowledgeDocument, KnowledgeCategory
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.request_log import RequestLog, IntentType

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    # Enums
    "UserRole",
    "FoodPreference",
    "VisibilityLevel",
    "PreferenceType",
    "AppointmentType",
    "ReminderType",
    "SubscriptionStatus",
    "KnowledgeCategory",
    "IntentType",
    # Models
    "FamilyUnit",
    "User",
    "ConsentRecord",
    "Meal",
    "MealItem",
    "MealNutrient",
    "Symptom",
    "Exercise",
    "Medication",
    "WeightLog",
    "WaterLog",
    "DoctorQuestion",
    "Preference",
    "Appointment",
    "Reminder",
    "Subscription",
    "KnowledgeDocument",
    "KnowledgeChunk",
    "RequestLog",
]
