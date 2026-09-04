import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import ExecutionStatus
from app.domain.job_status import JobStatus
from app.models.artifact import WritingArtifact
from app.models.job import WritingJob
from app.models.step import WritingStep
from app.schemas.checkpoint import ArtifactReference, ResumePoint
from app.schemas.job import RecoveryPlanResponse
from app.services.checkpoint import CheckpointError
from app.services.outbox import OutboxService


class RecoveryNotAllowedError(ValueError):
    pass


class RecoveryService:
    """从业务数据库重建长链路任务的最后可靠恢复点。"""

    def __init__(self, outbox_service: OutboxService | None = None) -> None:
        self.outbox_service = outbox_service

    async def load_resume_point(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> ResumePoint:
        job_result = await session.execute(
            select(WritingJob).where(
                WritingJob.id == job_id,
                WritingJob.tenant_id == tenant_id,
            )
        )
        job = job_result.scalar_one_or_none()
        if job is None:
            raise CheckpointError("WritingJob 不存在或不属于当前租户")

        step_result = await session.execute(
            select(WritingStep)
            .where(
                WritingStep.job_id == job_id,
                WritingStep.status.in_(
                    [ExecutionStatus.PENDING, ExecutionStatus.RUNNING]
                ),
            )
            .order_by(WritingStep.created_at.desc())
            .limit(1)
        )
        resumable_step = step_result.scalar_one_or_none()

        artifact_result = await session.execute(
            select(WritingArtifact)
            .where(WritingArtifact.job_id == job_id)
            .distinct(WritingArtifact.logical_key)
            .order_by(
                WritingArtifact.logical_key,
                WritingArtifact.version.desc(),
            )
        )
        artifacts = {
            artifact.logical_key: ArtifactReference(
                artifact_id=artifact.id,
                logical_key=artifact.logical_key,
                version=artifact.version,
                storage_uri=artifact.storage_uri,
                content_sha256=artifact.content_sha256,
            )
            for artifact in artifact_result.scalars().all()
        }
        return ResumePoint(
            job_id=job.id,
            status=job.status,
            current_step=job.current_step,
            resumable_step_id=resumable_step.id if resumable_step else None,
            artifacts=artifacts,
        )

    async def build_recovery_plan(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> RecoveryPlanResponse:
        point = await self.load_resume_point(session, tenant_id, job_id)
        terminal = point.status in {JobStatus.PUBLISHED, JobStatus.CANCELLED}
        requires_human = point.status == JobStatus.WAITING_HUMAN
        if terminal:
            reason = f"任务已经处于终态 {point.status.value}"
        elif requires_human:
            reason = "任务等待人工决策，恢复前需要明确处理指令"
        elif point.resumable_step_id is not None:
            reason = "存在 pending/running 步骤，可以从当前 checkpoint 恢复"
        elif point.artifacts:
            reason = "没有活动步骤，可以从最后一个不可变 Artifact 重新规划"
        else:
            reason = "任务没有可用 Artifact，需要从研究阶段重新开始"
        return RecoveryPlanResponse(
            job_id=job_id,
            can_resume=not terminal,
            requires_human=requires_human,
            reason=reason,
            resume_point=point,
        )

    async def prepare_resume(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        resume_token: str,
    ) -> WritingJob:
        workflow_id = f"news-writing:{tenant_id}:{job_id}:resume:{resume_token}"
        result = await session.execute(
            select(WritingJob)
            .where(WritingJob.id == job_id, WritingJob.tenant_id == tenant_id)
            .with_for_update()
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise CheckpointError("WritingJob 不存在或不属于当前租户")
        if job.temporal_workflow_id == workflow_id:
            return job
        if job.status not in {JobStatus.WAITING_HUMAN, JobStatus.FAILED}:
            raise RecoveryNotAllowedError(
                f"只有 waiting_human/failed 任务可以恢复，当前为 {job.status.value}"
            )

        latest_step_result = await session.execute(
            select(WritingStep)
            .where(WritingStep.job_id == job_id)
            .order_by(WritingStep.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        latest_step = latest_step_result.scalar_one_or_none()
        if latest_step is not None and latest_step.status == ExecutionStatus.FAILED:
            session.add(
                WritingStep(
                    id=uuid.uuid4(),
                    job_id=job_id,
                    step_type=latest_step.step_type,
                    step_key=latest_step.step_key,
                    attempt=latest_step.attempt + 1,
                    status=ExecutionStatus.PENDING,
                    input_snapshot=latest_step.input_snapshot,
                )
            )
        job.temporal_workflow_id = workflow_id
        if self.outbox_service is not None:
            await self.outbox_service.enqueue_job_event(
                session,
                event_name="job.resumed",
                tenant_id=tenant_id,
                job=job,
                deduplication_key=f"{job.id}:resumed:{resume_token}",
                occurred_at=datetime.now(timezone.utc),
            )
        await session.flush()
        return job
    def __init__(self, outbox_service: OutboxService | None = None) -> None:
        self.outbox_service = outbox_service
