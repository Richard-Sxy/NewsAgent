"""
聊天记录：会话归属、每轮问题与回答、工具轨迹、模型调用编号和错误码。
"""

from datetime import datetime
from typing import Any
from uuid import UUID as UUIDType, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class ConversationRecord(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "id", name="uq_conversations_owner_id"
        ),
        Index(
            "ix_conversations_owner_updated", "tenant_id", "user_id", "updated_at"
        ),
    )

    id: Mapped[UUIDType] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    context_memory: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConversationTurnRecord(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "conversation_id"],
            [
                "conversations.tenant_id",
                "conversations.user_id",
                "conversations.id",
            ],
            name="fk_conversation_turns_owner_conversation",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "conversation_id", "request_id", name="uq_conversation_turns_request"
        ),
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed')", name="status_valid"
        ),
        CheckConstraint(
            "(status = 'processing' AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="completion_valid",
        ),
        Index(
            "ix_conversation_turns_owner_created",
            "tenant_id",
            "user_id",
            "conversation_id",
            "created_at",
        ),
        Index(
            "uq_conversation_turns_processing",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'processing'"),
        ),
    )

    id: Mapped[UUIDType] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[UUIDType] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    request_id: Mapped[UUIDType] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_content: Mapped[str] = mapped_column(String(4000), nullable=False)
    assistant_content: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    model_request_ids: Mapped[list[str]] = mapped_column(
        JSONB(), nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
