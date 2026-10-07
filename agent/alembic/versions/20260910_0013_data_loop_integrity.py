"""Harden Data Loop tenant scope, lineage, and immutable ledgers.

Revision ID: 20260910_0013
Revises: 20260909_0012
"""

from alembic import op
import sqlalchemy as sa


revision = "20260910_0013"
down_revision = "20260909_0012"
branch_labels = None
depends_on = None


DELETE_PROTECTED_TABLES = (
    "evaluation_datasets",
    "evaluation_dataset_cases",
    "candidate_evaluation_runs",
    "promotion_decisions",
    "production_bundles",
    "configuration_candidates",
)


def upgrade() -> None:
    op.alter_column(
        "analysis_runs",
        "production_bundle_version",
        existing_type=sa.String(length=120),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uq_analysis_runs_tenant_id",
        "analysis_runs",
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_analysis_runs_tenant_run_identity",
        "analysis_runs",
        ["tenant_id", "id", "idempotency_key"],
    )

    op.drop_constraint(
        "fk_publication_outcomes_run_id_analysis_runs",
        "publication_outcomes",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_publication_outcomes_tenant_run_identity",
        "publication_outcomes",
        "analysis_runs",
        ["tenant_id", "run_id", "run_idempotency_key"],
        ["tenant_id", "id", "idempotency_key"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "fk_feedback_cases_run_id_analysis_runs",
        "feedback_cases",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_feedback_cases_tenant_run_identity",
        "feedback_cases",
        "analysis_runs",
        ["tenant_id", "run_id", "run_idempotency_key"],
        ["tenant_id", "id", "idempotency_key"],
        ondelete="RESTRICT",
    )

    op.create_unique_constraint(
        "uq_hot_news_decisions_tenant_id",
        "hot_news_decisions",
        ["tenant_id", "id"],
    )
    op.drop_constraint(
        "fk_hot_news_decisions_run_id_analysis_runs",
        "hot_news_decisions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_hot_news_decisions_tenant_run_analysis_runs",
        "hot_news_decisions",
        "analysis_runs",
        ["tenant_id", "run_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "fk_hot_news_decisions_supersedes_decision_id_hot_news_decisions",
        "hot_news_decisions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_hot_news_decisions_tenant_supersedes",
        "hot_news_decisions",
        "hot_news_decisions",
        ["tenant_id", "supersedes_decision_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_check_constraint(
        "ck_feedback_labels_approval_separation_valid",
        "feedback_labels",
        "approved_by IS NULL OR approved_by <> labeled_by",
    )

    _create_immutable_ledger_triggers()
    _create_lineage_triggers()
    _validate_existing_lineage()


def _create_immutable_ledger_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION prevent_data_loop_row_delete()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only and cannot be deleted',
                TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in DELETE_PROTECTED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_delete_immutable
            BEFORE DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION prevent_data_loop_row_delete();
            """
        )

    op.execute(
        """
        CREATE FUNCTION prevent_frozen_dataset_update()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are frozen and cannot be updated',
                TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_evaluation_datasets_update_immutable
        BEFORE UPDATE ON evaluation_datasets
        FOR EACH ROW EXECUTE FUNCTION prevent_frozen_dataset_update();

        CREATE TRIGGER trg_evaluation_dataset_cases_update_immutable
        BEFORE UPDATE ON evaluation_dataset_cases
        FOR EACH ROW EXECUTE FUNCTION prevent_frozen_dataset_update();
        """
    )


def _create_lineage_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION validate_evaluation_dataset_case_lineage()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM evaluation_datasets AS dataset
                WHERE dataset.tenant_id = NEW.tenant_id
                  AND dataset.id = NEW.dataset_id
                  AND dataset.status = 'frozen'
                  AND dataset.dataset_layer = NEW.dataset_layer
            ) THEN
                RAISE EXCEPTION
                    'evaluation dataset case does not match its frozen parent layer'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_evaluation_dataset_case_lineage
        BEFORE INSERT ON evaluation_dataset_cases
        FOR EACH ROW EXECUTE FUNCTION validate_evaluation_dataset_case_lineage();
        """
    )

    op.execute(
        """
        CREATE FUNCTION validate_candidate_evaluation_lineage()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM configuration_candidates AS candidate
                WHERE candidate.tenant_id = NEW.tenant_id
                  AND candidate.id = NEW.candidate_id
                  AND candidate.base_bundle_id = NEW.base_bundle_id
            ) THEN
                RAISE EXCEPTION
                    'evaluation candidate and base bundle lineage do not match'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.previous_experiment_candidate_id IS NOT NULL
               AND NOT EXISTS (
                    SELECT 1
                    FROM configuration_candidates AS previous_candidate
                    WHERE previous_candidate.tenant_id = NEW.tenant_id
                      AND previous_candidate.id =
                          NEW.previous_experiment_candidate_id
                      AND previous_candidate.id <> NEW.candidate_id
                      AND previous_candidate.base_bundle_id = NEW.base_bundle_id
               ) THEN
                RAISE EXCEPTION
                    'previous experiment does not share the evaluation base bundle'
                    USING ERRCODE = '23514';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM evaluation_datasets AS dataset
                WHERE dataset.tenant_id = NEW.tenant_id
                  AND dataset.id = NEW.golden_dataset_id
                  AND dataset.status = 'frozen'
                  AND dataset.dataset_layer = 'golden'
            ) OR NOT EXISTS (
                SELECT 1 FROM evaluation_datasets AS dataset
                WHERE dataset.tenant_id = NEW.tenant_id
                  AND dataset.id = NEW.fresh_bad_case_dataset_id
                  AND dataset.status = 'frozen'
                  AND dataset.dataset_layer = 'fresh_bad_case'
            ) OR NOT EXISTS (
                SELECT 1 FROM evaluation_datasets AS dataset
                WHERE dataset.tenant_id = NEW.tenant_id
                  AND dataset.id = NEW.high_risk_dataset_id
                  AND dataset.status = 'frozen'
                  AND dataset.dataset_layer = 'high_risk_regression'
            ) THEN
                RAISE EXCEPTION
                    'evaluation requires correctly layered frozen datasets'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_candidate_evaluation_lineage
        BEFORE INSERT ON candidate_evaluation_runs
        FOR EACH ROW EXECUTE FUNCTION validate_candidate_evaluation_lineage();
        """
    )

    op.execute(
        """
        CREATE FUNCTION validate_candidate_approval_lineage()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.approved_evaluation_run_id IS NOT NULL
                   AND NEW.approved_evaluation_run_id IS DISTINCT FROM
                       OLD.approved_evaluation_run_id THEN
                    RAISE EXCEPTION
                        'approved evaluation identity is immutable once assigned'
                        USING ERRCODE = '55000';
                END IF;
            END IF;

            IF NEW.approved_evaluation_run_id IS NOT NULL
               AND NOT EXISTS (
                    SELECT 1
                    FROM candidate_evaluation_runs AS evaluation
                    WHERE evaluation.tenant_id = NEW.tenant_id
                      AND evaluation.id = NEW.approved_evaluation_run_id
                      AND evaluation.candidate_id = NEW.id
                      AND evaluation.base_bundle_id = NEW.base_bundle_id
                      AND evaluation.status = 'completed'
                      AND evaluation.gate_decision @>
                          '{"passed": true}'::jsonb
               ) THEN
                RAISE EXCEPTION
                    'approved evaluation does not belong to a passed candidate run'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_configuration_candidate_approval_lineage
        BEFORE INSERT OR UPDATE ON configuration_candidates
        FOR EACH ROW EXECUTE FUNCTION validate_candidate_approval_lineage();
        """
    )

    op.execute(
        """
        CREATE FUNCTION validate_production_bundle_lineage()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.derived_from_bundle_id IS NOT NULL
               AND NOT EXISTS (
                    SELECT 1
                    FROM configuration_candidates AS candidate
                    WHERE candidate.tenant_id = NEW.tenant_id
                      AND candidate.id = NEW.source_candidate_id
                      AND candidate.base_bundle_id = NEW.derived_from_bundle_id
                      AND candidate.candidate_version = NEW.bundle_version
                      AND candidate.approved_evaluation_run_id IS NOT NULL
                      AND candidate.status IN ('approved', 'activated')
                      AND candidate.proposed_spec ->>
                          'metric_definition_version' =
                          NEW.metric_definition_version
                      AND candidate.proposed_spec ->>
                          'hot_score_policy_version' =
                          NEW.hot_score_policy_version
                      AND candidate.proposed_spec ->>
                          'reranker_policy_version' =
                          NEW.reranker_policy_version
                      AND candidate.proposed_spec ->>
                          'analysis_prompt_version' =
                          NEW.analysis_prompt_version
                      AND candidate.proposed_spec ->> 'fastgpt_app_id' =
                          NEW.fastgpt_app_id
                      AND candidate.proposed_spec ->> 'model_version' =
                          NEW.model_version
                      AND candidate.proposed_spec ->>
                          'output_schema_version' =
                          NEW.output_schema_version
                      AND candidate.proposed_spec ->> 'validator_version' =
                          NEW.validator_version
                      AND candidate.proposed_spec ->>
                          'memory_resolver_policy_version' =
                          NEW.memory_resolver_policy_version
               ) THEN
                RAISE EXCEPTION
                    'production bundle does not match its approved candidate lineage'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_production_bundle_lineage
        BEFORE INSERT OR UPDATE ON production_bundles
        FOR EACH ROW EXECUTE FUNCTION validate_production_bundle_lineage();
        """
    )

    op.execute(
        """
        CREATE FUNCTION validate_promotion_decision_lineage()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.action = 'approve' AND NOT EXISTS (
                SELECT 1
                FROM configuration_candidates AS candidate
                JOIN candidate_evaluation_runs AS evaluation
                  ON evaluation.tenant_id = candidate.tenant_id
                 AND evaluation.candidate_id = candidate.id
                 AND evaluation.base_bundle_id = candidate.base_bundle_id
                WHERE candidate.tenant_id = NEW.tenant_id
                  AND candidate.id = NEW.candidate_id
                  AND candidate.proposed_by <> NEW.actor_id
                  AND candidate.approved_evaluation_run_id = NEW.evaluation_run_id
                  AND candidate.status = 'approved'
                  AND evaluation.id = NEW.evaluation_run_id
                  AND evaluation.status = 'completed'
                  AND evaluation.gate_decision @> '{"passed": true}'::jsonb
            ) THEN
                RAISE EXCEPTION 'approval decision lineage is invalid'
                    USING ERRCODE = '23514';
            ELSIF NEW.action = 'reject' AND NOT EXISTS (
                SELECT 1 FROM configuration_candidates AS candidate
                WHERE candidate.tenant_id = NEW.tenant_id
                  AND candidate.id = NEW.candidate_id
                  AND candidate.status = 'rejected'
            ) THEN
                RAISE EXCEPTION 'rejection decision lineage is invalid'
                    USING ERRCODE = '23514';
            ELSIF NEW.action = 'activate' AND NOT EXISTS (
                SELECT 1
                FROM configuration_candidates AS candidate
                JOIN candidate_evaluation_runs AS evaluation
                  ON evaluation.tenant_id = candidate.tenant_id
                 AND evaluation.candidate_id = candidate.id
                 AND evaluation.base_bundle_id = candidate.base_bundle_id
                JOIN production_bundles AS target_bundle
                  ON target_bundle.tenant_id = candidate.tenant_id
                 AND target_bundle.source_candidate_id = candidate.id
                 AND target_bundle.derived_from_bundle_id =
                     candidate.base_bundle_id
                WHERE candidate.tenant_id = NEW.tenant_id
                  AND candidate.id = NEW.candidate_id
                  AND candidate.base_bundle_id = NEW.from_bundle_id
                  AND candidate.approved_evaluation_run_id = NEW.evaluation_run_id
                  AND candidate.status = 'activated'
                  AND evaluation.id = NEW.evaluation_run_id
                  AND evaluation.status = 'completed'
                  AND evaluation.gate_decision @> '{"passed": true}'::jsonb
                  AND target_bundle.id = NEW.to_bundle_id
                  AND target_bundle.status = 'active'
            ) THEN
                RAISE EXCEPTION 'activation decision lineage is invalid'
                    USING ERRCODE = '23514';
            ELSIF NEW.action = 'rollback' AND (
                NEW.from_bundle_id = NEW.to_bundle_id
                OR NOT EXISTS (
                    SELECT 1 FROM production_bundles AS source_bundle
                    WHERE source_bundle.tenant_id = NEW.tenant_id
                      AND source_bundle.id = NEW.from_bundle_id
                      AND source_bundle.status = 'inactive'
                )
                OR NOT EXISTS (
                    SELECT 1 FROM production_bundles AS target_bundle
                    WHERE target_bundle.tenant_id = NEW.tenant_id
                      AND target_bundle.id = NEW.to_bundle_id
                      AND target_bundle.status = 'active'
                )
            ) THEN
                RAISE EXCEPTION 'rollback decision lineage is invalid'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_promotion_decision_lineage
        BEFORE INSERT ON promotion_decisions
        FOR EACH ROW EXECUTE FUNCTION validate_promotion_decision_lineage();
        """
    )


def _validate_existing_lineage() -> None:
    """Fail migration atomically instead of grandfathering corrupted rows."""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM evaluation_dataset_cases AS dataset_case
                LEFT JOIN evaluation_datasets AS dataset
                  ON dataset.tenant_id = dataset_case.tenant_id
                 AND dataset.id = dataset_case.dataset_id
                 AND dataset.status = 'frozen'
                 AND dataset.dataset_layer = dataset_case.dataset_layer
                WHERE dataset.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'existing evaluation dataset case lineage is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM candidate_evaluation_runs AS evaluation
                LEFT JOIN configuration_candidates AS candidate
                  ON candidate.tenant_id = evaluation.tenant_id
                 AND candidate.id = evaluation.candidate_id
                 AND candidate.base_bundle_id = evaluation.base_bundle_id
                LEFT JOIN evaluation_datasets AS golden
                  ON golden.tenant_id = evaluation.tenant_id
                 AND golden.id = evaluation.golden_dataset_id
                 AND golden.status = 'frozen'
                 AND golden.dataset_layer = 'golden'
                LEFT JOIN evaluation_datasets AS fresh
                  ON fresh.tenant_id = evaluation.tenant_id
                 AND fresh.id = evaluation.fresh_bad_case_dataset_id
                 AND fresh.status = 'frozen'
                 AND fresh.dataset_layer = 'fresh_bad_case'
                LEFT JOIN evaluation_datasets AS high_risk
                  ON high_risk.tenant_id = evaluation.tenant_id
                 AND high_risk.id = evaluation.high_risk_dataset_id
                 AND high_risk.status = 'frozen'
                 AND high_risk.dataset_layer = 'high_risk_regression'
                LEFT JOIN configuration_candidates AS previous_candidate
                  ON previous_candidate.tenant_id = evaluation.tenant_id
                 AND previous_candidate.id =
                     evaluation.previous_experiment_candidate_id
                 AND previous_candidate.id <> evaluation.candidate_id
                 AND previous_candidate.base_bundle_id =
                     evaluation.base_bundle_id
                WHERE candidate.id IS NULL
                   OR golden.id IS NULL
                   OR fresh.id IS NULL
                   OR high_risk.id IS NULL
                   OR (
                        evaluation.previous_experiment_candidate_id IS NOT NULL
                        AND previous_candidate.id IS NULL
                   )
            ) THEN
                RAISE EXCEPTION 'existing candidate evaluation lineage is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM configuration_candidates AS candidate
                LEFT JOIN candidate_evaluation_runs AS evaluation
                  ON evaluation.tenant_id = candidate.tenant_id
                 AND evaluation.id = candidate.approved_evaluation_run_id
                 AND evaluation.candidate_id = candidate.id
                 AND evaluation.base_bundle_id = candidate.base_bundle_id
                 AND evaluation.status = 'completed'
                 AND evaluation.gate_decision @> '{"passed": true}'::jsonb
                WHERE candidate.approved_evaluation_run_id IS NOT NULL
                  AND evaluation.id IS NULL
            ) THEN
                RAISE EXCEPTION 'existing candidate approval lineage is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM production_bundles AS bundle
                LEFT JOIN configuration_candidates AS candidate
                  ON candidate.tenant_id = bundle.tenant_id
                 AND candidate.id = bundle.source_candidate_id
                 AND candidate.base_bundle_id = bundle.derived_from_bundle_id
                 AND candidate.candidate_version = bundle.bundle_version
                 AND candidate.approved_evaluation_run_id IS NOT NULL
                 AND candidate.proposed_spec ->> 'metric_definition_version' =
                     bundle.metric_definition_version
                 AND candidate.proposed_spec ->> 'hot_score_policy_version' =
                     bundle.hot_score_policy_version
                 AND candidate.proposed_spec ->> 'reranker_policy_version' =
                     bundle.reranker_policy_version
                 AND candidate.proposed_spec ->> 'analysis_prompt_version' =
                     bundle.analysis_prompt_version
                 AND candidate.proposed_spec ->> 'fastgpt_app_id' =
                     bundle.fastgpt_app_id
                 AND candidate.proposed_spec ->> 'model_version' =
                     bundle.model_version
                 AND candidate.proposed_spec ->> 'output_schema_version' =
                     bundle.output_schema_version
                 AND candidate.proposed_spec ->> 'validator_version' =
                     bundle.validator_version
                 AND candidate.proposed_spec ->>
                     'memory_resolver_policy_version' =
                     bundle.memory_resolver_policy_version
                WHERE bundle.derived_from_bundle_id IS NOT NULL
                  AND candidate.id IS NULL
            ) THEN
                RAISE EXCEPTION 'existing production bundle lineage is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM promotion_decisions AS decision
                WHERE (
                    decision.action = 'approve'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM configuration_candidates AS candidate
                        JOIN candidate_evaluation_runs AS evaluation
                          ON evaluation.tenant_id = candidate.tenant_id
                         AND evaluation.candidate_id = candidate.id
                         AND evaluation.base_bundle_id =
                             candidate.base_bundle_id
                        WHERE candidate.tenant_id = decision.tenant_id
                          AND candidate.id = decision.candidate_id
                          AND candidate.proposed_by <> decision.actor_id
                          AND candidate.approved_evaluation_run_id =
                              decision.evaluation_run_id
                          AND evaluation.id = decision.evaluation_run_id
                          AND evaluation.status = 'completed'
                          AND evaluation.gate_decision @>
                              '{"passed": true}'::jsonb
                    )
                ) OR (
                    decision.action = 'reject'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM configuration_candidates AS candidate
                        WHERE candidate.tenant_id = decision.tenant_id
                          AND candidate.id = decision.candidate_id
                    )
                ) OR (
                    decision.action = 'activate'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM configuration_candidates AS candidate
                        JOIN candidate_evaluation_runs AS evaluation
                          ON evaluation.tenant_id = candidate.tenant_id
                         AND evaluation.candidate_id = candidate.id
                         AND evaluation.base_bundle_id =
                             candidate.base_bundle_id
                        JOIN production_bundles AS target_bundle
                          ON target_bundle.tenant_id = candidate.tenant_id
                         AND target_bundle.source_candidate_id = candidate.id
                         AND target_bundle.derived_from_bundle_id =
                             candidate.base_bundle_id
                        WHERE candidate.tenant_id = decision.tenant_id
                          AND candidate.id = decision.candidate_id
                          AND candidate.base_bundle_id =
                              decision.from_bundle_id
                          AND candidate.approved_evaluation_run_id =
                              decision.evaluation_run_id
                          AND evaluation.id = decision.evaluation_run_id
                          AND evaluation.status = 'completed'
                          AND evaluation.gate_decision @>
                              '{"passed": true}'::jsonb
                          AND target_bundle.id = decision.to_bundle_id
                    )
                ) OR (
                    decision.action = 'rollback'
                    AND (
                        decision.from_bundle_id = decision.to_bundle_id
                        OR NOT EXISTS (
                            SELECT 1 FROM production_bundles AS source_bundle
                            WHERE source_bundle.tenant_id = decision.tenant_id
                              AND source_bundle.id = decision.from_bundle_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM production_bundles AS target_bundle
                            WHERE target_bundle.tenant_id = decision.tenant_id
                              AND target_bundle.id = decision.to_bundle_id
                        )
                    )
                )
            ) THEN
                RAISE EXCEPTION 'existing promotion decision lineage is invalid';
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_feedback_labels_approval_separation_valid",
        "feedback_labels",
        type_="check",
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_promotion_decision_lineage "
        "ON promotion_decisions"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_promotion_decision_lineage()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_production_bundle_lineage "
        "ON production_bundles"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_production_bundle_lineage()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_configuration_candidate_approval_lineage "
        "ON configuration_candidates"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_candidate_approval_lineage()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_candidate_evaluation_lineage "
        "ON candidate_evaluation_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_candidate_evaluation_lineage()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_dataset_case_lineage "
        "ON evaluation_dataset_cases"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS validate_evaluation_dataset_case_lineage()"
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_dataset_cases_update_immutable "
        "ON evaluation_dataset_cases"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_datasets_update_immutable "
        "ON evaluation_datasets"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_frozen_dataset_update()")
    for table_name in reversed(DELETE_PROTECTED_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_delete_immutable "
            f"ON {table_name}"
        )
    op.execute("DROP FUNCTION IF EXISTS prevent_data_loop_row_delete()")

    op.drop_constraint(
        "fk_hot_news_decisions_tenant_supersedes",
        "hot_news_decisions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_hot_news_decisions_supersedes_decision_id_hot_news_decisions",
        "hot_news_decisions",
        "hot_news_decisions",
        ["supersedes_decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "fk_hot_news_decisions_tenant_run_analysis_runs",
        "hot_news_decisions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_hot_news_decisions_run_id_analysis_runs",
        "hot_news_decisions",
        "analysis_runs",
        ["run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_hot_news_decisions_tenant_id",
        "hot_news_decisions",
        type_="unique",
    )

    op.drop_constraint(
        "fk_feedback_cases_tenant_run_identity",
        "feedback_cases",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_feedback_cases_run_id_analysis_runs",
        "feedback_cases",
        "analysis_runs",
        ["run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "fk_publication_outcomes_tenant_run_identity",
        "publication_outcomes",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_publication_outcomes_run_id_analysis_runs",
        "publication_outcomes",
        "analysis_runs",
        ["run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_analysis_runs_tenant_run_identity",
        "analysis_runs",
        type_="unique",
    )
    op.drop_constraint(
        "uq_analysis_runs_tenant_id",
        "analysis_runs",
        type_="unique",
    )
    op.alter_column(
        "analysis_runs",
        "production_bundle_version",
        existing_type=sa.String(length=128),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
