"""Create hot events lifecycle table.

Revision ID: 20260912_0015
Revises: 20260911_0014
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260912_0015"
down_revision = "20260911_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hot_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_window_end", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("distinct_news_count", sa.Integer(), nullable=False),
        sa.Column("member_news_ids", postgresql.JSONB(), nullable=False),
        sa.Column("latest_hot_score", sa.Float(), nullable=False),
        sa.Column("latest_metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('emerging', 'active', 'cooling', 'closed')",
            name="ck_hot_events_state_valid",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 1",
            name="ck_hot_events_occurrence_count_positive",
        ),
        sa.CheckConstraint(
            "distinct_news_count >= 1",
            name="ck_hot_events_distinct_news_count_positive",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_hot_events_version_positive"
        ),
        sa.CheckConstraint(
            "latest_hot_score >= 0 AND latest_hot_score <= 1",
            name="ck_hot_events_hot_score_range",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(member_news_ids) = 'array' "
            "AND jsonb_array_length(member_news_ids) > 0",
            name="ck_hot_events_member_news_ids_nonempty",
        ),
        sa.CheckConstraint(
            "first_seen_at <= last_seen_at",
            name="ck_hot_events_seen_order",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hot_events"),
        sa.UniqueConstraint(
            "tenant_id",
            "event_key",
            name="uq_hot_events_tenant_event_key",
        ),
    )
    op.create_index(
        "ix_hot_events_tenant_state_last_seen",
        "hot_events",
        ["tenant_id", "state", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hot_events_tenant_state_last_seen", table_name="hot_events"
    )
    op.drop_table("hot_events")
