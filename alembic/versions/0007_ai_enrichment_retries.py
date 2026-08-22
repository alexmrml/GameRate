"""Keep exhausted AI enrichment requests in a durable retry queue.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_enrichment_retries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.String(length=20), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "task", name="uq_ai_enrichment_retry_game_task"),
    )
    op.create_index("ix_ai_enrichment_retries_game_id", "ai_enrichment_retries", ["game_id"])
    op.create_index("ix_ai_enrichment_retries_task", "ai_enrichment_retries", ["task"])
    op.create_index("ix_ai_enrichment_retries_failed_at", "ai_enrichment_retries", ["failed_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_enrichment_retries_failed_at", table_name="ai_enrichment_retries")
    op.drop_index("ix_ai_enrichment_retries_task", table_name="ai_enrichment_retries")
    op.drop_index("ix_ai_enrichment_retries_game_id", table_name="ai_enrichment_retries")
    op.drop_table("ai_enrichment_retries")
