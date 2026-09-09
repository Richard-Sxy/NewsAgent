"""配置候选、离线评测、生产 Bundle 与人工决策 ORM 模型。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class ProductionBundleRecord(TimestampMixin, Base):
    __tablename__ = "production_bundles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_production_bundles_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "bundle_version",
            name="uq_production_bundles_tenant_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "derived_from_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_production_bundles_derived_scope",
            ondelete="RESTRICT",
        ),
        # source_candidate_id 的循环外键在迁移中于两张表创建后补充。
        Index(
            "uq_production_bundles_one_active_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_production_bundles_tenant_created",
            "tenant_id",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name="status_valid",
        ),
        CheckConstraint(
            "revision >= 1",
            name="revision_positive",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="sha256_valid",
        ),
        CheckConstraint(
            "(status = 'active' AND deactivated_at IS NULL) OR "
            "(status = 'inactive' AND deactivated_at IS NOT NULL)",
            name="lifecycle_valid",
        ),
        CheckConstraint(
            "activated_at >= created_at",
            name="activation_time_valid",
        ),
        CheckConstraint(
            "deactivated_at IS NULL OR deactivated_at >= activated_at",
            name="deactivation_time_valid",
        ),
        CheckConstraint(
            "(derived_from_bundle_id IS NULL AND source_candidate_id IS NULL) "
            "OR (derived_from_bundle_id IS NOT NULL "
            "AND source_candidate_id IS NOT NULL)",
            name="lineage_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bundle_version: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_definition_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    hot_score_policy_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    reranker_policy_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    analysis_prompt_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    fastgpt_app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    output_schema_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    validator_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    memory_resolver_policy_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    derived_from_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ConfigurationCandidateRecord(TimestampMixin, Base):
    __tablename__ = "configuration_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_configuration_candidates_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_version",
            name="uq_configuration_candidates_tenant_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_configuration_candidates_tenant_idempotency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "base_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_configuration_candidates_base_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_configuration_candidates_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('pending_evaluation', 'evaluation_passed', "
            "'evaluation_failed', 'approved', 'rejected', 'activated')",
            name="status_valid",
        ),
        CheckConstraint(
            "revision >= 1",
            name="revision_positive",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="sha256_valid",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="request_fingerprint_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(proposed_spec) = 'object'",
            name="proposed_spec_object",
        ),
        CheckConstraint(
            "jsonb_typeof(structured_diff) = 'array' "
            "AND jsonb_array_length(structured_diff) > 0",
            name="diff_nonempty",
        ),
        CheckConstraint(
            "(status IN ('approved', 'activated') "
            "AND approved_evaluation_run_id IS NOT NULL) OR "
            "(status NOT IN ('approved', 'activated') "
            "AND approved_evaluation_run_id IS NULL)",
            name="approval_link_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    candidate_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    proposed_spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    structured_diff: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending_evaluation", nullable=False
    )
    proposed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_evaluation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class CandidateEvaluationRunRecord(TimestampMixin, Base):
    __tablename__ = "candidate_evaluation_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_candidate_evaluation_runs_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_candidate_evaluation_runs_tenant_idempotency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            [
                "configuration_candidates.tenant_id",
                "configuration_candidates.id",
            ],
            name="fk_candidate_evaluation_runs_candidate_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "base_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_candidate_evaluation_runs_base_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "previous_experiment_candidate_id"],
            [
                "configuration_candidates.tenant_id",
                "configuration_candidates.id",
            ],
            name="fk_candidate_evaluation_runs_previous_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "golden_dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name="fk_candidate_evaluation_runs_golden_dataset_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "fresh_bad_case_dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name="fk_candidate_evaluation_runs_fresh_dataset_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "high_risk_dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name="fk_candidate_evaluation_runs_high_risk_dataset_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_candidate_evaluation_runs_candidate_created",
            "tenant_id",
            "candidate_id",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="artifact_sha256_valid",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="fingerprint_valid",
        ),
        CheckConstraint(
            "completed_at >= started_at",
            name="time_order_valid",
        ),
        CheckConstraint(
            "golden_dataset_id <> fresh_bad_case_dataset_id "
            "AND golden_dataset_id <> high_risk_dataset_id "
            "AND fresh_bad_case_dataset_id <> high_risk_dataset_id",
            name="datasets_distinct",
        ),
        CheckConstraint(
            "jsonb_typeof(suite_metrics) = 'object' "
            "AND jsonb_typeof(gate_policy) = 'object' "
            "AND jsonb_typeof(gate_decision) = 'object'",
            name="snapshots_object",
        ),
        CheckConstraint(
            "(status = 'completed' AND error_type IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND error_type IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="error_state_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    base_bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    previous_experiment_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    golden_dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    fresh_bad_case_dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    high_risk_dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    suite_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gate_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gate_decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class PromotionDecisionRecord(TimestampMixin, Base):
    __tablename__ = "promotion_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_promotion_decisions_tenant_idempotency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            [
                "configuration_candidates.tenant_id",
                "configuration_candidates.id",
            ],
            name="fk_promotion_decisions_candidate_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "evaluation_run_id"],
            [
                "candidate_evaluation_runs.tenant_id",
                "candidate_evaluation_runs.id",
            ],
            name="fk_promotion_decisions_evaluation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "from_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_promotion_decisions_from_bundle_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "to_bundle_id"],
            ["production_bundles.tenant_id", "production_bundles.id"],
            name="fk_promotion_decisions_to_bundle_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_promotion_decisions_tenant_created",
            "tenant_id",
            "created_at",
        ),
        CheckConstraint(
            "action IN ('approve', 'reject', 'activate', 'rollback')",
            name="action_valid",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="fingerprint_valid",
        ),
        CheckConstraint(
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
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    evaluation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    from_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    to_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


# 这两条约束形成有意的循环引用，必须在相关表全部声明后附加；Alembic
# 迁移同样会在建表完成后添加，保证审批记录和来源候选均不可悬空或跨租户。
ProductionBundleRecord.__table__.append_constraint(
    ForeignKeyConstraint(
        [
            ProductionBundleRecord.__table__.c.tenant_id,
            ProductionBundleRecord.__table__.c.source_candidate_id,
        ],
        [
            ConfigurationCandidateRecord.__table__.c.tenant_id,
            ConfigurationCandidateRecord.__table__.c.id,
        ],
        name="fk_production_bundles_source_candidate_scope",
        ondelete="RESTRICT",
        use_alter=True,
    )
)
ConfigurationCandidateRecord.__table__.append_constraint(
    ForeignKeyConstraint(
        [
            ConfigurationCandidateRecord.__table__.c.tenant_id,
            ConfigurationCandidateRecord.__table__.c.approved_evaluation_run_id,
        ],
        [
            CandidateEvaluationRunRecord.__table__.c.tenant_id,
            CandidateEvaluationRunRecord.__table__.c.id,
        ],
        name="fk_configuration_candidates_approved_evaluation_scope",
        ondelete="RESTRICT",
        use_alter=True,
    )
)
