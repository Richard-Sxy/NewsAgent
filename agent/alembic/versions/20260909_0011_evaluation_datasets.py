"""Create immutable evaluation datasets and case lineage indexes.

Revision ID: 20260909_0011
Revises: 20260909_0010
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260909_0011"
down_revision = "20260909_0010"
branch_labels = None
depends_on = None


DATASET_LAYER_CHECK = (
    "dataset_layer IN "
    "('golden', 'fresh_bad_case', 'high_risk_regression')"
)


def upgrade() -> None:
    op.create_table(
        "evaluation_datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("dataset_name", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("dataset_layer", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "source_cutoff_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "frozen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("frozen_by", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_size", sa.Integer(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("layer_counts", postgresql.JSONB(), nullable=False),
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
            DATASET_LAYER_CHECK,
            name=op.f("ck_evaluation_datasets_valid_dataset_layer"),
        ),
        sa.CheckConstraint(
            "status = 'frozen'",
            name=op.f("ck_evaluation_datasets_status_frozen"),
        ),
        sa.CheckConstraint(
            "case_count > 0",
            name=op.f("ck_evaluation_datasets_case_count_positive"),
        ),
        sa.CheckConstraint(
            "artifact_size > 0",
            name=op.f("ck_evaluation_datasets_artifact_size_positive"),
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_evaluation_datasets_content_sha256_valid"),
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_evaluation_datasets_request_fingerprint_valid"),
        ),
        sa.CheckConstraint(
            "frozen_at >= source_cutoff_at",
            name=op.f("ck_evaluation_datasets_valid_freeze_window"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(layer_counts) = 'object'",
            name=op.f("ck_evaluation_datasets_layer_counts_object"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_datasets"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_evaluation_datasets_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "dataset_name",
            "dataset_version",
            name="uq_evaluation_datasets_tenant_name_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_evaluation_datasets_tenant_idempotency_key",
        ),
    )
    op.create_index(
        "ix_evaluation_datasets_tenant_layer_frozen",
        "evaluation_datasets",
        ["tenant_id", "dataset_layer", "frozen_at"],
    )

    op.create_table(
        "evaluation_dataset_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "feedback_case_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(160), nullable=False),
        sa.Column("news_id", sa.String(128), nullable=False),
        sa.Column("dataset_layer", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("label_version", sa.Integer(), nullable=False),
        sa.Column(
            "analysis_input_snapshot",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "analysis_output_snapshot",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column("expected_label", postgresql.JSONB(), nullable=False),
        sa.Column("source_lineage", postgresql.JSONB(), nullable=False),
        sa.Column("case_content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            DATASET_LAYER_CHECK,
            name=op.f("ck_evaluation_dataset_cases_valid_dataset_layer"),
        ),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_evaluation_dataset_cases_position_nonnegative"),
        ),
        sa.CheckConstraint(
            "label_version >= 1",
            name=op.f("ck_evaluation_dataset_cases_label_version_positive"),
        ),
        sa.CheckConstraint(
            "case_content_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_evaluation_dataset_cases_content_sha256_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(analysis_input_snapshot) = 'object'",
            name=op.f("ck_evaluation_dataset_cases_input_snapshot_object"),
        ),
        sa.CheckConstraint(
            "analysis_output_snapshot IS NULL OR "
            "jsonb_typeof(analysis_output_snapshot) = 'object'",
            name=op.f("ck_evaluation_dataset_cases_output_snapshot_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_label) = 'object'",
            name=op.f("ck_evaluation_dataset_cases_expected_label_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_lineage) = 'object'",
            name=op.f("ck_evaluation_dataset_cases_source_lineage_object"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name=(
                "fk_evaluation_dataset_cases_tenant_dataset_"
                "evaluation_datasets"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "feedback_case_id"],
            ["feedback_cases.tenant_id", "feedback_cases.id"],
            name=(
                "fk_evaluation_dataset_cases_tenant_feedback_"
                "feedback_cases"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_dataset_cases"),
        sa.UniqueConstraint(
            "dataset_id",
            "feedback_case_id",
            name="uq_evaluation_dataset_cases_dataset_feedback",
        ),
        sa.UniqueConstraint(
            "dataset_id",
            "position",
            name="uq_evaluation_dataset_cases_dataset_position",
        ),
    )
    op.create_index(
        "ix_evaluation_dataset_cases_tenant_feedback",
        "evaluation_dataset_cases",
        ["tenant_id", "feedback_case_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evaluation_dataset_cases_tenant_feedback",
        table_name="evaluation_dataset_cases",
    )
    op.drop_table("evaluation_dataset_cases")
    op.drop_index(
        "ix_evaluation_datasets_tenant_layer_frozen",
        table_name="evaluation_datasets",
    )
    op.drop_table("evaluation_datasets")
