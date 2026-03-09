"""add character_recommendations table

Revision ID: 0012_add_recommendations
Revises: 0011_user_premium
Create Date: 2026-03-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_add_recommendations"
down_revision = "0011_user_premium"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_recommendations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Character fields
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("rarity", sa.String(length=20), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("tips", sa.Text(), nullable=True),
        sa.Column("backstory", sa.Text(), nullable=True),
        sa.Column("personality_traits", sa.Text(), nullable=True),
        sa.Column("quirks", sa.Text(), nullable=True),
        sa.Column("speech_style", sa.Text(), nullable=True),
        sa.Column("fears", sa.Text(), nullable=True),
        sa.Column("desires", sa.Text(), nullable=True),
        sa.Column("likes", sa.Text(), nullable=True),
        sa.Column("dislikes", sa.Text(), nullable=True),
        sa.Column("catchphrases", sa.Text(), nullable=True),
        sa.Column("secrets", sa.Text(), nullable=True),
        sa.Column("lore", sa.Text(), nullable=True),
        sa.Column("age", sa.String(length=100), nullable=True),
        sa.Column("occupation", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("world", sa.String(length=100), nullable=True),
        sa.Column("original_world", sa.String(length=100), nullable=True),
        sa.Column("world_knowledge", sa.Text(), nullable=True),
        sa.Column("relationships", sa.Text(), nullable=True),
        sa.Column("topic_reactions", sa.Text(), nullable=True),
        sa.Column("reviewer_notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_char_rec_user_id", "character_recommendations", ["user_id"])
    op.create_index("ix_char_rec_user_status", "character_recommendations", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_char_rec_user_status", table_name="character_recommendations")
    op.drop_index("ix_char_rec_user_id", table_name="character_recommendations")
    op.drop_table("character_recommendations")
