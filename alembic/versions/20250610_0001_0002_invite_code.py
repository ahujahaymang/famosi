"""Add invite_code to family_units for partner linking.

Revision ID: 0002
Revises: 0001
Create Date: 2025-06-10 00:00:00.000000

Adds:
  family_units.invite_code  — 6-char uppercase alphanumeric token (unique)
  family_units.invite_used  — True once a partner has joined
  family_units.mom_user_id  — FK to the mom user row for quick lookup

This enables the partner-linking flow:
  1. Mom sends /invite → bot generates a code and stores it here.
  2. Partner enters the code during onboarding → linked to the same family_unit.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "family_units",
        sa.Column("invite_code", sa.String(6), nullable=True, unique=True),
    )
    op.add_column(
        "family_units",
        sa.Column("invite_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "family_units",
        sa.Column("mom_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_family_units_mom_user_id",
        "family_units", "users",
        ["mom_user_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_family_units_invite_code", "family_units", ["invite_code"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_family_units_invite_code", table_name="family_units")
    op.drop_constraint("fk_family_units_mom_user_id", "family_units", type_="foreignkey")
    op.drop_column("family_units", "mom_user_id")
    op.drop_column("family_units", "invite_used")
    op.drop_column("family_units", "invite_code")
