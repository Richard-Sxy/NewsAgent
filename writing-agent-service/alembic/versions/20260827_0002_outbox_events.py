"""Add transactional outbox for progress events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260827_0002"
down_revision = "20260826_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("deduplication_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'published', 'dead_letter')", name="outbox_status_valid"),
        sa.CheckConstraint("attempts >= 0", name="outbox_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["job_id"], ["writing_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint("deduplication_key", name="uq_outbox_events_deduplication_key"),
    )
    op.create_index(
        "ix_outbox_events_pending_available",
        "outbox_events",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_job_created",
        "outbox_events",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_job_created", table_name="outbox_events")
    op.drop_index("ix_outbox_events_pending_available", table_name="outbox_events")
    op.drop_table("outbox_events")
