"""create guild_settings

Revision ID: 0001_create_guild_settings
Revises: 
Create Date: 2025-12-31T22:19:02.173967Z
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_create_guild_settings"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guild_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("guild_settings")
