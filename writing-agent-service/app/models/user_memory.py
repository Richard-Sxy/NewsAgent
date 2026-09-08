"""用户短期记忆、长期候选、长期记忆与晋升请求的 ORM 模型。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class MemoryScopeColumns:
    """四类记录共用的可信用户及组织作用域字段。"""

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    section_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ShortTermUserMemoryRecord(MemoryScopeColumns, TimestampMixin, Base):
    """有明确任务边界和过期时间的短期记忆。"""

    __tablename__ = "short_term_user_memories"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_short_term_user_memories_tenant_idempotency_key",
        ),
        Index(
            "ix_short_term_user_memories_active_lookup",
            "tenant_id",
            "user_id",
            "task_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_short_term_user_memories_user_key",
            "tenant_id",
            "user_id",
            "memory_key",
        ),
        CheckConstraint(
            "memory_kind IN ('task_goal', 'pending_item', "
            "'temporary_preference', 'stable_preference', "
            "'role_responsibility', 'user_constraint')",
            name="memory_kind_valid",
        ),
        CheckConstraint(
            "origin IN ('explicit_user', 'system_inference')",
            name="origin_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'revoked')",
            name="status_valid",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("expires_at > created_at", name="valid_window"),
        CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' "
            "AND jsonb_array_length(source_refs) > 0",
            name="source_refs_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_value: Mapped[Any] = mapped_column(
        JSONB(none_as_null=False), nullable=False
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="active", nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)


class LongTermMemoryCandidateRecord(
    MemoryScopeColumns, TimestampMixin, Base
):
    """等待显式审核的长期记忆候选。"""

    __tablename__ = "long_term_memory_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_long_term_memory_candidates_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "id",
            name="uq_long_term_candidates_tenant_user_id",
        ),
        Index(
            "ix_long_term_memory_candidates_pending_lookup",
            "tenant_id",
            "user_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_long_term_memory_candidates_user_key",
            "tenant_id",
            "user_id",
            "proposed_memory_key",
        ),
        CheckConstraint(
            "proposed_memory_kind IN ('task_goal', 'pending_item', "
            "'temporary_preference', 'stable_preference', "
            "'role_responsibility', 'user_constraint')",
            name="memory_kind_valid",
        ),
        CheckConstraint(
            "origin IN ('explicit_user', 'trusted_identity', "
            "'system_inference')",
            name="origin_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="status_valid",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("expires_at > created_at", name="valid_window"),
        CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' "
            "AND jsonb_array_length(source_refs) > 0",
            name="source_refs_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposed_memory_key: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    proposed_memory_kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    proposed_value: Mapped[Any] = mapped_column(
        JSONB(none_as_null=False), nullable=False
    )
    proposed_summary: Mapped[str] = mapped_column(
        String(500), nullable=False
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)


class LongTermUserMemoryRecord(MemoryScopeColumns, TimestampMixin, Base):
    """已被用户或可信操作者确认的长期记忆。"""

    __tablename__ = "long_term_user_memories"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "id",
            name="uq_long_term_memories_tenant_user_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "candidate_id"],
            [
                "long_term_memory_candidates.tenant_id",
                "long_term_memory_candidates.user_id",
                "long_term_memory_candidates.id",
            ],
            name="fk_long_term_memories_candidate_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "supersedes_memory_id"],
            [
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ],
            name="fk_long_term_memories_supersedes_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_long_term_user_memories_active_lookup",
            "tenant_id",
            "user_id",
            "status",
            "valid_from",
            "valid_until",
        ),
        Index(
            "ix_long_term_user_memories_candidate",
            "tenant_id",
            "candidate_id",
        ),
        CheckConstraint(
            "memory_kind IN ('task_goal', 'pending_item', "
            "'temporary_preference', 'stable_preference', "
            "'role_responsibility', 'user_constraint')",
            name="memory_kind_valid",
        ),
        CheckConstraint(
            "origin IN ('explicit_user', 'trusted_identity', "
            "'approved_candidate')",
            name="origin_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'revoked')",
            name="status_valid",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_window",
        ),
        CheckConstraint(
            "confirmed_at <= recorded_at",
            name="confirmation_order",
        ),
        CheckConstraint(
            "supersedes_memory_id IS NULL OR supersedes_memory_id <> id",
            name="no_self_supersession",
        ),
        CheckConstraint(
            "origin <> 'approved_candidate' OR candidate_id IS NOT NULL",
            name="approved_candidate_has_source",
        ),
        CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' "
            "AND jsonb_array_length(source_refs) > 0",
            name="source_refs_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_value: Mapped[Any] = mapped_column(
        JSONB(none_as_null=False), nullable=False
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="active", nullable=False
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    supersedes_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


Index(
    "uq_active_long_term_memory_scope_key",
    LongTermUserMemoryRecord.tenant_id,
    LongTermUserMemoryRecord.user_id,
    func.coalesce(LongTermUserMemoryRecord.team_id, ""),
    func.coalesce(LongTermUserMemoryRecord.section_id, ""),
    func.coalesce(LongTermUserMemoryRecord.role_id, ""),
    LongTermUserMemoryRecord.memory_key,
    unique=True,
    postgresql_where=LongTermUserMemoryRecord.status == "active",
)


class MemoryPromotionRequestRecord(TimestampMixin, Base):
    """一次可重放、可审计的候选晋升请求。"""

    __tablename__ = "memory_promotion_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_memory_promotion_requests_tenant_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "candidate_id"],
            [
                "long_term_memory_candidates.tenant_id",
                "long_term_memory_candidates.user_id",
                "long_term_memory_candidates.id",
            ],
            name="fk_memory_promotion_candidate_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "supersedes_memory_id"],
            [
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ],
            name="fk_memory_promotion_supersedes_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "resulting_memory_id"],
            [
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ],
            name="fk_memory_promotion_result_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_memory_promotion_requests_candidate_created",
            "tenant_id",
            "candidate_id",
            "created_at",
        ),
        CheckConstraint(
            "expected_candidate_version >= 1",
            name="expected_version_positive",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="fingerprint_length",
        ),
        CheckConstraint(
            "status IN ('reserved', 'completed')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'reserved' AND resulting_memory_id IS NULL) OR "
            "(status = 'completed' AND resulting_memory_id IS NOT NULL)",
            name="result_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    expected_candidate_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    resulting_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), default="reserved", nullable=False
    )
