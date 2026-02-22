"""add quest progress table

Revision ID: 0006_add_quests
Revises: 0005_add_points_economy
Create Date: 2026-01-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_add_quests"
down_revision = "0005_add_points_economy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quest_progress",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False, server_default="daily"),
        sa.Column("period_key", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("quest_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_quest_progress_unique", "quest_progress", ["guild_id", "user_id", "period", "quest_id"], unique=True)
    op.create_index("ix_quest_progress_user", "quest_progress", ["guild_id", "user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_quest_progress_user", table_name="quest_progress")
    op.drop_index("ix_quest_progress_unique", table_name="quest_progress")
    op.drop_table("quest_progress")
