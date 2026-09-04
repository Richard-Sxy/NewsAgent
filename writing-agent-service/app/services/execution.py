import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import AgentType, ExecutionStatus, StepType
from app.domain.job_status import JobStatus
from app.domain.transition import validate_transition
from app.models.agent_run import AgentRun
from app.models.artifact import WritingArtifact
from app.models.job import WritingJob
from app.models.step import WritingStep
from app.services.checkpoint import CheckpointError


@dataclass(frozen=True)
class PreparedExecution:
    step_id: uuid.UUID
    agent_run_id: uuid.UUID
    attempt: int
    replay_artifact: WritingArtifact | None = None


class ExecutionService:
    """幂等建立或恢复一个 Activity 对应的 Step 和 AgentRun。"""

    async def prepare(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        step_type: StepType,
        step_key: str,
        requested_attempt: int,
        input_snapshot: dict,
        agent_type: AgentType,
        fastgpt_app_id: str,
        active_status: JobStatus,
    ) -> PreparedExecution:
        job_result = await session.execute(
            select(WritingJob)
            .where(WritingJob.id == job_id, WritingJob.tenant_id == tenant_id)
            .with_for_update()
        )
        job = job_result.scalar_one_or_none()
        if job is None:
            raise CheckpointError("WritingJob 不存在或不属于当前租户")

        step_result = await session.execute(
            select(WritingStep)
            .where(
                WritingStep.job_id == job_id,
                WritingStep.step_key == step_key,
                WritingStep.attempt >= requested_attempt,
            )
            .order_by(WritingStep.attempt.desc())
            .limit(1)
            .with_for_update()
        )
        step = step_result.scalar_one_or_none()
        if step is None:
            step = WritingStep(
                id=uuid.uuid4(),
                job_id=job_id,
                step_type=step_type,
                step_key=step_key,
                attempt=requested_attempt,
                status=ExecutionStatus.PENDING,
                input_snapshot=input_snapshot,
            )
            session.add(step)
            await session.flush()
        elif step.step_type != step_type:
            raise CheckpointError("同一 step_key 对应了不同 step_type")

        # RecoveryService may have pre-created a pending retry from the failed
        # checkpoint.  The restarted workflow is the authority for the new
        # command input (for example, section progress calculated from the
        # replayed outline), so bind that input before the attempt starts.
        # Once an attempt is running its snapshot remains immutable for audit.
        if step.status == ExecutionStatus.PENDING:
            step.input_snapshot = input_snapshot

        run_result = await session.execute(
            select(AgentRun)
            .where(
                AgentRun.step_id == step.id,
                AgentRun.idempotency_key == f"{step_key}:{step.attempt}",
            )
            .with_for_update()
        )
        run = run_result.scalar_one_or_none()
        if run is not None and run.status == ExecutionStatus.SUCCEEDED:
            # 恢复 Workflow 会重放控制流程；先把 Job 放回该步骤的活动状态，
            # Handler 读取已存在 Artifact 后再推进到成功目标状态。
            if job.status != active_status:
                validate_transition(job.status, active_status)
                job.status = active_status
            job.current_step = step_type
            artifact = await session.get(WritingArtifact, run.output_artifact_id)
            if artifact is None:
                raise CheckpointError("成功的 AgentRun 缺少有效 Artifact")
            return PreparedExecution(step.id, run.id, step.attempt, artifact)

        if step.status == ExecutionStatus.FAILED:
            raise CheckpointError("失败步骤没有可用的后续重试步骤")

        started_at = datetime.now(timezone.utc)
        if run is None:
            run = AgentRun(
                id=uuid.uuid4(),
                job_id=job_id,
                step_id=step.id,
                agent_type=agent_type,
                status=ExecutionStatus.RUNNING,
                idempotency_key=f"{step_key}:{step.attempt}",
                fastgpt_app_id=fastgpt_app_id,
                input_artifact_ids=[],
                started_at=started_at,
            )
            session.add(run)
        else:
            run.status = ExecutionStatus.RUNNING
            run.started_at = run.started_at or started_at
        step.status = ExecutionStatus.RUNNING
        step.started_at = step.started_at or started_at

        if job.status != active_status:
            validate_transition(job.status, active_status)
            job.status = active_status
        job.current_step = step_type
        await session.flush()
        return PreparedExecution(step.id, run.id, step.attempt)

    async def advance_replayed_job(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        target_status: JobStatus,
        next_step: StepType | None,
        review_round: int | None = None,
    ) -> None:
        result = await session.execute(
            select(WritingJob)
            .where(WritingJob.id == job_id, WritingJob.tenant_id == tenant_id)
            .with_for_update()
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise CheckpointError("WritingJob 不存在或不属于当前租户")
        if job.status != target_status:
            validate_transition(job.status, target_status)
            job.status = target_status
        job.current_step = next_step
        if review_round is not None:
            job.review_rounds = max(job.review_rounds, review_round)
        await session.flush()
