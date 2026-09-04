import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.domain.execution import AgentType, ArtifactType, ExecutionStatus, StepType
from app.domain.job_status import JobStatus
from app.models.agent_run import AgentRun
from app.models.artifact import WritingArtifact
from app.models.job import WritingJob
from app.models.step import WritingStep
from app.schemas.checkpoint import CheckpointCommit, FailureCommit
from app.schemas.events import ProgressEvent
from app.services.checkpoint import CheckpointError, CheckpointService


def build_context():
    tenant_id = uuid.uuid4()
    job_id = uuid.uuid4()
    step_id = uuid.uuid4()
    run_id = uuid.uuid4()
    job = WritingJob(
        id=job_id,
        tenant_id=tenant_id,
        created_by=uuid.uuid4(),
        idempotency_key="create-1",
        temporal_workflow_id=f"writing-{job_id}",
        topic="新闻主题",
        requirements={},
        status=JobStatus.RESEARCHING,
        current_step=StepType.RESEARCH,
        research_retries=0,
        review_rounds=0,
        version=1,
    )
    step = WritingStep(
        id=step_id,
        job_id=job_id,
        step_type=StepType.RESEARCH,
        step_key="research",
        attempt=1,
        status=ExecutionStatus.RUNNING,
        input_snapshot={},
    )
    run = AgentRun(
        id=run_id,
        job_id=job_id,
        step_id=step_id,
        agent_type=AgentType.RESEARCH,
        status=ExecutionStatus.RUNNING,
        idempotency_key="research:1",
        fastgpt_app_id="research-app",
        input_artifact_ids=[],
    )
    command = CheckpointCommit.model_validate(
        {
            "tenant_id": tenant_id,
            "job_id": job_id,
            "step_id": step_id,
            "agent_run_id": run_id,
            "target_status": "research_review",
            "next_step": None,
            "artifact": {
                "artifact_type": "research_package",
                "logical_key": "research_package",
                "schema_version": "1.0",
                "storage_uri": "s3://news-writing/job/research-v1.json",
                "content_sha256": "a" * 64,
                "content_size": 1024,
                "metadata": {"fact_count": 3},
            },
        }
    )
    return job, step, run, command


@pytest.mark.asyncio
async def test_review_checkpoint_updates_review_round_counter() -> None:
    job, step, run, command = build_context()
    job.status = JobStatus.REVIEWING
    step.step_type = StepType.REVIEW
    command = command.model_copy(
        update={
            "target_status": JobStatus.FINAL_REVIEW,
            "review_round": 2,
        }
    )
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    service._next_artifact_version = AsyncMock(return_value=1)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    await service.commit_success(session, command)

    assert job.review_rounds == 2


@pytest.mark.asyncio
async def test_checkpoint_persists_monotonic_progress_from_outbox_event() -> None:
    job, step, run, command = build_context()
    job.progress_percent = 10
    job.sections_completed = 0
    job.sections_total = 0
    outbox = AsyncMock()
    outbox.enqueue_step_completed.return_value = ProgressEvent(
        event="step.completed",
        deduplication_key="research:completed",
        tenant_id=job.tenant_id,
        job_id=job.id,
        step=StepType.RESEARCH,
        status=JobStatus.RESEARCH_REVIEW,
        progress={"completed": 1, "total": 1, "percent": 15},
        occurred_at=datetime.now(timezone.utc),
    )
    service = CheckpointService(outbox)
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    service._next_artifact_version = AsyncMock(return_value=1)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    await service.commit_success(session, command)

    assert job.progress_percent == 15
    assert job.sections_completed == 0
    outbox.enqueue_step_completed.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_success_updates_all_checkpoint_records() -> None:
    job, step, run, command = build_context()
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    service._next_artifact_version = AsyncMock(return_value=1)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    artifact = await service.commit_success(session, command)

    assert artifact.artifact_type == ArtifactType.RESEARCH_PACKAGE
    assert artifact.version == 1
    assert run.status == ExecutionStatus.SUCCEEDED
    assert run.output_artifact_id == artifact.id
    assert run.fastgpt_request_id == command.fastgpt_request_id
    assert run.prompt_tokens == command.prompt_tokens
    assert run.completion_tokens == command.completion_tokens
    assert step.status == ExecutionStatus.SUCCEEDED
    assert job.status == JobStatus.RESEARCH_REVIEW
    assert job.current_step is None
    session.add.assert_called_once_with(artifact)
    assert session.flush.await_count == 2


@pytest.mark.asyncio
async def test_commit_rejects_non_running_step() -> None:
    job, step, run, command = build_context()
    step.status = ExecutionStatus.FAILED
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)

    with pytest.raises(CheckpointError, match="只有 running 步骤"):
        await service.commit_success(Mock(), command)


@pytest.mark.asyncio
async def test_successful_run_is_idempotently_replayed() -> None:
    job, step, run, command = build_context()
    artifact = WritingArtifact(
        id=uuid.uuid4(),
        job_id=job.id,
        step_id=step.id,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research_package",
        version=1,
        schema_version="1.0",
        storage_uri="s3://bucket/research.json",
        content_sha256="b" * 64,
        content_size=10,
        artifact_metadata={},
    )
    run.status = ExecutionStatus.SUCCEEDED
    run.output_artifact_id = artifact.id
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    session = Mock()
    session.get = AsyncMock(return_value=artifact)

    result = await service.commit_success(session, command)
    assert result is artifact


def test_artifact_hash_must_be_sha256_hex() -> None:
    _, _, _, command = build_context()
    data = command.model_dump(mode="json")
    data["artifact"]["content_sha256"] = "invalid"
    with pytest.raises(ValidationError):
        CheckpointCommit.model_validate(data)


def failure_command(command: CheckpointCommit, retryable: bool = True) -> FailureCommit:
    return FailureCommit(
        tenant_id=command.tenant_id,
        job_id=command.job_id,
        step_id=command.step_id,
        agent_run_id=command.agent_run_id,
        error_code="FASTGPT_TIMEOUT",
        error_message="FastGPT 请求超时",
        retryable=retryable,
    )


@pytest.mark.asyncio
async def test_research_failure_creates_next_attempt() -> None:
    job, step, run, command = build_context()
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    outcome = await service.commit_failure(session, failure_command(command))

    assert outcome.action == "retry"
    assert outcome.next_attempt == 2
    assert job.research_retries == 1
    assert step.status == ExecutionStatus.FAILED
    assert run.status == ExecutionStatus.FAILED
    retry_step = session.add.call_args.args[0]
    assert retry_step.status == ExecutionStatus.PENDING
    assert retry_step.step_key == "research"


@pytest.mark.asyncio
async def test_research_retry_limit_routes_to_human() -> None:
    job, step, run, command = build_context()
    job.research_retries = 2
    step.attempt = 3
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    outcome = await service.commit_failure(session, failure_command(command))

    assert outcome.action == "waiting_human"
    assert job.status == JobStatus.WAITING_HUMAN
    assert job.current_step is None
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_non_retryable_failure_routes_to_human_immediately() -> None:
    job, step, run, command = build_context()
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    outcome = await service.commit_failure(
        session,
        failure_command(command, retryable=False),
    )
    assert outcome.action == "waiting_human"
    assert job.status == JobStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_failure_replay_does_not_increment_retry_count() -> None:
    job, step, run, command = build_context()
    job.research_retries = 1
    step.status = ExecutionStatus.FAILED
    run.status = ExecutionStatus.FAILED
    retry_step = WritingStep(
        id=uuid.uuid4(),
        job_id=job.id,
        step_type=StepType.RESEARCH,
        step_key="research",
        attempt=2,
        status=ExecutionStatus.PENDING,
        input_snapshot={},
    )
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    result = Mock()
    result.scalar_one_or_none.return_value = retry_step
    session = Mock()
    session.execute = AsyncMock(return_value=result)

    outcome = await service.commit_failure(session, failure_command(command))
    assert outcome.action == "retry"
    assert outcome.retry_step_id == retry_step.id
    assert job.research_retries == 1


@pytest.mark.asyncio
async def test_second_section_rewrite_failure_routes_to_human() -> None:
    job, step, run, command = build_context()
    job.status = JobStatus.REVISING
    job.current_step = StepType.SECTION_REVISE
    step.step_type = StepType.SECTION_REVISE
    step.step_key = "revise:S01"
    step.attempt = 2
    service = CheckpointService()
    service._lock_job = AsyncMock(return_value=job)
    service._lock_step = AsyncMock(return_value=step)
    service._lock_agent_run = AsyncMock(return_value=run)
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()

    outcome = await service.commit_failure(session, failure_command(command))
    assert outcome.action == "waiting_human"
    assert job.status == JobStatus.WAITING_HUMAN
