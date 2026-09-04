import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.execution import ArtifactType, ExecutionStatus, StepType
from app.domain.job_status import JobStatus
from app.models.artifact import WritingArtifact
from app.models.outbox import OutboxEvent
from app.models.step import WritingStep
from app.services.event_publisher import ProgressPublishError
from app.services.outbox import OutboxRelay, OutboxService


@pytest.mark.asyncio
async def test_checkpoint_event_is_enqueued_with_section_progress() -> None:
    session = Mock()
    session.execute = AsyncMock()
    job_id, step_id = uuid.uuid4(), uuid.uuid4()
    step = WritingStep(
        id=step_id,
        job_id=job_id,
        step_type=StepType.SECTION_DRAFT,
        step_key="section_S02_v1",
        attempt=1,
        status=ExecutionStatus.SUCCEEDED,
        input_snapshot={
            "section_id": "S02",
            "progress": {
                "completed_sections": 2,
                "total_sections": 4,
                "previous_percent": 25,
            },
        },
    )
    artifact = WritingArtifact(
        id=uuid.uuid4(),
        job_id=job_id,
        step_id=step_id,
        artifact_type=ArtifactType.SECTION,
        logical_key="section_S02_v1",
        version=1,
        schema_version="1",
        storage_uri="s3://bucket/section.json",
        content_sha256="a" * 64,
        content_size=100,
        artifact_metadata={},
    )

    await OutboxService().enqueue_step_completed(
        session,
        tenant_id=uuid.uuid4(),
        job_id=job_id,
        step=step,
        artifact=artifact,
        status=JobStatus.DRAFTING,
        occurred_at=datetime.now(timezone.utc),
    )

    statement = session.execute.await_args.args[0]
    parameters = statement.compile().params
    assert parameters["payload"]["progress"]["percent"] == 45
    assert parameters["payload"]["section_id"] == "S02"


class FakeDatabase:
    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


def relay_settings(max_attempts=3):
    return SimpleNamespace(
        outbox_batch_size=100,
        outbox_max_attempts=max_attempts,
        outbox_poll_interval_seconds=0.1,
    )


def outbox_event(attempts=0):
    now = datetime.now(timezone.utc)
    return OutboxEvent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        event_type="step.completed",
        deduplication_key="job:research:1:completed",
        payload={
            "event": "step.completed",
            "deduplication_key": "job:research:1:completed",
            "tenant_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
            "step": "research",
            "status": "research_review",
            "progress": {"completed": 1, "total": 1, "percent": 15},
            "occurred_at": now.isoformat(),
        },
        status="pending",
        attempts=attempts,
        available_at=now,
    )


@pytest.mark.asyncio
async def test_relay_marks_event_published() -> None:
    event = outbox_event()
    scalar_result = Mock()
    scalar_result.scalars.return_value.all.return_value = [event]
    session = AsyncMock()
    session.execute.return_value = scalar_result
    publisher = AsyncMock()
    relay = OutboxRelay(FakeDatabase(session), publisher, relay_settings())

    processed = await relay.process_batch()

    assert processed == 1
    assert event.status == "published"
    assert event.published_at is not None
    publisher.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_relay_retries_then_dead_letters() -> None:
    event = outbox_event(attempts=2)
    publisher = AsyncMock()
    publisher.publish.side_effect = ProgressPublishError("redis unavailable")
    relay = OutboxRelay(Mock(), publisher, relay_settings(max_attempts=3))

    await relay._publish_one(event)

    assert event.attempts == 3
    assert event.status == "dead_letter"
    assert "redis unavailable" in event.last_error


@pytest.mark.asyncio
async def test_invalid_historical_payload_is_dead_lettered_without_redis_call() -> None:
    event = outbox_event()
    event.payload = {"event": "step.completed"}
    publisher = AsyncMock()
    relay = OutboxRelay(Mock(), publisher, relay_settings())

    await relay._publish_one(event)

    assert event.status == "dead_letter"
    assert "invalid event payload" in event.last_error
    publisher.publish.assert_not_awaited()


def test_outbox_model_has_pending_scan_index_and_dedup_constraint() -> None:
    indexes = {index.name for index in OutboxEvent.__table__.indexes}
    constraints = {
        constraint.name for constraint in OutboxEvent.__table__.constraints
    }
    assert "ix_outbox_events_pending_available" in indexes
    assert "uq_outbox_events_deduplication_key" in constraints
