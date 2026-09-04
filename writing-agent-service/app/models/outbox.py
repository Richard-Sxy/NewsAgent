import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class OutboxEvent(TimestampMixin, Base):
    """与业务 checkpoint 同事务提交的待发布进度事件。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index(
            "ix_outbox_events_pending_available",
            "status",
            "available_at",
            "created_at",
        ),
        Index("ix_outbox_events_job_created", "job_id", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'published', 'dead_letter')",
            name="outbox_status_valid",
        ),
        CheckConstraint("attempts >= 0", name="outbox_attempts_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("writing_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
