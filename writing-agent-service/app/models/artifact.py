import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.execution import ArtifactType
from app.models.common import TimestampMixin
from app.models.types import postgres_enum


class WritingArtifact(TimestampMixin, Base):
    """不可变写作产物；正文位于对象存储，数据库保存版本和完整性信息。"""

    __tablename__ = "writing_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "logical_key",
            "version",
            name="uq_writing_artifacts_job_key_version",
        ),
        Index("ix_writing_artifacts_job_type", "job_id", "artifact_type"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("content_size >= 0", name="content_size_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("writing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("writing_steps.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_type: Mapped[ArtifactType] = mapped_column(
        postgres_enum(ArtifactType, "writing_artifact_type"), nullable=False
    )
    logical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
