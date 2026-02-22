"""add points economy tables

Revision ID: 0005_add_points_economy
Revises: 0004_add_analytics_tables
Create Date: 2026-01-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# Alembic identifiers
revision = "0005_add_points_economy"
down_revision = "0004_add_analytics_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "points_wallet",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_claim_day_utc", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_claimed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("booster_kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("booster_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_points_wallet_unique", "points_wallet", ["guild_id", "user_id"], unique=True)
    op.create_index("ix_points_wallet_guild", "points_wallet", ["guild_id"], unique=False)
    op.create_index("ix_points_wallet_user", "points_wallet", ["user_id"], unique=False)

    op.create_table(
        "points_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_points_ledger_guild_day", "points_ledger", ["guild_id", "created_at"], unique=False)
    op.create_index("ix_points_ledger_user_day", "points_ledger", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_points_ledger_user_day", table_name="points_ledger")
    op.drop_index("ix_points_ledger_guild_day", table_name="points_ledger")
    op.drop_table("points_ledger")

    op.drop_index("ix_points_wallet_user", table_name="points_wallet")
    op.drop_index("ix_points_wallet_guild", table_name="points_wallet")
    op.drop_index("ix_points_wallet_unique", table_name="points_wallet")
    op.drop_table("points_wallet")
