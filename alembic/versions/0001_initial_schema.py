"""Initial application schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""

from alembic import op
from app import models  # noqa: F401
from app.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This baseline deliberately uses the canonical SQLAlchemy metadata so the
    # initial schema cannot drift from the new, still integration-free domain model.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
