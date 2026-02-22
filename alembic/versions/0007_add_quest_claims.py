"""add quest claims table

Revision ID: 0007_add_quest_claims
Revises: 0006_add_quests
Create Date: 2026-01-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_add_quest_claims"
down_revision = "0006_add_quests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quest_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False, server_default="daily"),
        sa.Column("period_key", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("quest_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_quest_claims_unique",
        "quest_claims",
        ["guild_id", "user_id", "period", "period_key", "quest_id"],
        unique=True,
    )
    op.create_index("ix_quest_claims_user", "quest_claims", ["guild_id", "user_id"], unique=False)

    # Backfill: mark already-completed quests as claimed so we don't double-award when switching
    # from auto-award to claim.
    try:
        op.execute(
            sa.text(
                "INSERT INTO quest_claims (guild_id, user_id, period, period_key, quest_id, claimed_at) "
                "SELECT guild_id, user_id, period, period_key, quest_id, NOW() "
                "FROM quest_progress WHERE completed = true"
            )
        )
    except Exception:
        # Best-effort backfill; safe to ignore in environments without quest_progress yet.
        pass


def downgrade() -> None:
    op.drop_index("ix_quest_claims_user", table_name="quest_claims")
    op.drop_index("ix_quest_claims_unique", table_name="quest_claims")
    op.drop_table("quest_claims")
