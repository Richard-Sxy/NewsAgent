"""Data Loop Feedback Case 与人工标签的 PostgreSQL ORM。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class PublicationOutcomeRecord(TimestampMixin, Base):
    """新闻发布后窗口级聚合效果，不保存任何用户明细。"""

    __tablename__ = "publication_outcomes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_idempotency_key"],
            [
                "analysis_runs.tenant_id",
                "analysis_runs.id",
                "analysis_runs.idempotency_key",
            ],
            name="fk_publication_outcomes_tenant_run_identity",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_publication_outcomes_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_publication_outcomes_tenant_id",
        ),
        Index(
            "ix_publication_outcomes_tenant_news_window",
            "tenant_id",
            "news_id",
            "window_start",
            "window_end",
        ),
        CheckConstraint(
            "window_start < window_end",
            name="window_valid",
        ),
        CheckConstraint(
            "window_end <= recorded_at",
            name="recording_order_valid",
        ),
        CheckConstraint(
            "impressions >= 0 AND clicks >= 0 AND unique_users >= 0 "
            "AND effective_consumptions >= 0 AND interactions >= 0 "
            "AND complaints >= 0 AND corrections >= 0",
            name="metrics_nonnegative",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="content_sha256_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    run_idempotency_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    news_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_publication_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_definition_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    unique_users: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_consumptions: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    interactions: Mapped[int] = mapped_column(Integer, nullable=False)
    complaints: Mapped[int] = mapped_column(Integer, nullable=False)
    corrections: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class AnalysisFeedbackCaseRecord(TimestampMixin, Base):
    """从线上结果收集的可审计反馈 Case。"""

    __tablename__ = "feedback_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_idempotency_key"],
            [
                "analysis_runs.tenant_id",
                "analysis_runs.id",
                "analysis_runs.idempotency_key",
            ],
            name="fk_feedback_cases_tenant_run_identity",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_feedback_cases_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_feedback_cases_tenant_id",
        ),
        Index(
            "ix_feedback_cases_tenant_status_recorded",
            "tenant_id",
            "status",
            "recorded_at",
        ),
        Index(
            "ix_feedback_cases_tenant_run_news",
            "tenant_id",
            "run_idempotency_key",
            "news_id",
        ),
        CheckConstraint(
            "source_type IN ('operator_rejected', 'operator_corrected', "
            "'validation_failed', 'low_confidence', 'retrieval_error', "
            "'post_publish_outcome')",
            name="source_type_valid",
        ),
        CheckConstraint(
            "status IN ('collected', 'needs_label', 'labeled', "
            "'excluded', 'frozen')",
            name="status_valid",
        ),
        CheckConstraint(
            "problem_type IN ('analysis_incorrect', 'unsupported_claim', "
            "'metric_mismatch', 'evidence_mismatch', 'missing_evidence', "
            "'low_confidence', 'retrieval_miss', "
            "'retrieval_false_positive', 'schema_violation', "
            "'policy_violation', 'outcome_underperformance', 'other')",
            name="problem_type_valid",
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="severity_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(analysis_input_snapshot) = 'object'",
            name="input_snapshot_object",
        ),
        CheckConstraint(
            "analysis_output_snapshot IS NULL OR "
            "jsonb_typeof(analysis_output_snapshot) = 'object'",
            name="output_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(source_reference) = 'object'",
            name="source_reference_object",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="content_sha256_valid",
        ),
        CheckConstraint(
            "occurred_at <= recorded_at",
            name="recording_order_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    run_idempotency_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    news_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    problem_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="needs_label", nullable=False
    )
    severity: Mapped[str] = mapped_column(
        String(16), default="medium", nullable=False
    )
    production_bundle_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    analysis_input_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    analysis_output_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    source_reference: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )


class AnalysisFeedbackLabelRecord(TimestampMixin, Base):
    """Feedback Case 的版本化人工标签。"""

    __tablename__ = "feedback_labels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "feedback_case_id"],
            ["feedback_cases.tenant_id", "feedback_cases.id"],
            name="fk_feedback_labels_case_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "feedback_case_id",
            "label_version",
            name="uq_feedback_labels_case_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_feedback_labels_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "approval_idempotency_key",
            name="uq_feedback_labels_tenant_approval_idempotency_key",
        ),
        Index(
            "ix_feedback_labels_tenant_case_version",
            "tenant_id",
            "feedback_case_id",
            "label_version",
        ),
        CheckConstraint(
            "label_version >= 1",
            name="label_version_positive",
        ),
        CheckConstraint(
            "verdict IN ('correct', 'incorrect', 'partially_correct', "
            "'not_evaluable')",
            name="verdict_valid",
        ),
        CheckConstraint(
            "approval_status IN ('pending', 'approved', 'rejected', "
            "'superseded')",
            name="approval_status_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_dominant_drivers) = 'array' AND "
            "jsonb_typeof(required_evidence_news_ids) = 'array' AND "
            "jsonb_typeof(forbidden_evidence_news_ids) = 'array' AND "
            "jsonb_typeof(required_metric_keys) = 'array'",
            name="label_collections_array",
        ),
        CheckConstraint(
            "(approval_status = 'approved' AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL "
            "AND approval_idempotency_key IS NOT NULL) OR "
            "(approval_status IN ('pending', 'rejected') "
            "AND approved_by IS NULL AND approved_at IS NULL "
            "AND approval_idempotency_key IS NULL) OR "
            "approval_status = 'superseded'",
            name="approval_metadata_valid",
        ),
        CheckConstraint(
            "approved_at IS NULL OR approved_at >= labeled_at",
            name="approval_order_valid",
        ),
        CheckConstraint(
            "approved_by IS NULL OR approved_by <> labeled_by",
            name="approval_separation_valid",
        ),
        CheckConstraint(
            "labeled_at <= recorded_at",
            name="label_recording_order_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    feedback_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    label_version: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed_dominant_drivers: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False
    )
    required_evidence_news_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False
    )
    forbidden_evidence_news_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False
    )
    required_metric_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False
    )
    must_state_limitation: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )
    operator_comment: Mapped[str] = mapped_column(Text, nullable=False)
    approval_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    labeled_by: Mapped[str] = mapped_column(String(128), nullable=False)
    labeled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    approval_idempotency_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
