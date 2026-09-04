import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.execution import ExecutionStatus, StepType
from app.models.common import TimestampMixin
from app.models.types import postgres_enum


class WritingStep(TimestampMixin, Base):
    """一个可独立重试、恢复并提交 checkpoint 的业务步骤。"""

    __tablename__ = "writing_steps"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "step_key", "attempt", name="uq_writing_steps_job_key_attempt"
        ),
        Index("ix_writing_steps_job_status", "job_id", "status"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("writing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    step_type: Mapped[StepType] = mapped_column(
        postgres_enum(StepType, "writing_step_type"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        postgres_enum(ExecutionStatus, "writing_execution_status"),
        default=ExecutionStatus.PENDING,
        nullable=False,
    )
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
