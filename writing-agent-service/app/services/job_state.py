import uuid

from sqlalchemy import select

from app.db.session import Database
from app.domain.job_status import JobStatus
from app.domain.transition import validate_transition
from app.models.job import WritingJob
from app.services.checkpoint import CheckpointError
from app.workflows.contracts import JobStateCommand
from app.services.outbox import OutboxService
from datetime import datetime, timezone


class JobStateHandler:
    """同步不产生 Artifact 的 Workflow 控制状态到 PostgreSQL。"""

    def __init__(
        self,
        database: Database,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self.database = database
        self.outbox_service = outbox_service

    async def __call__(self, command: JobStateCommand) -> None:
        tenant_id = uuid.UUID(command.tenant_id)
        job_id = uuid.UUID(command.job_id)
        target = JobStatus(command.target_status)
        async with self.database.session() as session:
            result = await session.execute(
                select(WritingJob)
                .where(WritingJob.id == job_id, WritingJob.tenant_id == tenant_id)
                .with_for_update()
            )
            job = result.scalar_one_or_none()
            if job is None:
                raise CheckpointError("WritingJob 不存在或不属于当前租户")
            if job.status != target:
                validate_transition(job.status, target)
                job.status = target
            job.current_step = None
            if target == JobStatus.RESEARCH_COMPLETED:
                job.progress_percent = 100
            if self.outbox_service is not None:
                event_names = {
                    JobStatus.RESEARCH_COMPLETED: "job.completed",
                    JobStatus.CANCELLED: "job.cancelled",
                    JobStatus.WAITING_HUMAN: "job.waiting_human",
                    JobStatus.FAILED: "job.failed",
                }
                event_name = event_names[target]
                await self.outbox_service.enqueue_job_event(
                    session,
                    event_name=event_name,
                    tenant_id=tenant_id,
                    job=job,
                    deduplication_key=(
                        f"{job.id}:{event_name}:{command.reason or 'state'}"
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    message=command.reason,
                )
            await session.flush()
