"""Create hot news analysis runs.

Revision ID: 20260905_0007
Revises: 20260831_0006
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260905_0007"
down_revision = "20260831_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "production_bundle_version", sa.String(120), nullable=False
        ),
        sa.Column("workflow_version", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fetched_record_count", sa.Integer(), nullable=False),
        sa.Column("metric_snapshot_count", sa.Integer(), nullable=False),
        sa.Column("ranked_news_count", sa.Integer(), nullable=False),
        sa.Column("analyzed_news_count", sa.Integer(), nullable=False),
        sa.Column("payload_schema_version", sa.String(32), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=False
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
            "window_start < window_end",
            name="ck_analysis_runs_valid_window",
        ),
        sa.CheckConstraint(
            "fetched_record_count >= 0",
            name="ck_analysis_runs_fetched_count_nonnegative",
        ),
        sa.CheckConstraint(
            "metric_snapshot_count >= 0",
            name="ck_analysis_runs_metric_count_nonnegative",
        ),
        sa.CheckConstraint(
            "ranked_news_count >= 0",
            name="ck_analysis_runs_ranked_count_nonnegative",
        ),
        sa.CheckConstraint(
            "analyzed_news_count >= 0",
            name="ck_analysis_runs_analyzed_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analysis_runs"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_analysis_runs_tenant_idempotency_key",
        ),
    )
    op.create_index(
        "ix_analysis_runs_tenant_window",
        "analysis_runs",
        ["tenant_id", "window_start", "window_end"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_runs_tenant_window", table_name="analysis_runs"
    )
    op.drop_table("analysis_runs")
