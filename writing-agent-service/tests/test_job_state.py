import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.domain.execution import StepType
from app.domain.job_status import JobStatus
from app.services.job_state import JobStateHandler
from app.workflows.contracts import JobStateCommand


class FakeDatabase:
    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


@pytest.mark.asyncio
async def test_control_transition_updates_database_job() -> None:
    job = type("Job", (), {})()
    job.status = JobStatus.RESEARCH_REVIEW
    job.current_step = StepType.RESEARCH
    result = type("Result", (), {"scalar_one_or_none": lambda self: job})()
    session = AsyncMock()
    session.execute.return_value = result
    handler = JobStateHandler(FakeDatabase(session))

    await handler(
        JobStateCommand(
            tenant_id=str(uuid.uuid4()),
            job_id=str(uuid.uuid4()),
            target_status="cancelled",
        )
    )

    assert job.status == JobStatus.CANCELLED
    assert job.current_step is None
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_transition_enqueues_failed_event() -> None:
    job = type("Job", (), {})()
    job.id = uuid.uuid4()
    job.status = JobStatus.REVIEWING
    job.current_step = StepType.REVIEW
    result = type("Result", (), {"scalar_one_or_none": lambda self: job})()
    session = AsyncMock()
    session.execute.return_value = result
    outbox = AsyncMock()
    handler = JobStateHandler(FakeDatabase(session), outbox)

    await handler(
        JobStateCommand(
            tenant_id=str(uuid.uuid4()),
            job_id=str(job.id),
            target_status="failed",
            reason="ValueError: invalid transition",
        )
    )

    assert job.status == JobStatus.FAILED
    assert job.current_step is None
    event = outbox.enqueue_job_event.await_args.kwargs
    assert event["event_name"] == "job.failed"
    assert event["message"] == "ValueError: invalid transition"


@pytest.mark.asyncio
async def test_research_completion_is_persisted_and_emits_completed_event() -> None:
    job = type("Job", (), {})()
    job.id = uuid.uuid4()
    job.status = JobStatus.RESEARCH_REVIEW
    job.current_step = StepType.RESEARCH
    job.progress_percent = 15
    result = type("Result", (), {"scalar_one_or_none": lambda self: job})()
    session = AsyncMock()
    session.execute.return_value = result
    outbox = AsyncMock()
    handler = JobStateHandler(FakeDatabase(session), outbox)

    await handler(
        JobStateCommand(
            tenant_id=str(uuid.uuid4()),
            job_id=str(job.id),
            target_status="research_completed",
        )
    )

    assert job.status == JobStatus.RESEARCH_COMPLETED
    assert job.current_step is None
    assert job.progress_percent == 100
    event = outbox.enqueue_job_event.await_args.kwargs
    assert event["event_name"] == "job.completed"
