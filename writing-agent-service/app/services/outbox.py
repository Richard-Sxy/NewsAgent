import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError

from app.config import Settings
from app.db.session import Database
from app.domain.execution import StepType
from app.domain.job_status import JobStatus
from app.models.artifact import WritingArtifact
from app.models.outbox import OutboxEvent
from app.models.step import WritingStep
from app.schemas.events import ProgressEvent
from app.services.event_publisher import ProgressPublishError, RedisProgressPublisher
from app.services.progress import ProgressCalculator, ProgressContext


class OutboxService:
    """在现有 PostgreSQL 事务中幂等写入进度事件。"""

    def __init__(self, calculator: ProgressCalculator | None = None) -> None:
        self.calculator = calculator or ProgressCalculator()

    async def enqueue_step_completed(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        step: WritingStep,
        artifact: WritingArtifact,
        status: JobStatus,
        occurred_at: datetime,
        previous_percent: int = 0,
    ) -> ProgressEvent:
        progress_data = step.input_snapshot.get("progress", {})
        progress = self.calculator.calculate(
            ProgressContext(
                step=step.step_type,
                completed_sections=progress_data.get("completed_sections", 0),
                total_sections=progress_data.get("total_sections", 0),
                previous_percent=max(
                    progress_data.get("previous_percent", 0),
                    previous_percent,
                ),
            )
        )
        section_id = step.input_snapshot.get("section_id")
        event = ProgressEvent(
            event="step.completed",
            deduplication_key=(
                f"{job_id}:{step.step_key}:{step.attempt}:completed"
            ),
            tenant_id=tenant_id,
            job_id=job_id,
            step=step.step_type,
            section_id=section_id,
            status=status,
            progress=progress,
            artifact={
                "artifact_id": artifact.id,
                "logical_key": artifact.logical_key,
                "version": artifact.version,
            },
            occurred_at=occurred_at,
        )
        await self.enqueue(session, event)
        return event

    async def enqueue_step_failed(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        job,
        step: WritingStep,
        message: str,
        occurred_at: datetime,
    ) -> None:
        event = ProgressEvent(
            event="step.failed",
            deduplication_key=f"{job.id}:{step.step_key}:{step.attempt}:failed",
            tenant_id=tenant_id,
            job_id=job.id,
            step=step.step_type,
            section_id=step.input_snapshot.get("section_id"),
            status=job.status,
            progress={
                "completed": job.sections_completed or 0,
                "total": job.sections_total or 0,
                "percent": job.progress_percent or 0,
            },
            message=message,
            occurred_at=occurred_at,
        )
        await self.enqueue(session, event)

    async def enqueue_job_event(
        self,
        session: AsyncSession,
        *,
        event_name: str,
        tenant_id: uuid.UUID,
        job,
        deduplication_key: str,
        occurred_at: datetime,
        message: str | None = None,
    ) -> None:
        event = ProgressEvent(
            event=event_name,
            deduplication_key=deduplication_key,
            tenant_id=tenant_id,
            job_id=job.id,
            step=job.current_step,
            status=job.status,
            progress={
                "completed": job.sections_completed or 0,
                "total": job.sections_total or 0,
                "percent": job.progress_percent or 0,
            },
            message=message,
            occurred_at=occurred_at,
        )
        await self.enqueue(session, event)

    @staticmethod
    async def enqueue(session: AsyncSession, event: ProgressEvent) -> None:
        statement = (
            insert(OutboxEvent)
            .values(
                id=uuid.uuid4(),
                tenant_id=event.tenant_id,
                job_id=event.job_id,
                event_type=event.event,
                deduplication_key=event.deduplication_key,
                payload=event.model_dump(mode="json", exclude={"event_id"}),
                status="pending",
                attempts=0,
                available_at=event.occurred_at,
            )
            .on_conflict_do_nothing(
                index_elements=[OutboxEvent.deduplication_key]
            )
        )
        await session.execute(statement)


class OutboxRelay:
    """可多副本运行的 PostgreSQL Outbox 到 Redis Stream Relay。"""

    def __init__(
        self,
        database: Database,
        publisher: RedisProgressPublisher,
        settings: Settings,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.batch_size = settings.outbox_batch_size
        self.max_attempts = settings.outbox_max_attempts
        self.poll_interval = settings.outbox_poll_interval_seconds

    async def process_batch(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "pending",
                    OutboxEvent.available_at <= datetime.now(timezone.utc),
                )
                .order_by(OutboxEvent.created_at)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            )
            events = list(result.scalars().all())
            for outbox_event in events:
                await self._publish_one(outbox_event)
            return len(events)

    async def _publish_one(self, outbox_event: OutboxEvent) -> None:
        try:
            event = ProgressEvent.model_validate(outbox_event.payload)
        except ValidationError as exc:
            outbox_event.attempts += 1
            outbox_event.status = "dead_letter"
            outbox_event.last_error = f"invalid event payload: {exc}"[:8000]
            return
        try:
            await self.publisher.publish(event)
        except ProgressPublishError as exc:
            outbox_event.attempts += 1
            outbox_event.last_error = str(exc)[:8000]
            if outbox_event.attempts >= self.max_attempts:
                outbox_event.status = "dead_letter"
            else:
                delay = min(2 ** outbox_event.attempts, 300)
                outbox_event.available_at = datetime.now(timezone.utc) + timedelta(
                    seconds=delay
                )
            return
        outbox_event.status = "published"
        outbox_event.published_at = datetime.now(timezone.utc)
        outbox_event.last_error = None

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        while not stop_event.is_set():
            processed = await self.process_batch()
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
