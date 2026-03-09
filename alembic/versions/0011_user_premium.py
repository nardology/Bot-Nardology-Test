"""add user_premium_entitlements and premium_gifts tables

Revision ID: 0011_user_premium
Revises: 0010_add_stripe_tables
Create Date: 2026-02-16

Individual premium: premium is now per-user instead of per-guild.
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_user_premium"
down_revision = "0010_add_stripe_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- New table: user_premium_entitlements ---
    op.create_table(
        "user_premium_entitlements",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("tier", sa.String(length=16), nullable=False, server_default="free"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("stripe_subscription_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=True),
        sa.Column("subscription_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gifted_by_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_user_premium_entitlements_user", "user_premium_entitlements", ["user_id"])

    # --- New table: premium_gifts ---
    op.create_table(
        "premium_gifts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("gifter_user_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_user_id", sa.BigInteger(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stripe_session_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_premium_gifts_gifter", "premium_gifts", ["gifter_user_id"])
    op.create_index("ix_premium_gifts_recipient", "premium_gifts", ["recipient_user_id"])


def downgrade() -> None:
    op.drop_index("ix_premium_gifts_recipient", table_name="premium_gifts")
    op.drop_index("ix_premium_gifts_gifter", table_name="premium_gifts")
    op.drop_table("premium_gifts")

    op.drop_index("ix_user_premium_entitlements_user", table_name="user_premium_entitlements")
    op.drop_table("user_premium_entitlements")
