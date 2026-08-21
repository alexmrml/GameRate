"""Record which source a YouTube analysis came from and which caption track fed it.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("youtube_analyses", sa.Column("analysis_source", sa.String(20), nullable=True))
    op.add_column(
        "youtube_analyses", sa.Column("transcript_language", sa.String(20), nullable=True)
    )
    op.add_column(
        "youtube_analyses", sa.Column("transcript_is_automatic", sa.Boolean(), nullable=True)
    )
    # Every row that exists at this point was produced by the Gemini video path, which is
    # now the fallback. Naming it keeps the detail page honest about older results.
    op.execute(
        "UPDATE youtube_analyses SET analysis_source = 'video' WHERE speech_transcript IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("youtube_analyses", "transcript_is_automatic")
    op.drop_column("youtube_analyses", "transcript_language")
    op.drop_column("youtube_analyses", "analysis_source")
