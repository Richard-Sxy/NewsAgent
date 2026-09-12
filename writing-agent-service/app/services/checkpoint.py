import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import ExecutionStatus, StepType
from app.domain.job_status import JobStatus
from app.domain.transition import validate_transition
from app.models.agent_run import AgentRun
from app.models.artifact import WritingArtifact
from app.models.job import WritingJob
from app.models.step import WritingStep
from app.schemas.checkpoint import CheckpointCommit, FailureCommit, FailureOutcome
from app.services.outbox import OutboxService


class CheckpointError(RuntimeError):
    """Checkpoint 前置条件不成立或目标记录不存在。"""


class CheckpointService:
    """在一个 PostgreSQL 事务内提交步骤产物和全部关联状态。"""

    def __init__(self, outbox_service: OutboxService | None = None) -> None:
        self.outbox_service = outbox_service

    async def commit_success(
        self,
        session: AsyncSession,
        command: CheckpointCommit,
    ) -> WritingArtifact:
        """提交成功 checkpoint；同一 AgentRun 重放时返回原产物。"""
        job = await self._lock_job(session, command.tenant_id, command.job_id)
        step = await self._lock_step(session, command.job_id, command.step_id)
        agent_run = await self._lock_agent_run(
            session,
            command.job_id,
            command.step_id,
            command.agent_run_id,
        )

        if agent_run.status == ExecutionStatus.SUCCEEDED:
            if agent_run.output_artifact_id is None:
                raise CheckpointError("已成功的 AgentRun 缺少 output_artifact_id")
            artifact = await session.get(
                WritingArtifact,
                agent_run.output_artifact_id,
            )
            if artifact is None:
                raise CheckpointError("AgentRun 指向的产物不存在")
            return artifact

        if step.status != ExecutionStatus.RUNNING:
            raise CheckpointError(
                f"只有 running 步骤可以提交成功，当前为 {step.status.value}"
            )
        if agent_run.status != ExecutionStatus.RUNNING:
            raise CheckpointError(
                "只有 running AgentRun 可以提交成功，"
                f"当前为 {agent_run.status.value}"
            )

        if job.status != command.target_status:
            validate_transition(job.status, command.target_status)

        version = await self._next_artifact_version(
            session,
            command.job_id,
            command.artifact.logical_key,
        )
        artifact = WritingArtifact(
            id=uuid.uuid4(),
            job_id=command.job_id,
            step_id=command.step_id,
            artifact_type=command.artifact.artifact_type,
            logical_key=command.artifact.logical_key,
            version=version,
            schema_version=command.artifact.schema_version,
            storage_uri=command.artifact.storage_uri,
            content_sha256=command.artifact.content_sha256,
            content_size=command.artifact.content_size,
            artifact_metadata=command.artifact.metadata,
        )
        session.add(artifact)
        await session.flush()

        completed_at = datetime.now(timezone.utc)
        agent_run.status = ExecutionStatus.SUCCEEDED
        agent_run.output_artifact_id = artifact.id
        agent_run.fastgpt_request_id = command.fastgpt_request_id
        agent_run.prompt_tokens = command.prompt_tokens
        agent_run.completion_tokens = command.completion_tokens
        agent_run.completed_at = completed_at
        step.status = ExecutionStatus.SUCCEEDED
        step.completed_at = completed_at
        step.error_code = None
        step.error_message = None
        job.status = command.target_status
        job.current_step = command.next_step
        if command.review_round is not None:
            job.review_rounds = max(job.review_rounds, command.review_round)

        if self.outbox_service is not None:
            progress_event = await self.outbox_service.enqueue_step_completed(
                session,
                tenant_id=command.tenant_id,
                job_id=command.job_id,
                step=step,
                artifact=artifact,
                status=command.target_status,
                occurred_at=completed_at,
                previous_percent=job.progress_percent or 0,
            )
            job.progress_percent = max(
                job.progress_percent or 0,
                progress_event.progress.percent,
            )
            if step.step_type == StepType.SECTION_DRAFT:
                job.sections_completed = max(
                    job.sections_completed or 0,
                    progress_event.progress.completed,
                )
                job.sections_total = max(
                    job.sections_total or 0,
                    progress_event.progress.total,
                )
            if command.target_status == JobStatus.FINAL_APPROVED:
                await self.outbox_service.enqueue_job_event(
                    session,
                    event_name="job.completed",
                    tenant_id=command.tenant_id,
                    job=job,
                    deduplication_key=f"{job.id}:completed",
                    occurred_at=completed_at,
                )

        await session.flush()
        return artifact

    async def commit_failure(
        self,
        session: AsyncSession,
        command: FailureCommit,
    ) -> FailureOutcome:
        """原子记录失败，并确定创建重试步骤或转入人工处理。"""
        job = await self._lock_job(session, command.tenant_id, command.job_id)
        step = await self._lock_step(session, command.job_id, command.step_id)
        agent_run = await self._lock_agent_run(
            session,
            command.job_id,
            command.step_id,
            command.agent_run_id,
        )

        if (
            agent_run.status == ExecutionStatus.FAILED
            and step.status == ExecutionStatus.FAILED
        ):
            return await self._replay_failure_outcome(session, job, step)

        if step.status != ExecutionStatus.RUNNING:
            raise CheckpointError(
                f"只有 running 步骤可以提交失败，当前为 {step.status.value}"
            )
        if agent_run.status != ExecutionStatus.RUNNING:
            raise CheckpointError(
                "只有 running AgentRun 可以提交失败，"
                f"当前为 {agent_run.status.value}"
            )

        failed_at = datetime.now(timezone.utc)
        agent_run.status = ExecutionStatus.FAILED
        agent_run.error_code = command.error_code
        agent_run.error_message = command.error_message
        agent_run.completed_at = failed_at
        step.status = ExecutionStatus.FAILED
        step.error_code = command.error_code
        step.error_message = command.error_message
        step.completed_at = failed_at

        if self.outbox_service is not None:
            await self.outbox_service.enqueue_step_failed(
                session,
                tenant_id=command.tenant_id,
                job=job,
                step=step,
                message=command.error_message,
                occurred_at=failed_at,
            )

        if command.retryable and self._can_retry(job, step):
            if step.step_type == StepType.RESEARCH:
                job.research_retries += 1
            retry_step = WritingStep(
                id=uuid.uuid4(),
                job_id=job.id,
                step_type=step.step_type,
                step_key=step.step_key,
                attempt=step.attempt + 1,
                status=ExecutionStatus.PENDING,
                input_snapshot=step.input_snapshot,
            )
            session.add(retry_step)
            await session.flush()
            return FailureOutcome(
                action="retry",
                retry_step_id=retry_step.id,
                next_attempt=retry_step.attempt,
            )

        validate_transition(job.status, JobStatus.WAITING_HUMAN)
        job.status = JobStatus.WAITING_HUMAN
        job.current_step = None
        if self.outbox_service is not None:
            await self.outbox_service.enqueue_job_event(
                session,
                event_name="job.waiting_human",
                tenant_id=command.tenant_id,
                job=job,
                deduplication_key=f"{job.id}:{step.step_key}:{step.attempt}:waiting",
                occurred_at=failed_at,
                message=command.error_message,
            )
        await session.flush()
        return FailureOutcome(action="waiting_human")

    @staticmethod
    def _can_retry(job: WritingJob, step: WritingStep) -> bool:
        """应用 README 中的业务重试上限，普通执行故障最多尝试 3 次。"""
        if step.step_type == StepType.RESEARCH:
            return job.research_retries < 2
        if step.step_type == StepType.SECTION_REVISE:
            return step.attempt < 2
        return step.attempt < 3

    @staticmethod
    async def _replay_failure_outcome(
        session: AsyncSession,
        job: WritingJob,
        step: WritingStep,
    ) -> FailureOutcome:
        """失败事件重复投递时返回已有决策，不重复增加计数。"""
        if job.status == JobStatus.WAITING_HUMAN:
            return FailureOutcome(action="waiting_human")
        result = await session.execute(
            select(WritingStep).where(
                WritingStep.job_id == step.job_id,
                WritingStep.step_key == step.step_key,
                WritingStep.attempt == step.attempt + 1,
            )
        )
        retry_step = result.scalar_one_or_none()
        if retry_step is None:
            raise CheckpointError("失败记录已存在，但找不到对应的重试步骤")
        return FailureOutcome(
            action="retry",
            retry_step_id=retry_step.id,
            next_attempt=retry_step.attempt,
        )

    """ 对于Job上锁 """
    @staticmethod
    async def _lock_job(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> WritingJob:
        result = await session.execute(
            select(WritingJob)
            .where(WritingJob.id == job_id, WritingJob.tenant_id == tenant_id)
            .with_for_update()             # 这边的意思是把查询到的记录加上行级排他锁
        )
        job = result.scalar_one_or_none()  # 没有返回[]，有一个返回对象，多个报错
        if job is None:
            raise CheckpointError("WritingJob 不存在或不属于当前租户")
        return job

    @staticmethod
    async def _lock_step(
        session: AsyncSession,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
    ) -> WritingStep:
        result = await session.execute(
            select(WritingStep)
            .where(WritingStep.id == step_id, WritingStep.job_id == job_id)
            .with_for_update()
        )
        step = result.scalar_one_or_none()
        if step is None:
            raise CheckpointError("WritingStep 不存在或不属于当前任务")
        return step

    @staticmethod
    async def _lock_agent_run(
        session: AsyncSession, # 异步会话
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        agent_run_id: uuid.UUID,
    ) -> AgentRun:
        result = await session.execute(
            select(AgentRun)
            .where(
                AgentRun.id == agent_run_id,
                AgentRun.job_id == job_id,
                AgentRun.step_id == step_id,
            )
            .with_for_update()
        )
        agent_run = result.scalar_one_or_none()
        if agent_run is None:
            raise CheckpointError("AgentRun 不存在或不属于当前步骤")
        return agent_run

    @staticmethod
    async def _next_artifact_version(
        session: AsyncSession,
        job_id: uuid.UUID,
        logical_key: str,
    ) -> int:
        """按 Job 和逻辑键加事务级锁，再安全计算不可变版本号。"""
        lock_key = f"artifact:{job_id}:{logical_key}"
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(lock_key, 0)
                )
            )
        )
        result = await session.execute(
            select(func.max(WritingArtifact.version)).where(
                WritingArtifact.job_id == job_id,
                WritingArtifact.logical_key == logical_key,
            )
        )
        current_version = result.scalar_one_or_none()
        return (current_version or 0) + 1
