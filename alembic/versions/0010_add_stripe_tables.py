"""add stripe_customers table and stripe columns on premium_entitlements

Revision ID: 0010_add_stripe_tables
Revises: 0009_add_user_activity_day
Create Date: 2026-02-14

Phase 6: Stripe payment integration.
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_add_stripe_tables"
down_revision = "0009_add_user_activity_day"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- New table: stripe_customers ---
    op.create_table(
        "stripe_customers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_stripe_customers_discord", "stripe_customers", ["discord_user_id"], unique=True)
    op.create_index("ix_stripe_customers_stripe", "stripe_customers", ["stripe_customer_id"], unique=True)

    # --- Add Stripe columns to premium_entitlements ---
    op.add_column("premium_entitlements", sa.Column("stripe_subscription_id", sa.String(length=128), nullable=True))
    op.add_column("premium_entitlements", sa.Column("stripe_customer_id", sa.String(length=128), nullable=True))
    op.add_column("premium_entitlements", sa.Column("subscription_period_end", sa.DateTime(timezone=True), nullable=True))
    op.add_column("premium_entitlements", sa.Column("activated_by_user_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("premium_entitlements", "activated_by_user_id")
    op.drop_column("premium_entitlements", "subscription_period_end")
    op.drop_column("premium_entitlements", "stripe_customer_id")
    op.drop_column("premium_entitlements", "stripe_subscription_id")

    op.drop_index("ix_stripe_customers_stripe", table_name="stripe_customers")
    op.drop_index("ix_stripe_customers_discord", table_name="stripe_customers")
    op.drop_table("stripe_customers")
