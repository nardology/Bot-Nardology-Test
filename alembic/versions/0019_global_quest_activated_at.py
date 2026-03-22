"""Add global_quest_events.activated_at

Revision ID: 0019_global_quest_activated_at
Revises: 0018_global_quest_system
Create Date: 2026-03-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_global_quest_activated_at"
down_revision = "0018_global_quest_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "global_quest_events",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE global_quest_events SET activated_at = starts_at "
        "WHERE status = 'active' AND activated_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("global_quest_events", "activated_at")
