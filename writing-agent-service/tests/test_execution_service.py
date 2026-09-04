import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.execution import AgentType, ExecutionStatus, StepType
from app.domain.job_status import JobStatus
from app.services.execution import ExecutionService


def scalar_result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.asyncio
async def test_pending_recovery_attempt_uses_restarted_workflow_input() -> None:
    job_id = uuid.uuid4()
    job = SimpleNamespace(
        id=job_id,
        status=JobStatus.DRAFTING,
        current_step=StepType.SECTION_DRAFT,
    )
    step = SimpleNamespace(
        id=uuid.uuid4(),
        job_id=job_id,
        step_type=StepType.SECTION_DRAFT,
        step_key="section_S01_v1",
        attempt=2,
        status=ExecutionStatus.PENDING,
        input_snapshot={
            "section_id": "S01",
            "progress": {"completed_sections": 1, "total_sections": 0},
        },
        started_at=None,
    )
    session = AsyncMock()
    session.add = Mock()
    session.execute.side_effect = [
        scalar_result(job),
        scalar_result(step),
        scalar_result(None),
    ]
    current_input = {
        "section_id": "S01",
        "progress": {"completed_sections": 1, "total_sections": 4},
    }

    prepared = await ExecutionService().prepare(
        session,
        tenant_id=uuid.uuid4(),
        job_id=job_id,
        step_type=StepType.SECTION_DRAFT,
        step_key="section_S01_v1",
        requested_attempt=1,
        input_snapshot=current_input,
        agent_type=AgentType.WRITER,
        fastgpt_app_id="writer-app",
        active_status=JobStatus.DRAFTING,
    )

    assert prepared.attempt == 2
    assert step.input_snapshot == current_input
    assert step.status == ExecutionStatus.RUNNING
