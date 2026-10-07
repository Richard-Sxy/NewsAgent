"""Allow owned conversations to be hidden and restored without deleting history."""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0020"
down_revision = "20261006_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversations", "deleted_at")
