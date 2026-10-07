"""Persist length-triggered conversation summaries without changing original turns."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261006_0019"
down_revision = "20261004_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("context_memory", postgresql.JSONB(),
                  nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    op.drop_column("conversations", "context_memory")
