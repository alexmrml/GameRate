"""Add the unique index for game-level review summaries.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_review_summary_game_audience",
        "review_summaries",
        ["game_id", "audience"],
        unique=True,
        postgresql_where=sa.text("platform_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_summary_game_audience", table_name="review_summaries")
