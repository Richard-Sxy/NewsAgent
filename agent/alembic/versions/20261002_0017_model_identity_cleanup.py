"""Rename legacy provider identity fields without losing persisted data.

Revision ID: 20261002_0017
Revises: 20261002_0016
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20261002_0017"
down_revision = "20261002_0016"
branch_labels = None
depends_on = None


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _drop_mutation_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_production_bundle_lineage "
        "ON production_bundles"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_production_bundle_lineage()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_production_bundle_content_immutable "
        "ON production_bundles"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_production_bundle_content_update()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_configuration_candidate_content_immutable "
        "ON configuration_candidates"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_configuration_candidate_content_update()"
    )


def _create_mutation_guards(*, profile_column: str, profile_json_key: str) -> None:
    op.execute(
        f"""
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
               OR NEW.{profile_column} IS DISTINCT FROM OLD.{profile_column}
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
        f"""
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
                      AND candidate.proposed_spec ->> 'metric_definition_version' =
                          NEW.metric_definition_version
                      AND candidate.proposed_spec ->> 'hot_score_policy_version' =
                          NEW.hot_score_policy_version
                      AND candidate.proposed_spec ->> 'reranker_policy_version' =
                          NEW.reranker_policy_version
                      AND candidate.proposed_spec ->> 'analysis_prompt_version' =
                          NEW.analysis_prompt_version
                      AND candidate.proposed_spec ->> '{profile_json_key}' =
                          NEW.{profile_column}
                      AND candidate.proposed_spec ->> 'model_version' =
                          NEW.model_version
                      AND candidate.proposed_spec ->> 'output_schema_version' =
                          NEW.output_schema_version
                      AND candidate.proposed_spec ->> 'validator_version' =
                          NEW.validator_version
                      AND candidate.proposed_spec ->> 'memory_resolver_policy_version' =
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


def _rewrite_candidate_snapshots(
    *,
    old_profile_key: str,
    new_profile_key: str,
    old_asset: str,
    new_asset: str,
) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, tenant_id, base_bundle_id, candidate_version,
                   proposed_spec, structured_diff, proposed_by, proposal_reason
            FROM configuration_candidates
            """
        )
    ).mappings()
    for row in rows:
        proposed_spec = dict(row["proposed_spec"])
        if old_profile_key in proposed_spec:
            proposed_spec[new_profile_key] = proposed_spec.pop(old_profile_key)
        structured_diff = []
        for raw_change in row["structured_diff"]:
            change = dict(raw_change)
            if change.get("asset") == old_asset:
                change["asset"] = new_asset
            structured_diff.append(change)

        content = {
            "tenant_id": row["tenant_id"],
            "base_bundle_id": str(row["base_bundle_id"]),
            "candidate_version": row["candidate_version"],
            "proposed_spec": proposed_spec,
            "structured_diff": structured_diff,
        }
        command = {
            **content,
            "proposed_by": row["proposed_by"],
            "proposal_reason": row["proposal_reason"],
        }
        connection.execute(
            sa.text(
                """
                UPDATE configuration_candidates
                SET proposed_spec = :proposed_spec,
                    structured_diff = :structured_diff,
                    content_sha256 = :content_sha256,
                    request_fingerprint = :request_fingerprint
                WHERE id = :id
                """
            ).bindparams(
                sa.bindparam("proposed_spec", type_=sa.JSON),
                sa.bindparam("structured_diff", type_=sa.JSON),
            ),
            {
                "id": row["id"],
                "proposed_spec": proposed_spec,
                "structured_diff": structured_diff,
                "content_sha256": _canonical_sha256(content),
                "request_fingerprint": _canonical_sha256(command),
            },
        )


def _refresh_bundle_hashes(*, profile_column: str, profile_key: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            f"""
            SELECT id, metric_definition_version, hot_score_policy_version,
                   reranker_policy_version, analysis_prompt_version,
                   {profile_column} AS profile_id, model_version,
                   output_schema_version, validator_version,
                   memory_resolver_policy_version
            FROM production_bundles
            """
        )
    ).mappings()
    for row in rows:
        spec = {
            "metric_definition_version": row["metric_definition_version"],
            "hot_score_policy_version": row["hot_score_policy_version"],
            "reranker_policy_version": row["reranker_policy_version"],
            "analysis_prompt_version": row["analysis_prompt_version"],
            profile_key: row["profile_id"],
            "model_version": row["model_version"],
            "output_schema_version": row["output_schema_version"],
            "validator_version": row["validator_version"],
            "memory_resolver_policy_version": row[
                "memory_resolver_policy_version"
            ],
        }
        connection.execute(
            sa.text(
                "UPDATE production_bundles "
                "SET content_sha256 = :content_sha256 WHERE id = :id"
            ),
            {"id": row["id"], "content_sha256": _canonical_sha256(spec)},
        )


def upgrade() -> None:
    _drop_mutation_guards()
    op.alter_column(
        "production_bundles",
        "fastgpt_app_id",
        new_column_name="model_profile_id",
    )
    op.alter_column(
        "agent_runs",
        "fastgpt_app_id",
        new_column_name="model_profile_id",
    )
    op.alter_column(
        "agent_runs",
        "fastgpt_request_id",
        new_column_name="model_request_id",
    )
    _rewrite_candidate_snapshots(
        old_profile_key="fastgpt_app_id",
        new_profile_key="model_profile_id",
        old_asset="fastgpt_app",
        new_asset="model_profile",
    )
    _refresh_bundle_hashes(
        profile_column="model_profile_id",
        profile_key="model_profile_id",
    )
    _create_mutation_guards(
        profile_column="model_profile_id",
        profile_json_key="model_profile_id",
    )


def downgrade() -> None:
    _drop_mutation_guards()
    _rewrite_candidate_snapshots(
        old_profile_key="model_profile_id",
        new_profile_key="fastgpt_app_id",
        old_asset="model_profile",
        new_asset="fastgpt_app",
    )
    op.alter_column(
        "production_bundles",
        "model_profile_id",
        new_column_name="fastgpt_app_id",
    )
    op.alter_column(
        "agent_runs",
        "model_profile_id",
        new_column_name="fastgpt_app_id",
    )
    op.alter_column(
        "agent_runs",
        "model_request_id",
        new_column_name="fastgpt_request_id",
    )
    _refresh_bundle_hashes(
        profile_column="fastgpt_app_id",
        profile_key="fastgpt_app_id",
    )
    _create_mutation_guards(
        profile_column="fastgpt_app_id",
        profile_json_key="fastgpt_app_id",
    )
