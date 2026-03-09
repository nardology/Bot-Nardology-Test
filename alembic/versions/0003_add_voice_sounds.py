"""add voice_sounds registry

Revision ID: 0003_add_voice_sounds
Revises: 0002_add_perf_indexes
Create Date: 2026-01-16
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_add_voice_sounds"
down_revision = "0002_add_perf_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep it idempotent for repeated deploys.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_sounds (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            name VARCHAR(64) NOT NULL,
            storage_mode VARCHAR(16) NOT NULL DEFAULT 'local',
            object_key VARCHAR(256) NULL,
            url TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_voice_sounds_guild_name UNIQUE (guild_id, name)
        );
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_voice_sounds_guild_id
        ON voice_sounds (guild_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_voice_sounds_guild_name
        ON voice_sounds (guild_id, name);
        """
    )


def downgrade() -> None:
    # Safe downgrade
    op.execute("DROP TABLE IF EXISTS voice_sounds;")
