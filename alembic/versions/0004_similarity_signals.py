"""Extra Metacritic signals used by similarity matching.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("games", sa.Column("esrb_rating", sa.String(length=16), nullable=True))
    op.add_column("games", sa.Column("related_slugs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("games", "related_slugs")
    op.drop_column("games", "esrb_rating")
