import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.job_status import JobStatus
from app.models.job import WritingJob
from app.schemas.job import CreateWritingJobRequest
from app.services.outbox import OutboxService


class JobNotFoundError(LookupError):
    pass


class JobService:
    """WritingJob 的租户隔离、创建幂等和读取服务。"""

    def __init__(self, outbox_service: OutboxService | None = None) -> None:
        self.outbox_service = outbox_service

    """幂等创建：不存在就插入返回True，存在就冲突，返回存在对象然后返回False"""
    async def create_or_get(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        created_by: uuid.UUID,
        request: CreateWritingJobRequest,
    ) -> tuple[WritingJob, bool]:
        job_id = uuid.uuid4()
        workflow_id = f"news-writing:{tenant_id}:{job_id}"
        statement = (
            insert(WritingJob)
            .values(
                id=job_id,
                tenant_id=tenant_id,
                created_by=created_by,
                idempotency_key=request.idempotency_key,
                temporal_workflow_id=workflow_id,
                topic=request.topic,
                scenario=request.scenario,
                requirements=request.requirements,
                status=JobStatus.CREATED,
                current_step=None,
                research_retries=0,
                review_rounds=0,
                progress_percent=0,
                sections_completed=0,
                sections_total=0,
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=[WritingJob.tenant_id, WritingJob.idempotency_key]
            )
            .returning(WritingJob)
        )
        result = await session.execute(statement)
        job = result.scalar_one_or_none()
        if job is not None:
            if self.outbox_service is not None:
                await self.outbox_service.enqueue_job_event(
                    session,
                    event_name="job.created",
                    tenant_id=tenant_id,
                    job=job,
                    deduplication_key=f"{job.id}:created",
                    occurred_at=datetime.now(timezone.utc),
                )
            return job, True

        existing = await session.execute(
            select(WritingJob).where(
                WritingJob.tenant_id == tenant_id,
                WritingJob.idempotency_key == request.idempotency_key,
            )
        )
        return existing.scalar_one(), False

    """获取Job信息"""
    async def get(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> WritingJob:
        # 从数据库中获取 Job 的信息
        result = await session.execute(
            select(WritingJob).where(
                WritingJob.id == job_id,
                WritingJob.tenant_id == tenant_id,
            )
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise JobNotFoundError("WritingJob 不存在")
        return job

    async def list_for_operations(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        statuses: list[JobStatus] | None = None,
        waiting_human_only: bool = False,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WritingJob], int]:
        """返回租户隔离的运营任务队列，最新变更的任务优先。"""
        filters = [WritingJob.tenant_id == tenant_id]
        if statuses:
            filters.append(WritingJob.status.in_(statuses))
        if waiting_human_only:
            filters.append(
                or_(
                    WritingJob.status == JobStatus.WAITING_HUMAN,
                    WritingJob.status == JobStatus.RESEARCH_REVIEW,
                    WritingJob.status == JobStatus.OUTLINE_REVIEW,
                    WritingJob.status == JobStatus.FINAL_REVIEW,
                )
            )
        normalized_query = query.strip() if query else ""
        if normalized_query:
            filters.append(WritingJob.topic.ilike(f"%{normalized_query}%"))

        total_result = await session.execute(
            select(func.count()).select_from(WritingJob).where(*filters)
        )
        rows_result = await session.execute(
            select(WritingJob)
            .where(*filters)
            .order_by(WritingJob.updated_at.desc(), WritingJob.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows_result.scalars().all()), int(total_result.scalar_one())
