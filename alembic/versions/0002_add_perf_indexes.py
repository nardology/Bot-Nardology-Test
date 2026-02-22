"""add performance indexes

Revision ID: 0002_add_perf_indexes
Revises: 0001_create_guild_settings
Create Date: 2026-01-04
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_add_perf_indexes"
down_revision = "0001_create_guild_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE:
    # Using raw SQL with IF NOT EXISTS so repeated deploys don't fail.
    # These indexes target your real hot queries in talk_store.py + scene_store.py.

    # ---- ask_submissions (used by /talk usage tracking) ----
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ask_submissions_guild_created_at
        ON ask_submissions (guild_id, created_at);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ask_submissions_guild_user_created_at
        ON ask_submissions (guild_id, user_id, created_at);
        """
    )

    # ---- scene_turn_usage (used by /scene daily usage counts) ----
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_scene_turn_usage_guild_created_at
        ON scene_turn_usage (guild_id, created_at);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_scene_turn_usage_guild_user_created_at
        ON scene_turn_usage (guild_id, user_id, created_at);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_scene_turn_usage_scene_created_at
        ON scene_turn_usage (scene_id, created_at);
        """
    )

    # ---- rp_scenes (active scene lookups, listing, and stale expiry) ----
    # list_active_scenes_in_channel: guild_id + channel_id + is_active ORDER BY updated_at desc
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rp_scenes_guild_channel_active_updated
        ON rp_scenes (guild_id, channel_id, is_active, updated_at);
        """
    )
    # count_active_scenes_in_guild: guild_id + is_active
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rp_scenes_guild_active
        ON rp_scenes (guild_id, is_active);
        """
    )
    # find_active_scene_between: filters by guild_id + channel_id + is_active + p1/p2 ids (in either order)
    # Two indexes help either side of the OR efficiently.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rp_scenes_guild_channel_active_p1
        ON rp_scenes (guild_id, channel_id, is_active, p1_user_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rp_scenes_guild_channel_active_p2
        ON rp_scenes (guild_id, channel_id, is_active, p2_user_id);
        """
    )

    # ---- rp_scene_lines (recent transcript fetch) ----
    # get_recent_scene_lines: scene_id ORDER BY created_at desc LIMIT N
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rp_scene_lines_scene_created_at
        ON rp_scene_lines (scene_id, created_at);
        """
    )
    # Optional helpful index for moderation or channel history tooling
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_rp_scene_lines_guild_channel_created_at
        ON rp_scene_lines (guild_id, channel_id, created_at);
        """
    )

    # ---- feedback_submissions / say_submissions (optional but cheap) ----
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_feedback_submissions_guild_created_at
        ON feedback_submissions (guild_id, created_at);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_say_submissions_guild_created_at
        ON say_submissions (guild_id, created_at);
        """
    )


def downgrade() -> None:
    # Downgrade intentionally minimal. If you need it, add DROP INDEX IF EXISTS statements.
    pass
