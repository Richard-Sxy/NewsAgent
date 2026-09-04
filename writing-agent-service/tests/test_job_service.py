import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.job_scenario import JobScenario
from app.schemas.job import CreateWritingJobRequest
from app.domain.job_status import JobStatus
from app.services.job import JobNotFoundError, JobService
from app.services.outbox import OutboxService


def result(*, scalar=None, scalar_one=None):
    value = SimpleNamespace()
    value.scalar_one_or_none = lambda: scalar
    value.scalar_one = lambda: scalar_one
    return value


@pytest.mark.asyncio
async def test_idempotency_conflict_returns_existing_job() -> None:
    existing = SimpleNamespace(id=uuid.uuid4())
    session = AsyncMock()
    session.execute.side_effect = [
        result(scalar=None),
        result(scalar_one=existing),
    ]

    job, created = await JobService().create_or_get(
        session,
        tenant_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        request=CreateWritingJobRequest(
            topic="主题",
            idempotency_key="request-0001",
        ),
    )

    assert job is existing
    assert created is False
    assert session.execute.await_count == 2
    insert_statement = session.execute.await_args_list[0].args[0]
    assert insert_statement.compile().params["scenario"] == JobScenario.ASSISTED_WRITING


def test_job_scenario_defaults_to_assisted_writing() -> None:
    request = CreateWritingJobRequest(
        topic="测试主题",
        idempotency_key="request-0001",
    )

    assert request.scenario == JobScenario.ASSISTED_WRITING


def test_job_request_rejects_short_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        CreateWritingJobRequest(topic="测试主题", idempotency_key="short")


@pytest.mark.asyncio
async def test_get_enforces_tenant_scope() -> None:
    session = AsyncMock()
    session.execute.return_value = result(scalar=None)

    with pytest.raises(JobNotFoundError):
        await JobService().get(
            session,
            tenant_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
        )


def test_job_service_keeps_injected_outbox_service() -> None:
    outbox = OutboxService()
    assert JobService(outbox).outbox_service is outbox


@pytest.mark.asyncio
async def test_list_for_operations_returns_rows_and_total() -> None:
    first = SimpleNamespace(id=uuid.uuid4())
    second = SimpleNamespace(id=uuid.uuid4())
    count_result = SimpleNamespace(scalar_one=lambda: 2)
    rows_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [first, second])
    )
    session = AsyncMock()
    session.execute.side_effect = [count_result, rows_result]

    rows, total = await JobService().list_for_operations(
        session,
        tenant_id=uuid.uuid4(),
        statuses=[JobStatus.WAITING_HUMAN],
        waiting_human_only=True,
        query="  财经  ",
        limit=20,
        offset=10,
    )

    assert rows == [first, second]
    assert total == 2
    assert session.execute.await_count == 2
