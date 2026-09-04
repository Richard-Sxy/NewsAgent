import uuid

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.execution import StepType
from app.domain.job_status import JobStatus
from app.models.common import TimestampMixin
from app.models.types import postgres_enum
from app.domain.job_scenario import JobScenario


class WritingJob(TimestampMixin, Base):
    """新闻写作业务任务，是 checkpoint 状态的唯一事实源。"""

    __tablename__ = "writing_jobs"
    """这边是约束的意思。"""
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_writing_jobs_tenant_idempotency_key"
        ),
        UniqueConstraint(
            "tenant_id",
            "temporal_workflow_id",
            name="uq_writing_jobs_tenant_temporal_workflow_id",
        ),
        Index("ix_writing_jobs_tenant_status_updated", "tenant_id", "status", "updated_at"),
        CheckConstraint("research_retries BETWEEN 0 AND 2", name="research_retries_limit"),
        CheckConstraint("review_rounds BETWEEN 0 AND 3", name="review_rounds_limit"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="progress_percent_range"),
        CheckConstraint("sections_completed >= 0", name="sections_completed_nonnegative"),
        CheckConstraint("sections_total >= 0", name="sections_total_nonnegative"),
        CheckConstraint("sections_completed <= sections_total", name="sections_progress_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    temporal_workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    requirements: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        postgres_enum(JobStatus, "writing_job_status"),
        default=JobStatus.CREATED,
        nullable=False,
    )
    current_step: Mapped[StepType | None] = mapped_column(
        postgres_enum(StepType, "writing_step_type"), nullable=True
    )
    research_retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    external_publication_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scenario: Mapped[JobScenario] = mapped_column(
        postgres_enum(JobScenario, "writing_job_scenario"),
        default=JobScenario.ASSISTED_WRITING,
        nullable=False,
    )

    __mapper_args__ = {"version_id_col": version}
