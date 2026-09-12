"""热点事件的 PostgreSQL ORM 模型。

事件按租户和确定性 event_key 唯一；成员、最近指标和生命周期状态随每次
运行 upsert，不保存企业原始用户行为明细。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import TimestampMixin


class HotEventRecord(TimestampMixin, Base):
    """跨窗口聚合后的一个热点事件及其成员新闻。"""

    __tablename__ = "hot_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "event_key",
            name="uq_hot_events_tenant_event_key",
        ),
        Index(
            "ix_hot_events_tenant_state_last_seen",
            "tenant_id",
            "state",
            "last_seen_at",
        ),
        CheckConstraint(
            "state IN ('emerging', 'active', 'cooling', 'closed')",
            name="state_valid",
        ),
        CheckConstraint(
            "occurrence_count >= 1", name="occurrence_count_positive"
        ),
        CheckConstraint(
            "distinct_news_count >= 1", name="distinct_news_count_positive"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "latest_hot_score >= 0 AND latest_hot_score <= 1",
            name="hot_score_range",
        ),
        CheckConstraint(
            "jsonb_typeof(member_news_ids) = 'array' "
            "AND jsonb_array_length(member_news_ids) > 0",
            name="member_news_ids_nonempty",
        ),
        CheckConstraint(
            "first_seen_at <= last_seen_at", name="seen_order"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_news_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_news_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    latest_hot_score: Mapped[float] = mapped_column(Float, nullable=False)
    latest_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
