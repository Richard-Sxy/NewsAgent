import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.execution import ArtifactType, StepType
from app.domain.job_status import JobStatus


class ArtifactDescriptor(BaseModel):
    """已写入对象存储、等待数据库 checkpoint 的不可变产物描述。"""

    artifact_type: ArtifactType
    logical_key: str = Field(min_length=1, max_length=160)
    schema_version: str = Field(min_length=1, max_length=32)
    storage_uri: str = Field(min_length=1, max_length=2048)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_size: int = Field(ge=0)
    metadata: dict = Field(default_factory=dict)


class CheckpointCommit(BaseModel):
    """成功完成一个业务步骤时提交的原子 checkpoint 命令。"""

    tenant_id: uuid.UUID
    job_id: uuid.UUID
    step_id: uuid.UUID
    agent_run_id: uuid.UUID
    target_status: JobStatus
    next_step: StepType | None
    artifact: ArtifactDescriptor
    fastgpt_request_id: str | None = Field(default=None, max_length=255)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    review_round: int | None = Field(default=None, ge=1, le=3)


class FailureCommit(BaseModel):
    """Agent 执行失败时提交的原子状态命令。"""

    tenant_id: uuid.UUID
    job_id: uuid.UUID
    step_id: uuid.UUID
    agent_run_id: uuid.UUID
    error_code: str = Field(min_length=1, max_length=120)
    error_message: str = Field(min_length=1, max_length=8000)
    retryable: bool = True


class FailureOutcome(BaseModel):
    """失败 checkpoint 提交后交给 Temporal 的确定性路由结果。"""

    action: Literal["retry", "waiting_human"]
    retry_step_id: uuid.UUID | None = None
    next_attempt: int | None = None


class ArtifactReference(BaseModel):
    """恢复工作流所需的不可变产物引用。"""

    artifact_id: uuid.UUID
    logical_key: str
    version: int
    storage_uri: str
    content_sha256: str


class ResumePoint(BaseModel):
    """Temporal Worker 恢复任务时读取的业务恢复点。"""

    job_id: uuid.UUID
    status: JobStatus
    current_step: StepType | None
    resumable_step_id: uuid.UUID | None
    artifacts: dict[str, ArtifactReference]
