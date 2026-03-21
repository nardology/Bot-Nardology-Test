"""character connection profiles (traits shop)

Revision ID: 0016_character_connection_profiles
Revises: 0015_daily_topic_engagement
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_character_connection_profiles"
down_revision = "0015_daily_topic_engagement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_connection_profiles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("style_id", sa.String(length=64), nullable=False),
        sa.Column("purchased_traits_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("user_id", "style_id"),
    )
    op.create_index("ix_conn_profile_user", "character_connection_profiles", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_conn_profile_user", table_name="character_connection_profiles")
    op.drop_table("character_connection_profiles")
