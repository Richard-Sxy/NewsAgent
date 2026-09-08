"""这边先创建一个决策表"""
import uuid
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin

class HotNewsDecision(TimestampMixin, Base):
    __tablename__ = "hot_news_decisions"

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_hot_news_decisions_tenant_idempotency_key",
        ),
        Index(
            "ix_hot_news_decisions_tenant_news_created",
            "tenant_id",
            "news_id",
            "created_at",
        ),
        CheckConstraint(
            "decision_type IN "
            "('accepted', 'rejected', 'deferred', 'corrected')",
            name="valid_decision_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    news_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    decision_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    correction_payload: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
    )

    operator_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
    )

    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hot_news_decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )