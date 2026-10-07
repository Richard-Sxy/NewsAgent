"""Create configuration candidate evaluation and production bundle ledger.

Revision ID: 20260909_0012
Revises: 20260909_0011
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260909_0012"
down_revision = "20260909_0011"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, ...]:
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
        "production_bundles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("bundle_version", sa.String(128), nullable=False),
        sa.Column(
            "metric_definition_version", sa.String(128), nullable=False
        ),
        sa.Column(
            "hot_score_policy_version", sa.String(128), nullable=False
        ),
        sa.Column(
            "reranker_policy_version", sa.String(128), nullable=False
        ),
        sa.Column(
            "analysis_prompt_version", sa.String(128), nullable=False
        ),
        sa.Column("fastgpt_app_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column(
            "output_schema_version", sa.String(128), nullable=False
        ),
        sa.Column("validator_version", sa.String(128), nullable=False),
        sa.Column(
            "memory_resolver_policy_version", sa.String(128), nullable=False
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "derived_from_bundle_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "source_candidate_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("activated_by", sa.String(128), nullable=False),
        sa.Column(
            "activated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "deactivated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="revision_positive",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="sha256_valid",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND deactivated_at IS NULL) OR "
            "(status = 'inactive' AND deactivated_at IS NOT NULL)",
            name="lifecycle_valid",
        ),
        sa.CheckConstraint(
            "activated_at >= created_at",
            name="activation_time_valid",
        ),
        sa.CheckConstraint(
            "deactivated_at IS NULL OR deactivated_at >= activated_at",
            name="deactivation_time_valid",
        ),
        sa.CheckConstraint(
            "(derived_from_bundle_id IS NULL AND source_candidate_id IS NULL) "
            "OR (derived_from_bundle_id IS NOT NULL "
            "AND source_candidate_id IS NOT NULL)",
            name="lineage_complete",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "derived_from_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_production_bundles_derived_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_production_bundles"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_production_bundles_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "bundle_version",
            name="uq_production_bundles_tenant_version",
        ),
    )
    op.create_index(
        "uq_production_bundles_one_active_per_tenant",
        "production_bundles",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_production_bundles_tenant_created",
        "production_bundles",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "configuration_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "base_bundle_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("candidate_version", sa.String(128), nullable=False),
        sa.Column("proposed_spec", postgresql.JSONB(), nullable=False),
        sa.Column("structured_diff", postgresql.JSONB(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("proposed_by", sa.String(128), nullable=False),
        sa.Column("proposal_reason", sa.String(500), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "approved_evaluation_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending_evaluation', 'evaluation_passed', "
            "'evaluation_failed', 'approved', 'rejected', 'activated')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="revision_positive",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="sha256_valid",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="request_fingerprint_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(proposed_spec) = 'object'",
            name="proposed_spec_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(structured_diff) = 'array' "
            "AND jsonb_array_length(structured_diff) > 0",
            name="diff_nonempty",
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'activated') "
            "AND approved_evaluation_run_id IS NOT NULL) OR "
            "(status NOT IN ('approved', 'activated') "
            "AND approved_evaluation_run_id IS NULL)",
            name="approval_link_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "base_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_configuration_candidates_base_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_configuration_candidates"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_configuration_candidates_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_version",
            name="uq_configuration_candidates_tenant_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_configuration_candidates_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_configuration_candidates_tenant_status_created",
        "configuration_candidates",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "candidate_evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "candidate_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "base_bundle_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "previous_experiment_candidate_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "golden_dataset_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "fresh_bad_case_dataset_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "high_risk_dataset_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("suite_metrics", postgresql.JSONB(), nullable=False),
        sa.Column("gate_policy", postgresql.JSONB(), nullable=False),
        sa.Column("gate_decision", postgresql.JSONB(), nullable=False),
        sa.Column("evaluator_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("artifact_uri", sa.String(500), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="artifact_sha256_valid",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="fingerprint_valid",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at",
            name="time_order_valid",
        ),
        sa.CheckConstraint(
            "golden_dataset_id <> fresh_bad_case_dataset_id "
            "AND golden_dataset_id <> high_risk_dataset_id "
            "AND fresh_bad_case_dataset_id <> high_risk_dataset_id",
            name="datasets_distinct",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(suite_metrics) = 'object' "
            "AND jsonb_typeof(gate_policy) = 'object' "
            "AND jsonb_typeof(gate_decision) = 'object'",
            name="snapshots_object",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND error_type IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND error_type IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="error_state_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            [
                "configuration_candidates.tenant_id",
                "configuration_candidates.id",
            ],
            name="fk_candidate_evaluation_runs_candidate_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "base_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_candidate_evaluation_runs_base_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "previous_experiment_candidate_id"],
            [
                "configuration_candidates.tenant_id",
                "configuration_candidates.id",
            ],
            name="fk_candidate_evaluation_runs_previous_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "golden_dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name="fk_candidate_evaluation_runs_golden_dataset_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "fresh_bad_case_dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name="fk_candidate_evaluation_runs_fresh_dataset_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "high_risk_dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name="fk_candidate_evaluation_runs_high_risk_dataset_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_candidate_evaluation_runs"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_candidate_evaluation_runs_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_candidate_evaluation_runs_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_candidate_evaluation_runs_candidate_created",
        "candidate_evaluation_runs",
        ["tenant_id", "candidate_id", "created_at"],
    )

    op.create_foreign_key(
        "fk_configuration_candidates_approved_evaluation_scope",
        "configuration_candidates",
        "candidate_evaluation_runs",
        ["tenant_id", "approved_evaluation_run_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_production_bundles_source_candidate_scope",
        "production_bundles",
        "configuration_candidates",
        ["tenant_id", "source_candidate_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "promotion_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column(
            "candidate_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "evaluation_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "from_bundle_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "to_bundle_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "action IN ('approve', 'reject', 'activate', 'rollback')",
            name="action_valid",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="fingerprint_valid",
        ),
        sa.CheckConstraint(
            "(action = 'approve' AND candidate_id IS NOT NULL "
            "AND evaluation_run_id IS NOT NULL "
            "AND from_bundle_id IS NULL AND to_bundle_id IS NULL) OR "
            "(action = 'reject' AND candidate_id IS NOT NULL "
            "AND evaluation_run_id IS NULL "
            "AND from_bundle_id IS NULL AND to_bundle_id IS NULL) OR "
            "(action = 'activate' AND candidate_id IS NOT NULL "
            "AND evaluation_run_id IS NOT NULL "
            "AND from_bundle_id IS NOT NULL AND to_bundle_id IS NOT NULL) OR "
            "(action = 'rollback' AND candidate_id IS NULL "
            "AND evaluation_run_id IS NULL "
            "AND from_bundle_id IS NOT NULL AND to_bundle_id IS NOT NULL)",
            name="action_links_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            [
                "configuration_candidates.tenant_id",
                "configuration_candidates.id",
            ],
            name="fk_promotion_decisions_candidate_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evaluation_run_id"],
            [
                "candidate_evaluation_runs.tenant_id",
                "candidate_evaluation_runs.id",
            ],
            name="fk_promotion_decisions_evaluation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "from_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_promotion_decisions_from_bundle_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "to_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_promotion_decisions_to_bundle_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promotion_decisions"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_promotion_decisions_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_promotion_decisions_tenant_created",
        "promotion_decisions",
        ["tenant_id", "created_at"],
    )

    _create_immutability_triggers()


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION prevent_production_bundle_content_update()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.bundle_version IS DISTINCT FROM OLD.bundle_version
               OR NEW.metric_definition_version IS DISTINCT FROM OLD.metric_definition_version
               OR NEW.hot_score_policy_version IS DISTINCT FROM OLD.hot_score_policy_version
               OR NEW.reranker_policy_version IS DISTINCT FROM OLD.reranker_policy_version
               OR NEW.analysis_prompt_version IS DISTINCT FROM OLD.analysis_prompt_version
               OR NEW.fastgpt_app_id IS DISTINCT FROM OLD.fastgpt_app_id
               OR NEW.model_version IS DISTINCT FROM OLD.model_version
               OR NEW.output_schema_version IS DISTINCT FROM OLD.output_schema_version
               OR NEW.validator_version IS DISTINCT FROM OLD.validator_version
               OR NEW.memory_resolver_policy_version IS DISTINCT FROM OLD.memory_resolver_policy_version
               OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
               OR NEW.derived_from_bundle_id IS DISTINCT FROM OLD.derived_from_bundle_id
               OR NEW.source_candidate_id IS DISTINCT FROM OLD.source_candidate_id
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'production bundle content is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_production_bundle_content_immutable
        BEFORE UPDATE ON production_bundles
        FOR EACH ROW EXECUTE FUNCTION prevent_production_bundle_content_update();
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_configuration_candidate_content_update()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.base_bundle_id IS DISTINCT FROM OLD.base_bundle_id
               OR NEW.candidate_version IS DISTINCT FROM OLD.candidate_version
               OR NEW.proposed_spec IS DISTINCT FROM OLD.proposed_spec
               OR NEW.structured_diff IS DISTINCT FROM OLD.structured_diff
               OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
               OR NEW.proposed_by IS DISTINCT FROM OLD.proposed_by
               OR NEW.proposal_reason IS DISTINCT FROM OLD.proposal_reason
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'configuration candidate content is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_configuration_candidate_content_immutable
        BEFORE UPDATE ON configuration_candidates
        FOR EACH ROW EXECUTE FUNCTION prevent_configuration_candidate_content_update();
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_data_loop_ledger_update()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_candidate_evaluation_runs_immutable
        BEFORE UPDATE ON candidate_evaluation_runs
        FOR EACH ROW EXECUTE FUNCTION prevent_data_loop_ledger_update();

        CREATE TRIGGER trg_promotion_decisions_immutable
        BEFORE UPDATE ON promotion_decisions
        FOR EACH ROW EXECUTE FUNCTION prevent_data_loop_ledger_update();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_promotion_decisions_immutable "
        "ON promotion_decisions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_candidate_evaluation_runs_immutable "
        "ON candidate_evaluation_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_data_loop_ledger_update()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_configuration_candidate_content_immutable "
        "ON configuration_candidates"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_configuration_candidate_content_update()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_production_bundle_content_immutable "
        "ON production_bundles"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_production_bundle_content_update()"
    )

    op.drop_index(
        "ix_promotion_decisions_tenant_created",
        table_name="promotion_decisions",
    )
    op.drop_table("promotion_decisions")
    op.drop_constraint(
        "fk_production_bundles_source_candidate_scope",
        "production_bundles",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_configuration_candidates_approved_evaluation_scope",
        "configuration_candidates",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_candidate_evaluation_runs_candidate_created",
        table_name="candidate_evaluation_runs",
    )
    op.drop_table("candidate_evaluation_runs")
    op.drop_index(
        "ix_configuration_candidates_tenant_status_created",
        table_name="configuration_candidates",
    )
    op.drop_table("configuration_candidates")
    op.drop_index(
        "ix_production_bundles_tenant_created",
        table_name="production_bundles",
    )
    op.drop_index(
        "uq_production_bundles_one_active_per_tenant",
        table_name="production_bundles",
    )
    op.drop_table("production_bundles")
