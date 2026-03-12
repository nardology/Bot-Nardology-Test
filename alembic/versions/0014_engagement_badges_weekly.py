"""add streak badges (30/60/90) and weekly activity bonus tracking

Revision ID: 0014_engagement_badges_weekly
Revises: 0013_engagement_streak_rewards
Create Date: 2026-03-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_engagement_badges_weekly"
down_revision = "0013_engagement_streak_rewards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "points_wallet",
        sa.Column("streak_badge_30", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_badge_60", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_badge_90", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("weekly_activity_bonus_week_utc", sa.String(length=8), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("points_wallet", "weekly_activity_bonus_week_utc")
    op.drop_column("points_wallet", "streak_badge_90")
    op.drop_column("points_wallet", "streak_badge_60")
    op.drop_column("points_wallet", "streak_badge_30")
