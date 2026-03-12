"""add engagement streak reward fields to points_wallet

Revision ID: 0013_engagement_streak_rewards
Revises: 0012_add_recommendations
Create Date: 2026-03-10

Streak milestones: day 7 (500 pts), every 30 days (2000 pts), day 10/15/25 (character pick),
day 75 (owner DM). Random daily bonus: escalating chance, resets on reward.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_engagement_streak_rewards"
down_revision = "0012_add_recommendations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "points_wallet",
        sa.Column("streak_7_bonus_given", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_last_30_bonus_at", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_10_character_claimed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_15_character_claimed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_25_character_claimed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("streak_75_notification_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "points_wallet",
        sa.Column("random_bonus_consecutive_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "points_wallet",
        sa.Column("random_bonus_last_reward_day_utc", sa.String(length=8), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("points_wallet", "random_bonus_last_reward_day_utc")
    op.drop_column("points_wallet", "random_bonus_consecutive_days")
    op.drop_column("points_wallet", "streak_75_notification_sent")
    op.drop_column("points_wallet", "streak_25_character_claimed")
    op.drop_column("points_wallet", "streak_15_character_claimed")
    op.drop_column("points_wallet", "streak_10_character_claimed")
    op.drop_column("points_wallet", "streak_last_30_bonus_at")
    op.drop_column("points_wallet", "streak_7_bonus_given")
