"""weekly character topics (AI engagement)

Revision ID: 0017_character_weekly_topics
Revises: 0016_character_connection_profiles
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_character_weekly_topics"
down_revision = "0016_character_connection_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_weekly_topics",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("style_id", sa.String(length=64), nullable=False),
        sa.Column("week_id", sa.String(length=16), nullable=False),
        sa.Column("topics_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("keywords_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("hints_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("topic_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_mask", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", "style_id", "week_id"),
    )
    op.create_index(
        "ix_char_weekly_topics_user_week",
        "character_weekly_topics",
        ["user_id", "week_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_char_weekly_topics_user_week", table_name="character_weekly_topics")
    op.drop_table("character_weekly_topics")
