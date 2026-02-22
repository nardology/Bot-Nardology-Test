"""add user activity day table for retention

Revision ID: 0009_add_user_activity_day
Revises: 0008_add_streak_restore
Create Date: 2026-02-03

Phase 3: Enables D1/D7/D30 retention queries.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_add_user_activity_day"
down_revision = "0008_add_streak_restore"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_user_activity_day",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("day_utc", sa.String(length=8), nullable=False),
        sa.UniqueConstraint("user_id", "guild_id", "day_utc", name="ix_user_activity_day_unique"),
    )
    op.create_index("ix_user_activity_day_user_guild", "analytics_user_activity_day", ["user_id", "guild_id"])
    op.create_index("ix_user_activity_day_day", "analytics_user_activity_day", ["day_utc"])


def downgrade() -> None:
    op.drop_index("ix_user_activity_day_day", table_name="analytics_user_activity_day")
    op.drop_index("ix_user_activity_day_user_guild", table_name="analytics_user_activity_day")
    op.drop_table("analytics_user_activity_day")
