import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.execution import ArtifactType, ExecutionStatus
from app.domain.job_status import JobStatus
from app.models.artifact import WritingArtifact
from app.schemas.checkpoint import ResumePoint
from app.services.recovery import RecoveryService


@pytest.mark.asyncio
async def test_waiting_human_recovery_plan_requires_instruction() -> None:
    service = RecoveryService()
    point = ResumePoint(
        job_id=uuid.uuid4(),
        status=JobStatus.WAITING_HUMAN,
        current_step=None,
        resumable_step_id=None,
        artifacts={},
    )
    service.load_resume_point = AsyncMock(return_value=point)

    plan = await service.build_recovery_plan(
        AsyncMock(), uuid.uuid4(), point.job_id
    )

    assert plan.can_resume is True
    assert plan.requires_human is True
    assert "人工决策" in plan.reason


@pytest.mark.asyncio
async def test_prepare_resume_uses_deterministic_token_workflow_id() -> None:
    tenant_id, job_id = uuid.uuid4(), uuid.uuid4()
    job = Mock(
        id=job_id,
        tenant_id=tenant_id,
        status=JobStatus.WAITING_HUMAN,
    )
    result = Mock()
    result.scalar_one_or_none.return_value = job
    session = AsyncMock()
    no_failed_step = Mock()
    no_failed_step.scalar_one_or_none.return_value = None
    session.execute.side_effect = [result, no_failed_step]

    resumed = await RecoveryService().prepare_resume(
        session, tenant_id, job_id, "operator-001"
    )

    assert resumed is job
    assert job.temporal_workflow_id.endswith(":resume:operator-001")
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_resume_token_is_idempotent_after_job_progresses() -> None:
    tenant_id, job_id = uuid.uuid4(), uuid.uuid4()
    workflow_id = f"news-writing:{tenant_id}:{job_id}:resume:operator-001"
    job = Mock(
        id=job_id,
        tenant_id=tenant_id,
        status=JobStatus.RESEARCHING,
        temporal_workflow_id=workflow_id,
    )
    result = Mock()
    result.scalar_one_or_none.return_value = job
    session = AsyncMock()
    session.execute.return_value = result

    resumed = await RecoveryService().prepare_resume(
        session, tenant_id, job_id, "operator-001"
    )

    assert resumed is job
    session.flush.assert_not_awaited()
from tests.test_checkpoint_service import build_context


@pytest.mark.asyncio
async def test_load_resume_point_returns_latest_artifacts_and_pending_step() -> None:
    job, step, _, command = build_context()
    step.status = ExecutionStatus.PENDING
    artifact = WritingArtifact(
        id=command.job_id,
        job_id=job.id,
        step_id=step.id,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research_package",
        version=2,
        schema_version="1.0",
        storage_uri="s3://bucket/research-v2.json",
        content_sha256="c" * 64,
        content_size=100,
        artifact_metadata={},
    )
    job_result = Mock()
    job_result.scalar_one_or_none.return_value = job
    step_result = Mock()
    step_result.scalar_one_or_none.return_value = step
    artifact_result = Mock()
    artifact_result.scalars.return_value.all.return_value = [artifact]
    session = Mock()
    session.execute = AsyncMock(
        side_effect=[job_result, step_result, artifact_result]
    )

    resume = await RecoveryService().load_resume_point(
        session,
        command.tenant_id,
        command.job_id,
    )

    assert resume.resumable_step_id == step.id
    assert resume.artifacts["research_package"].version == 2
    assert resume.artifacts["research_package"].content_sha256 == "c" * 64


@pytest.mark.asyncio
async def test_resume_point_without_pending_step_is_valid() -> None:
    job, _, _, command = build_context()
    job_result = Mock()
    job_result.scalar_one_or_none.return_value = job
    step_result = Mock()
    step_result.scalar_one_or_none.return_value = None
    artifact_result = Mock()
    artifact_result.scalars.return_value.all.return_value = []
    session = Mock()
    session.execute = AsyncMock(
        side_effect=[job_result, step_result, artifact_result]
    )

    resume = await RecoveryService().load_resume_point(
        session,
        command.tenant_id,
        command.job_id,
    )
    assert resume.resumable_step_id is None
    assert resume.artifacts == {}
