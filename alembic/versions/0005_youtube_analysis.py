"""YouTube discovery state, source metadata and structured Gemini analysis.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_game_youtube_video", "youtube_analyses", type_="unique")
    op.alter_column("youtube_analyses", "video_id", existing_type=sa.String(32), nullable=True)
    op.add_column("youtube_analyses", sa.Column("status", sa.String(40), nullable=True))
    op.add_column("youtube_analyses", sa.Column("status_reason", sa.String(1000), nullable=True))
    op.add_column("youtube_analyses", sa.Column("search_query", sa.String(1000), nullable=True))
    op.add_column("youtube_analyses", sa.Column("search_data", sa.JSON(), nullable=True))
    op.add_column(
        "youtube_analyses",
        sa.Column("search_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "youtube_analyses",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("youtube_analyses", sa.Column("video_url", sa.String(1000), nullable=True))
    op.add_column("youtube_analyses", sa.Column("channel_id", sa.String(80), nullable=True))
    op.add_column("youtube_analyses", sa.Column("view_count", sa.Integer(), nullable=True))
    op.add_column("youtube_analyses", sa.Column("duration_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "youtube_analyses", sa.Column("fragment_start_seconds", sa.Integer(), nullable=True)
    )
    op.add_column(
        "youtube_analyses", sa.Column("fragment_end_seconds", sa.Integer(), nullable=True)
    )
    op.add_column("youtube_analyses", sa.Column("speech_transcript", sa.Text(), nullable=True))
    op.add_column("youtube_analyses", sa.Column("liked", sa.JSON(), nullable=True))
    op.add_column("youtube_analyses", sa.Column("disliked", sa.JSON(), nullable=True))

    # The pre-integration table should be empty, but make an existing row explicit and valid.
    op.execute("UPDATE youtube_analyses SET status = 'pending' WHERE status IS NULL")
    op.alter_column("youtube_analyses", "status", existing_type=sa.String(40), nullable=False)
    op.create_unique_constraint("uq_youtube_analysis_game", "youtube_analyses", ["game_id"])
    op.create_index("ix_youtube_analyses_status", "youtube_analyses", ["status"])
    op.create_index("ix_youtube_analyses_next_retry_at", "youtube_analyses", ["next_retry_at"])


def downgrade() -> None:
    op.drop_index("ix_youtube_analyses_next_retry_at", table_name="youtube_analyses")
    op.drop_index("ix_youtube_analyses_status", table_name="youtube_analyses")
    op.drop_constraint("uq_youtube_analysis_game", "youtube_analyses", type_="unique")
    op.drop_column("youtube_analyses", "disliked")
    op.drop_column("youtube_analyses", "liked")
    op.drop_column("youtube_analyses", "speech_transcript")
    op.drop_column("youtube_analyses", "fragment_end_seconds")
    op.drop_column("youtube_analyses", "fragment_start_seconds")
    op.drop_column("youtube_analyses", "duration_seconds")
    op.drop_column("youtube_analyses", "view_count")
    op.drop_column("youtube_analyses", "channel_id")
    op.drop_column("youtube_analyses", "video_url")
    op.drop_column("youtube_analyses", "next_retry_at")
    op.drop_column("youtube_analyses", "search_attempted_at")
    op.drop_column("youtube_analyses", "search_data")
    op.drop_column("youtube_analyses", "search_query")
    op.drop_column("youtube_analyses", "status_reason")
    op.drop_column("youtube_analyses", "status")
    op.alter_column("youtube_analyses", "video_id", existing_type=sa.String(32), nullable=False)
    op.create_unique_constraint(
        "uq_game_youtube_video", "youtube_analyses", ["game_id", "video_id"]
    )
