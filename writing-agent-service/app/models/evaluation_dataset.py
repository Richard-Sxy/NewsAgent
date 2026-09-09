"""不可变评测数据集的 PostgreSQL ORM 模型。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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


DATASET_LAYER_SQL = (
    "dataset_layer IN "
    "('golden', 'fresh_bad_case', 'high_risk_regression')"
)
SHA256_SQL = "content_sha256 ~ '^[0-9a-f]{64}$'"
CASE_SHA256_SQL = "case_content_sha256 ~ '^[0-9a-f]{64}$'"
REQUEST_SHA256_SQL = "request_fingerprint ~ '^[0-9a-f]{64}$'"


class EvaluationDatasetRecord(TimestampMixin, Base):
    """一个已冻结、内容寻址的评测数据集。"""

    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_evaluation_datasets_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "dataset_name",
            "dataset_version",
            name="uq_evaluation_datasets_tenant_name_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_evaluation_datasets_tenant_idempotency_key",
        ),
        Index(
            "ix_evaluation_datasets_tenant_layer_frozen",
            "tenant_id",
            "dataset_layer",
            "frozen_at",
        ),
        CheckConstraint(DATASET_LAYER_SQL, name="valid_dataset_layer"),
        CheckConstraint("status = 'frozen'", name="status_frozen"),
        CheckConstraint("case_count > 0", name="case_count_positive"),
        CheckConstraint("artifact_size > 0", name="artifact_size_positive"),
        CheckConstraint(SHA256_SQL, name="content_sha256_valid"),
        CheckConstraint(REQUEST_SHA256_SQL, name="request_fingerprint_valid"),
        CheckConstraint(
            "jsonb_typeof(layer_counts) = 'object'",
            name="layer_counts_object",
        ),
        CheckConstraint(
            "frozen_at >= source_cutoff_at",
            name="valid_freeze_window",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_layer: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="frozen",
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(
        String(32),
        default="1.0",
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_cutoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    frozen_by: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_size: Mapped[int] = mapped_column(Integer, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    layer_counts: Mapped[dict] = mapped_column(JSONB, nullable=False)


class EvaluationDatasetCaseRecord(Base):
    """数据库中的可查询 Case 索引；Artifact 仍是完整冻结快照。"""

    __tablename__ = "evaluation_dataset_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "dataset_id"],
            ["evaluation_datasets.tenant_id", "evaluation_datasets.id"],
            name=(
                "fk_evaluation_dataset_cases_tenant_dataset_"
                "evaluation_datasets"
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "feedback_case_id"],
            ["feedback_cases.tenant_id", "feedback_cases.id"],
            name=(
                "fk_evaluation_dataset_cases_tenant_feedback_"
                "feedback_cases"
            ),
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "dataset_id",
            "feedback_case_id",
            name="uq_evaluation_dataset_cases_dataset_feedback",
        ),
        UniqueConstraint(
            "dataset_id",
            "position",
            name="uq_evaluation_dataset_cases_dataset_position",
        ),
        Index(
            "ix_evaluation_dataset_cases_tenant_feedback",
            "tenant_id",
            "feedback_case_id",
        ),
        CheckConstraint(DATASET_LAYER_SQL, name="valid_dataset_layer"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint("label_version >= 1", name="label_version_positive"),
        CheckConstraint(CASE_SHA256_SQL, name="content_sha256_valid"),
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
            "jsonb_typeof(expected_label) = 'object'",
            name="expected_label_object",
        ),
        CheckConstraint(
            "jsonb_typeof(source_lineage) = 'object'",
            name="source_lineage_object",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    feedback_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    case_id: Mapped[str] = mapped_column(String(160), nullable=False)
    news_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_layer: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    label_version: Mapped[int] = mapped_column(Integer, nullable=False)
    analysis_input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    analysis_output_snapshot: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    expected_label: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_lineage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    case_content_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
