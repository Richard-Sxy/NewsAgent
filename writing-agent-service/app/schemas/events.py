# 这边创建 events.py
import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

from app.domain.execution import StepType
from app.domain.job_status import JobStatus

ProgressEventName = Literal[
    "job.created",
    "step.started",
    "step.completed",
    "step.failed",
    "job.waiting_human",
    "job.resumed",
    "job.completed",
    "job.cancelled",
    "job.failed",
    "job.published",
]

class ProgressValue(BaseModel):
    """前端进度条需要的确定性数值"""
    completed: int = Field(ge=0)
    total: int = Field(ge=0)
    percent: int = Field(ge=0, le=100)

class EventArtifactReference(BaseModel):
    """事件只携带产物引用，不携带文章正文。"""
    artifact_id: uuid.UUID | None = None
    logical_key: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)

class ProgressEvent(BaseModel):
    """写入RedisStream并通过SSE推送的统一事件协议。"""
    event: ProgressEventName
    event_id: str | None = None
    deduplication_key: str = Field(min_length=1, max_length=255)
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    step: StepType | None = None
    section_id: str | None = Field(default=None, pattern=r"^S\d{2,}$")
    status: JobStatus
    progress: ProgressValue
    artifact: EventArtifactReference | None = None
    message: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime
