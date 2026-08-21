"""Metacritic pipeline storage: collected reviews, cover art, scheduled trigger.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("games", sa.Column("cover_image_url", sa.String(length=1000), nullable=True))
    op.add_column("games", sa.Column("video_url", sa.String(length=1000), nullable=True))

    # The hourly crawl needs a trigger of its own next to MANUAL and DAILY.
    op.execute("ALTER TYPE run_trigger ADD VALUE IF NOT EXISTS 'SCHEDULED'")

    op.create_table(
        "game_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("platform_id", sa.Uuid(), nullable=True),
        sa.Column(
            "audience",
            postgresql.ENUM("CRITICS", "USERS", name="review_audience", create_type=False),
            nullable=False,
        ),
        sa.Column("external_key", sa.String(length=255), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("publication", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("review_date", sa.Date(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "external_key", name="uq_game_review_external_key"),
    )
    op.create_index("ix_game_reviews_game_audience", "game_reviews", ["game_id", "audience"])


def downgrade() -> None:
    op.drop_index("ix_game_reviews_game_audience", table_name="game_reviews")
    op.drop_table("game_reviews")
    op.drop_column("games", "video_url")
    op.drop_column("games", "cover_image_url")
    # PostgreSQL cannot drop a single enum label; SCHEDULED stays behind by design.
