"""Create Data Loop publication outcomes, feedback cases, and labels.

Revision ID: 20260909_0010
Revises: 20260907_0009
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260909_0010"
down_revision = "20260907_0009"
branch_labels = None
depends_on = None


def _timestamp_columns() -> tuple[sa.Column, ...]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "publication_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("run_idempotency_key", sa.String(160), nullable=False),
        sa.Column("news_id", sa.String(128), nullable=False),
        sa.Column(
            "external_publication_id", sa.String(256), nullable=False
        ),
        sa.Column("source_system", sa.String(128), nullable=False),
        sa.Column(
            "metric_definition_version", sa.String(64), nullable=False
        ),
        sa.Column(
            "window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "window_end", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("unique_users", sa.Integer(), nullable=False),
        sa.Column(
            "effective_consumptions", sa.Integer(), nullable=False
        ),
        sa.Column("interactions", sa.Integer(), nullable=False),
        sa.Column("complaints", sa.Integer(), nullable=False),
        sa.Column("corrections", sa.Integer(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "window_start < window_end",
            name="window_valid",
        ),
        sa.CheckConstraint(
            "window_end <= recorded_at",
            name="recording_order_valid",
        ),
        sa.CheckConstraint(
            "impressions >= 0 AND clicks >= 0 AND unique_users >= 0 "
            "AND effective_consumptions >= 0 AND interactions >= 0 "
            "AND complaints >= 0 AND corrections >= 0",
            name="metrics_nonnegative",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="content_sha256_valid",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["analysis_runs.id"],
            name="fk_publication_outcomes_run_id_analysis_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_publication_outcomes"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_publication_outcomes_tenant_idempotency_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_publication_outcomes_tenant_id",
        ),
    )
    op.create_index(
        "ix_publication_outcomes_tenant_news_window",
        "publication_outcomes",
        ["tenant_id", "news_id", "window_start", "window_end"],
    )

    op.create_table(
        "feedback_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("run_idempotency_key", sa.String(160), nullable=False),
        sa.Column("news_id", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("problem_type", sa.String(48), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column(
            "production_bundle_version", sa.String(128), nullable=False
        ),
        sa.Column(
            "analysis_input_snapshot", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "analysis_output_snapshot", postgresql.JSONB(), nullable=True
        ),
        sa.Column("source_reference", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "source_type IN ('operator_rejected', 'operator_corrected', "
            "'validation_failed', 'low_confidence', 'retrieval_error', "
            "'post_publish_outcome')",
            name="source_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('collected', 'needs_label', 'labeled', "
            "'excluded', 'frozen')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "problem_type IN ('analysis_incorrect', 'unsupported_claim', "
            "'metric_mismatch', 'evidence_mismatch', 'missing_evidence', "
            "'low_confidence', 'retrieval_miss', "
            "'retrieval_false_positive', 'schema_violation', "
            "'policy_violation', 'outcome_underperformance', 'other')",
            name="problem_type_valid",
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="severity_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(analysis_input_snapshot) = 'object'",
            name="input_snapshot_object",
        ),
        sa.CheckConstraint(
            "analysis_output_snapshot IS NULL OR "
            "jsonb_typeof(analysis_output_snapshot) = 'object'",
            name="output_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_reference) = 'object'",
            name="source_reference_object",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="content_sha256_valid",
        ),
        sa.CheckConstraint(
            "occurred_at <= recorded_at",
            name="recording_order_valid",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["analysis_runs.id"],
            name="fk_feedback_cases_run_id_analysis_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feedback_cases"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_feedback_cases_tenant_idempotency_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_feedback_cases_tenant_id",
        ),
    )
    op.create_index(
        "ix_feedback_cases_tenant_status_recorded",
        "feedback_cases",
        ["tenant_id", "status", "recorded_at"],
    )
    op.create_index(
        "ix_feedback_cases_tenant_run_news",
        "feedback_cases",
        ["tenant_id", "run_idempotency_key", "news_id"],
    )

    op.create_table(
        "feedback_labels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "feedback_case_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("label_version", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column(
            "allowed_dominant_drivers", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "required_evidence_news_ids", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "forbidden_evidence_news_ids",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "required_metric_keys", postgresql.JSONB(), nullable=False
        ),
        sa.Column("must_state_limitation", sa.Boolean(), nullable=False),
        sa.Column("operator_comment", sa.Text(), nullable=False),
        sa.Column("approval_status", sa.String(32), nullable=False),
        sa.Column("labeled_by", sa.String(128), nullable=False),
        sa.Column(
            "labeled_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column(
            "approval_idempotency_key", sa.String(160), nullable=True
        ),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "label_version >= 1",
            name="label_version_positive",
        ),
        sa.CheckConstraint(
            "verdict IN ('correct', 'incorrect', 'partially_correct', "
            "'not_evaluable')",
            name="verdict_valid",
        ),
        sa.CheckConstraint(
            "approval_status IN ('pending', 'approved', 'rejected', "
            "'superseded')",
            name="approval_status_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_dominant_drivers) = 'array' AND "
            "jsonb_typeof(required_evidence_news_ids) = 'array' AND "
            "jsonb_typeof(forbidden_evidence_news_ids) = 'array' AND "
            "jsonb_typeof(required_metric_keys) = 'array'",
            name="label_collections_array",
        ),
        sa.CheckConstraint(
            "(approval_status = 'approved' AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL "
            "AND approval_idempotency_key IS NOT NULL) OR "
            "(approval_status IN ('pending', 'rejected') "
            "AND approved_by IS NULL AND approved_at IS NULL "
            "AND approval_idempotency_key IS NULL) OR "
            "approval_status = 'superseded'",
            name="approval_metadata_valid",
        ),
        sa.CheckConstraint(
            "approved_at IS NULL OR approved_at >= labeled_at",
            name="approval_order_valid",
        ),
        sa.CheckConstraint(
            "labeled_at <= recorded_at",
            name="label_recording_order_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "feedback_case_id"],
            ["feedback_cases.tenant_id", "feedback_cases.id"],
            name="fk_feedback_labels_case_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feedback_labels"),
        sa.UniqueConstraint(
            "tenant_id",
            "feedback_case_id",
            "label_version",
            name="uq_feedback_labels_case_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_feedback_labels_tenant_idempotency_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "approval_idempotency_key",
            name="uq_feedback_labels_tenant_approval_idempotency_key",
        ),
    )
    op.create_index(
        "ix_feedback_labels_tenant_case_version",
        "feedback_labels",
        ["tenant_id", "feedback_case_id", "label_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feedback_labels_tenant_case_version",
        table_name="feedback_labels",
    )
    op.drop_table("feedback_labels")
    op.drop_index(
        "ix_feedback_cases_tenant_run_news",
        table_name="feedback_cases",
    )
    op.drop_index(
        "ix_feedback_cases_tenant_status_recorded",
        table_name="feedback_cases",
    )
    op.drop_table("feedback_cases")
    op.drop_index(
        "ix_publication_outcomes_tenant_news_window",
        table_name="publication_outcomes",
    )
    op.drop_table("publication_outcomes")
