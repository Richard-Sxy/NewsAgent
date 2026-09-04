import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.execution import AgentType, ExecutionStatus
from app.models.common import TimestampMixin
from app.models.types import postgres_enum


class AgentRun(TimestampMixin, Base):
    """一次 FastGPT 子 Agent 调用的审计、计费和幂等记录。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "step_id", "idempotency_key", name="uq_agent_runs_step_idempotency_key"
        ),
        Index("ix_agent_runs_job_status", "job_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("writing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("writing_steps.id", ondelete="CASCADE"), nullable=False
    )
    agent_type: Mapped[AgentType] = mapped_column(
        postgres_enum(AgentType, "writing_agent_type"), nullable=False
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        postgres_enum(ExecutionStatus, "writing_execution_status"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    fastgpt_app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fastgpt_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workflow_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    input_artifact_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("writing_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
