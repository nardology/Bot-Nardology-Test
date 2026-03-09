"""add analytics tables

Revision ID: 0004_add_analytics_tables
Revises: 0003_add_voice_sounds
Create Date: 2026-01-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_add_analytics_tables"
down_revision = "0003_add_voice_sounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_daily_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("day_utc", sa.String(length=8), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("day_utc", "guild_id", "metric", name="ix_analytics_daily_unique"),
    )
    op.create_index("ix_analytics_daily_metrics_day_utc", "analytics_daily_metrics", ["day_utc"])
    op.create_index("ix_analytics_daily_metrics_guild_id", "analytics_daily_metrics", ["guild_id"])
    op.create_index("ix_analytics_daily_metrics_metric", "analytics_daily_metrics", ["metric"])

    op.create_table(
        "analytics_user_first_seen",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("first_day_utc", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("guild_id", "user_id", name="ix_first_seen_unique"),
    )
    op.create_index("ix_analytics_user_first_seen_guild_id", "analytics_user_first_seen", ["guild_id"])
    op.create_index("ix_analytics_user_first_seen_user_id", "analytics_user_first_seen", ["user_id"])
    op.create_index("ix_analytics_user_first_seen_first_day_utc", "analytics_user_first_seen", ["first_day_utc"])


def downgrade() -> None:
    op.drop_index("ix_analytics_user_first_seen_first_day_utc", table_name="analytics_user_first_seen")
    op.drop_index("ix_analytics_user_first_seen_user_id", table_name="analytics_user_first_seen")
    op.drop_index("ix_analytics_user_first_seen_guild_id", table_name="analytics_user_first_seen")
    op.drop_table("analytics_user_first_seen")

    op.drop_index("ix_analytics_daily_metrics_metric", table_name="analytics_daily_metrics")
    op.drop_index("ix_analytics_daily_metrics_guild_id", table_name="analytics_daily_metrics")
    op.drop_index("ix_analytics_daily_metrics_day_utc", table_name="analytics_daily_metrics")
    op.drop_table("analytics_daily_metrics")
