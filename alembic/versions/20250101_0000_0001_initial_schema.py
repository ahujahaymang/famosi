"""Initial schema — all tables, enums, indexes, and pgvector HNSW index.

Revision ID: 0001
Revises: None
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enable pgvector extension (idempotent)
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------
    # Enums
    # ------------------------------------------------------------------
    user_role = postgresql.ENUM(
        "mom", "partner", "admin", name="user_role", create_type=False
    )
    user_role.create(op.get_bind(), checkfirst=True)

    food_preference = postgresql.ENUM(
        "vegetarian", "vegan", "jain", "eggitarian", "non_vegetarian",
        name="food_preference", create_type=False,
    )
    food_preference.create(op.get_bind(), checkfirst=True)

    visibility_level = postgresql.ENUM(
        "private", "partner_shared", "doctor_shared",
        name="visibility_level", create_type=False,
    )
    visibility_level.create(op.get_bind(), checkfirst=True)

    preference_type = postgresql.ENUM(
        "like", "dislike", "allergy", "dietary",
        name="preference_type", create_type=False,
    )
    preference_type.create(op.get_bind(), checkfirst=True)

    appointment_type = postgresql.ENUM(
        "ob_visit", "ultrasound", "bloodwork",
        name="appointment_type", create_type=False,
    )
    appointment_type.create(op.get_bind(), checkfirst=True)

    reminder_type = postgresql.ENUM(
        "vitamin", "meal", "water", "exercise", "appointment",
        name="reminder_type", create_type=False,
    )
    reminder_type.create(op.get_bind(), checkfirst=True)

    subscription_status = postgresql.ENUM(
        "trial", "active", "inactive", "grace",
        name="subscription_status", create_type=False,
    )
    subscription_status.create(op.get_bind(), checkfirst=True)

    intent_type = postgresql.ENUM(
        "logging", "personal_data_query", "knowledge_question",
        "mixed_query", "unclassified",
        name="intent_type", create_type=False,
    )
    intent_type.create(op.get_bind(), checkfirst=True)

    knowledge_category = postgresql.ENUM(
        "nutrition", "symptoms", "exercise", "medications",
        "baby_development", "labor", "postpartum", "mental_health", "dad_support",
        name="knowledge_category", create_type=False,
    )
    knowledge_category.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # family_units
    # ------------------------------------------------------------------
    op.create_table(
        "family_units",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("role",
            postgresql.ENUM("mom", "partner", "admin", name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("lmp_date", sa.Date(), nullable=True),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("language", sa.String(10), server_default="en", nullable=False),
        sa.Column("first_pregnancy", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "food_preference",
            postgresql.ENUM(
                "vegetarian", "vegan", "jain", "eggitarian", "non_vegetarian",
                name="food_preference", create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("exercise_habit", sa.String(255), nullable=True),
        sa.Column("wake_time", sa.Time(), nullable=True),
        sa.Column("sleep_time", sa.Time(), nullable=True),
        sa.Column("onboarding_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_daily_fact_date", sa.Date(), nullable=True),
        sa.Column("last_milestone_week", sa.Integer(), nullable=True),
        sa.Column("last_appointment_anchor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("family_unit_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["family_unit_id"], ["family_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_index("idx_users_telegram_user_id", "users", ["telegram_user_id"])
    op.create_index("idx_users_family_unit_id", "users", ["family_unit_id"])

    # ------------------------------------------------------------------
    # consent_records
    # ------------------------------------------------------------------
    op.create_table(
        "consent_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(20), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_consent_user_id", "consent_records", ["user_id"])

    # ------------------------------------------------------------------
    # meals
    # ------------------------------------------------------------------
    op.create_table(
        "meals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_meals_user_id_logged_at", "meals", ["user_id", "logged_at"])
    op.create_index("idx_meals_visibility", "meals", ["user_id", "visibility_level"])

    # ------------------------------------------------------------------
    # meal_items
    # ------------------------------------------------------------------
    op.create_table(
        "meal_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meal_id", sa.BigInteger(), nullable=False),
        sa.Column("food_name", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Numeric(8, 2), nullable=True),
        sa.Column("unit", sa.String(50), nullable=True),
        sa.ForeignKeyConstraint(["meal_id"], ["meals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # meal_nutrients
    # ------------------------------------------------------------------
    op.create_table(
        "meal_nutrients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meal_id", sa.BigInteger(), nullable=False),
        sa.Column("protein_g", sa.Numeric(8, 2), nullable=True),
        sa.Column("iron_mg", sa.Numeric(8, 2), nullable=True),
        sa.Column("calcium_mg", sa.Numeric(8, 2), nullable=True),
        sa.Column("folate_mcg", sa.Numeric(8, 2), nullable=True),
        sa.Column("fiber_g", sa.Numeric(8, 2), nullable=True),
        sa.ForeignKeyConstraint(["meal_id"], ["meals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_meal_nutrients_meal_id", "meal_nutrients", ["meal_id"])

    # ------------------------------------------------------------------
    # symptoms
    # ------------------------------------------------------------------
    op.create_table(
        "symptoms",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("symptom_name", sa.String(255), nullable=False),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        sa.Column("frequency", sa.SmallInteger(), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("severity BETWEEN 1 AND 10", name="ck_symptoms_severity"),
        sa.CheckConstraint("frequency BETWEEN 1 AND 99", name="ck_symptoms_frequency"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_symptoms_user_id_logged_at", "symptoms", ["user_id", "logged_at"])

    # ------------------------------------------------------------------
    # exercises
    # ------------------------------------------------------------------
    op.create_table(
        "exercises",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("activity_type", sa.String(255), nullable=False),
        sa.Column("duration_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_exercises_user_id_logged_at", "exercises", ["user_id", "logged_at"])

    # ------------------------------------------------------------------
    # medications
    # ------------------------------------------------------------------
    op.create_table(
        "medications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("medication_name", sa.String(255), nullable=False),
        sa.Column("dose", sa.String(100), nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_medications_user_id_logged_at", "medications", ["user_id", "logged_at"])

    # ------------------------------------------------------------------
    # weight_logs
    # ------------------------------------------------------------------
    op.create_table(
        "weight_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(6, 2), nullable=False),
        sa.Column("unit", sa.String(10), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # water_logs
    # ------------------------------------------------------------------
    op.create_table(
        "water_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("volume", sa.Numeric(7, 2), nullable=False),
        sa.Column("unit", sa.String(10), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # doctor_questions
    # ------------------------------------------------------------------
    op.create_table(
        "doctor_questions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="private",
            nullable=False,
        ),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("doctor_visit_tagged", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("used_in_summary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_doctor_questions_user_id", "doctor_questions", ["user_id", "doctor_visit_tagged"])

    # ------------------------------------------------------------------
    # preferences
    # ------------------------------------------------------------------
    op.create_table(
        "preferences",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "preference_type",
            postgresql.ENUM("like", "dislike", "allergy", "dietary", name="preference_type", create_type=False),
            nullable=False,
        ),
        sa.Column("food_item", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "preference_type", "food_item", name="uq_preferences_user_type_item"),
    )
    op.create_index("idx_preferences_user_id", "preferences", ["user_id", "active"])

    # ------------------------------------------------------------------
    # appointments
    # ------------------------------------------------------------------
    op.create_table(
        "appointments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "visibility_level",
            postgresql.ENUM("private", "partner_shared", "doctor_shared", name="visibility_level", create_type=False),
            server_default="partner_shared",
            nullable=False,
        ),
        sa.Column(
            "appointment_type",
            postgresql.ENUM("ob_visit", "ultrasound", "bloodwork", name="appointment_type", create_type=False),
            nullable=False,
        ),
        sa.Column("appointment_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("cancelled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_appointments_user_id_at", "appointments", ["user_id", "appointment_at"])

    # ------------------------------------------------------------------
    # reminders
    # ------------------------------------------------------------------
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "reminder_type",
            postgresql.ENUM("vitamin", "meal", "water", "exercise", "appointment", name="reminder_type", create_type=False),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("appointment_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("failed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial index — only pending reminders
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_scheduled ON reminders(scheduled_at) "
        "WHERE active = TRUE AND delivered = FALSE AND failed = FALSE"
    )

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "subscription_status",
            postgresql.ENUM("trial", "active", "inactive", "grace", name="subscription_status", create_type=False),
            server_default="trial",
            nullable=False,
        ),
        sa.Column("payment_status", sa.String(50), server_default="none", nullable=False),
        sa.Column("payment_provider", sa.String(20), nullable=True),
        sa.Column("provider_customer_id", sa.String(255), nullable=True),
        sa.Column("provider_sub_id", sa.String(255), nullable=True),
        sa.Column("trial_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_retry_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_subscriptions_user_id"),
    )

    # ------------------------------------------------------------------
    # knowledge_documents
    # ------------------------------------------------------------------
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
                "nutrition", "symptoms", "exercise", "medications",
                "baby_development", "labor", "postpartum", "mental_health", "dad_support",
                name="knowledge_category", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("version", sa.String(50), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # knowledge_chunks  (requires pgvector extension)
    # ------------------------------------------------------------------
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # vector column — type registered by pgvector extension
        sa.Column("embedding", sa.Text(), nullable=True),  # placeholder; actual type set below
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Change the embedding column to the native vector type
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector(1536) USING NULL::vector(1536)")
    op.create_index("idx_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    # HNSW index for ANN cosine similarity search (Req 15.4)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # ------------------------------------------------------------------
    # request_logs
    # ------------------------------------------------------------------
    op.create_table(
        "request_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "intent",
            postgresql.ENUM(
                "logging", "personal_data_query", "knowledge_question",
                "mixed_query", "unclassified",
                name="intent_type", create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("is_rag", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rag_chunks_count", sa.Integer(), nullable=True),
        sa.Column("rag_empty", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("idx_request_logs_user_id", "request_logs", ["user_id", "created_at"])
    op.create_index("idx_request_logs_created_at", "request_logs", ["created_at"])


def downgrade() -> None:
    # Drop tables in reverse dependency order
    op.drop_table("request_logs")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("subscriptions")
    op.drop_table("reminders")
    op.drop_table("appointments")
    op.drop_table("preferences")
    op.drop_table("doctor_questions")
    op.drop_table("water_logs")
    op.drop_table("weight_logs")
    op.drop_table("medications")
    op.drop_table("exercises")
    op.drop_table("symptoms")
    op.drop_table("meal_nutrients")
    op.drop_table("meal_items")
    op.drop_table("meals")
    op.drop_table("consent_records")
    op.drop_table("users")
    op.drop_table("family_units")

    # Drop enums
    for enum_name in [
        "intent_type",
        "knowledge_category",
        "subscription_status",
        "reminder_type",
        "appointment_type",
        "preference_type",
        "visibility_level",
        "food_preference",
        "user_role",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
