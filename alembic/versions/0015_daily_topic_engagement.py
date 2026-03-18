"""daily topic engagement tables

Revision ID: 0015_daily_topic_engagement
Revises: 0014_engagement_badges_weekly
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_daily_topic_engagement"
down_revision = "0014_engagement_badges_weekly"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_topic_config",
        sa.Column("guild_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("topic_text", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("topic_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic_examples_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_set_day_utc", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("last_auto_rotate_day_utc", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_daily_topic_config_guild", "daily_topic_config", ["guild_id"], unique=False)

    op.create_table(
        "daily_topic_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_text", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("topic_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic_examples_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_daily_topic_history_guild", "daily_topic_history", ["guild_id"], unique=False)

    op.create_table(
        "daily_topic_completions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_day_utc", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_daily_topic_completion_unique",
        "daily_topic_completions",
        ["guild_id", "user_id", "topic_version"],
        unique=True,
    )
    op.create_index(
        "ix_daily_topic_completion_user",
        "daily_topic_completions",
        ["guild_id", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_daily_topic_completion_user", table_name="daily_topic_completions")
    op.drop_index("ix_daily_topic_completion_unique", table_name="daily_topic_completions")
    op.drop_table("daily_topic_completions")

    op.drop_index("ix_daily_topic_history_guild", table_name="daily_topic_history")
    op.drop_table("daily_topic_history")

    op.drop_index("ix_daily_topic_config_guild", table_name="daily_topic_config")
    op.drop_table("daily_topic_config")

