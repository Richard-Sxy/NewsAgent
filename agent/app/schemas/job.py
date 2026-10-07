import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.execution import StepType
from app.domain.job_scenario import JobScenario
from app.domain.job_status import JobStatus
from app.schemas.checkpoint import ResumePoint
from app.schemas.research import ResearchMetrics
from app.workflows.contracts import WorkflowSnapshot

"""创建写作任务请求"""
class CreateWritingJobRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=2000)
    scenario: JobScenario = JobScenario.ASSISTED_WRITING
    requirements: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)

"""写作任务回答"""
class WritingJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    topic: str
    requirements: dict[str, Any]
    status: JobStatus
    current_step: StepType | None
    temporal_workflow_id: str
    scenario: JobScenario
    research_retries: int
    review_rounds: int
    progress_percent: int
    sections_completed: int
    sections_total: int
    created_at: datetime
    updated_at: datetime


class WritingJobListResponse(BaseModel):
    """运营工作台任务列表，使分页信息与任务数据一起返回。"""

    items: list[WritingJobResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)

"""人工请求决定"""
class HumanDecisionRequest(BaseModel):
    gate: Literal["research", "outline", "review", "final"]
    action: Literal["approve", "revise", "research", "cancel"]
    instruction: str | None = Field(default=None, max_length=8000)

"""工作流进程回复"""
class WorkflowProgressResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    current_step: StepType | None
    workflow: WorkflowSnapshot | None = None
    workflow_status: Literal["available", "missing"] = "available"

"""决定接收回复"""
class DecisionAcceptedResponse(BaseModel):
    accepted: bool = True
    job_id: uuid.UUID
    gate: str


class RecoveryPlanResponse(BaseModel):
    job_id: uuid.UUID
    can_resume: bool
    requires_human: bool
    reason: str
    resume_point: ResumePoint


class ResumeJobRequest(BaseModel):
    resume_token: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    recovery_action: Literal["research"] | None = None
    instruction: str | None = Field(default=None, max_length=8000)


class ResumeJobResponse(BaseModel):
    accepted: bool = True
    job_id: uuid.UUID
    workflow_id: str
    status: JobStatus


class ResearchMetricsResponse(BaseModel):
    job_id: uuid.UUID
    logical_key: str
    artifact_version: int
    metrics: ResearchMetrics


class PublishJobRequest(BaseModel):
    channel: str = Field(default="default", min_length=1, max_length=64)


class PublishJobResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    external_publication_id: str
    published_at: datetime
    idempotent_replay: bool = False
