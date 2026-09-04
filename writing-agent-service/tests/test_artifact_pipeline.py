import uuid
from unittest.mock import AsyncMock

import pytest

from app.clients.fastgpt import AgentResult
from app.domain.errors import ArtifactStoreError
from app.domain.execution import ArtifactType, StepType
from app.domain.job_status import JobStatus
from app.schemas.checkpoint import ArtifactDescriptor
from app.schemas.research import ResearchPackage
from app.services.artifact_pipeline import ArtifactPipeline


@pytest.mark.asyncio
async def test_pipeline_uploads_before_committing_checkpoint() -> None:
    descriptor = ArtifactDescriptor(
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research-package",
        schema_version="1",
        storage_uri="s3://bucket/news-writing/result.json",
        content_sha256="a" * 64,
        content_size=42,
    )
    artifact_store = AsyncMock()
    artifact_store.put_json.return_value = descriptor
    checkpoint_service = AsyncMock()
    persisted_artifact = object()
    checkpoint_service.commit_success.return_value = persisted_artifact
    pipeline = ArtifactPipeline(artifact_store, checkpoint_service)
    ids = [uuid.uuid4() for _ in range(4)]
    result = AgentResult(
        value=ResearchPackage(job_id="job-1", topic="主题"),
        request_id="req-1",
        usage={"prompt_tokens": 12, "completion_tokens": 8},
        raw_content="{}",
    )

    actual = await pipeline.persist_success(
        session=AsyncMock(),
        tenant_id=ids[0],
        job_id=ids[1],
        step_id=ids[2],
        agent_run_id=ids[3],
        result=result,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research-package",
        schema_version="1",
        target_status=JobStatus.OUTLINING,
        next_step=StepType.OUTLINE,
    )

    assert actual is persisted_artifact
    upload = artifact_store.put_json.await_args.kwargs
    assert upload["payload"]["topic"] == "主题"
    assert upload["metadata"]["request_id"] == "req-1"
    command = checkpoint_service.commit_success.await_args.args[1]
    assert command.artifact == descriptor
    assert command.next_step == StepType.OUTLINE


@pytest.mark.asyncio
async def test_pipeline_does_not_checkpoint_when_upload_fails() -> None:
    artifact_store = AsyncMock()
    artifact_store.put_json.side_effect = ArtifactStoreError("S3 unavailable")
    checkpoint_service = AsyncMock()
    pipeline = ArtifactPipeline(artifact_store, checkpoint_service)

    with pytest.raises(ArtifactStoreError):
        await pipeline.persist_success(
            session=AsyncMock(),
            tenant_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            step_id=uuid.uuid4(),
            agent_run_id=uuid.uuid4(),
            result=AgentResult(
                value=ResearchPackage(job_id="job-1", topic="主题"),
                request_id=None,
                usage={},
                raw_content="{}",
            ),
            artifact_type=ArtifactType.RESEARCH_PACKAGE,
            logical_key="research-package",
            schema_version="1",
            target_status=JobStatus.OUTLINING,
            next_step=StepType.OUTLINE,
        )

    checkpoint_service.commit_success.assert_not_awaited()
