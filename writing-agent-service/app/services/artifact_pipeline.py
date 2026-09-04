import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.fastgpt import AgentResult
from app.domain.execution import ArtifactType, StepType
from app.domain.job_status import JobStatus
from app.models.artifact import WritingArtifact
from app.schemas.checkpoint import CheckpointCommit
from app.services.checkpoint import CheckpointService
from app.storage.s3 import S3ArtifactStore


class ArtifactPipeline:
    """连接已校验 Agent 输出、对象存储和原子数据库 checkpoint。"""

    def __init__(
        self,
        artifact_store: S3ArtifactStore,
        checkpoint_service: CheckpointService,
    ) -> None:
        self.artifact_store = artifact_store
        self.checkpoint_service = checkpoint_service

    async def persist_success(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        agent_run_id: uuid.UUID,
        result: AgentResult[BaseModel],
        artifact_type: ArtifactType,
        logical_key: str,
        schema_version: str,
        target_status: JobStatus,
        next_step: StepType | None,
        metadata: dict[str, Any] | None = None,
        review_round: int | None = None,
    ) -> WritingArtifact:
        artifact_metadata = {
            **(metadata or {}),
            "request_id": result.request_id,
            "prompt_tokens": result.usage.get("prompt_tokens"),
            "completion_tokens": result.usage.get("completion_tokens"),
        }
        descriptor = await self.artifact_store.put_json(
            tenant_id=tenant_id,
            job_id=job_id,
            artifact_type=artifact_type,
            logical_key=logical_key,
            schema_version=schema_version,
            payload=result.value.model_dump(mode="json"),
            metadata=artifact_metadata,
        )
        return await self.checkpoint_service.commit_success(
            session,
            CheckpointCommit(
                tenant_id=tenant_id,
                job_id=job_id,
                step_id=step_id,
                agent_run_id=agent_run_id,
                target_status=target_status,
                next_step=next_step,
                artifact=descriptor,
                fastgpt_request_id=result.request_id,
                prompt_tokens=result.usage.get("prompt_tokens"),
                completion_tokens=result.usage.get("completion_tokens"),
                review_round=review_round,
            ),
        )
