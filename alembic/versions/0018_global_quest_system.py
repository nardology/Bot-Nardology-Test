"""Global monthly quest (team training) + profile badges

Revision ID: 0018_global_quest_system
Revises: 0017_character_weekly_topics
Create Date: 2026-03-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_global_quest_system"
down_revision = "0017_character_weekly_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "global_quest_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("image_url_secondary", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_training_points", sa.BigInteger(), nullable=False, server_default="100000"),
        sa.Column("character_multipliers_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("reward_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_badge_emoji", sa.String(length=16), nullable=True),
        sa.Column("success_badge_label", sa.String(length=120), nullable=True),
        sa.Column("grant_success_badge", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("resolution_applied", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_global_quest_events_slug"),
    )
    op.create_index("ix_global_quest_events_status", "global_quest_events", ["status"])
    op.create_index("ix_global_quest_events_guild", "global_quest_events", ["guild_id"])

    op.create_table(
        "global_quest_contributions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("style_id", sa.String(length=64), nullable=False),
        sa.Column("training_points", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["global_quest_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "guild_id", "user_id", "style_id", name="uq_gq_contrib_event_guild_user_style"),
    )
    op.create_index("ix_gq_contrib_event", "global_quest_contributions", ["event_id"])
    op.create_index("ix_gq_contrib_user", "global_quest_contributions", ["user_id"])

    op.create_table(
        "user_profile_badges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("badge_key", sa.String(length=128), nullable=False),
        sa.Column("display_text", sa.String(length=200), nullable=False),
        sa.Column("source_event_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["global_quest_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "badge_key", name="uq_user_profile_badges_user_key"),
    )
    op.create_index("ix_user_profile_badges_user", "user_profile_badges", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_profile_badges_user", table_name="user_profile_badges")
    op.drop_table("user_profile_badges")
    op.drop_index("ix_gq_contrib_user", table_name="global_quest_contributions")
    op.drop_index("ix_gq_contrib_event", table_name="global_quest_contributions")
    op.drop_table("global_quest_contributions")
    op.drop_index("ix_global_quest_events_guild", table_name="global_quest_events")
    op.drop_index("ix_global_quest_events_status", table_name="global_quest_events")
    op.drop_table("global_quest_events")
