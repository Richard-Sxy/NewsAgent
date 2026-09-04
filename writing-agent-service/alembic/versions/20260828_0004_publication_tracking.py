"""add publication tracking

Revision ID: 20260828_0004
Revises: 20260827_0003
"""

from alembic import op
import sqlalchemy as sa

revision = "20260828_0004"
down_revision = "20260827_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "writing_jobs",
        sa.Column("external_publication_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "writing_jobs",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("writing_jobs", "published_at")
    op.drop_column("writing_jobs", "external_publication_id")
