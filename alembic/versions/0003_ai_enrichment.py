"""AI enrichment storage: tag facets, summary details and tag freshness markers.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tags", sa.Column("facet", sa.String(length=40), nullable=True))
    op.create_index("ix_tags_facet", "tags", ["facet"])

    op.add_column("games", sa.Column("ai_tags_digest", sa.String(length=64), nullable=True))
    op.add_column("games", sa.Column("ai_tags_model", sa.String(length=120), nullable=True))
    op.add_column(
        "games", sa.Column("ai_tags_generated_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column("review_summaries", sa.Column("verdict", sa.String(length=200), nullable=True))
    op.add_column("review_summaries", sa.Column("positives", sa.JSON(), nullable=True))
    op.add_column("review_summaries", sa.Column("negatives", sa.JSON(), nullable=True))
    op.add_column(
        "review_summaries", sa.Column("input_digest", sa.String(length=80), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("review_summaries", "input_digest")
    op.drop_column("review_summaries", "negatives")
    op.drop_column("review_summaries", "positives")
    op.drop_column("review_summaries", "verdict")
    op.drop_column("games", "ai_tags_generated_at")
    op.drop_column("games", "ai_tags_model")
    op.drop_column("games", "ai_tags_digest")
    op.drop_index("ix_tags_facet", table_name="tags")
    op.drop_column("tags", "facet")
