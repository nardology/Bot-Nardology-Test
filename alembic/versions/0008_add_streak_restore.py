"""add streak restore fields to points wallet

Revision ID: 0008_add_streak_restore
Revises: 0007_add_quest_claims
Create Date: 2026-01-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0008_add_streak_restore"
down_revision = "0007_add_quest_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("points_wallet", sa.Column("streak_saved", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "points_wallet",
        sa.Column("streak_restore_deadline_day_utc", sa.String(length=8), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("points_wallet", "streak_restore_deadline_day_utc")
    op.drop_column("points_wallet", "streak_saved")
