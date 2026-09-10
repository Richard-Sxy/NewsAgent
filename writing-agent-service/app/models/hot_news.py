"""热点分析运行的 PostgreSQL ORM 模型。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class HotNewsAnalysisRun(TimestampMixin, Base):
    """一次有边界热点窗口的不可变成功快照。"""

    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_analysis_runs_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_analysis_runs_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "idempotency_key",
            name="uq_analysis_runs_tenant_run_identity",
        ),
        Index(
            "ix_analysis_runs_tenant_window",
            "tenant_id",
            "window_start",
            "window_end",
        ),
        CheckConstraint("window_start < window_end", name="valid_window"),
        CheckConstraint(
            "fetched_record_count >= 0", name="fetched_count_nonnegative"
        ),
        CheckConstraint(
            "metric_snapshot_count >= 0", name="metric_count_nonnegative"
        ),
        CheckConstraint(
            "ranked_news_count >= 0", name="ranked_count_nonnegative"
        ),
        CheckConstraint(
            "analyzed_news_count >= 0", name="analyzed_count_nonnegative"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    production_bundle_version: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    workflow_version: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ranked_news_count: Mapped[int] = mapped_column(Integer, nullable=False)
    analyzed_news_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(
        String(32), default="1.0", nullable=False
    )
    result_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
