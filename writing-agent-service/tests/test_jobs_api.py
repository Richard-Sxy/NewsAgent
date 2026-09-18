import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import httpx
import pytest

from app.api.dependencies import (
    get_artifact_store,
    get_cms_publisher,
    get_job_service,
    get_orchestrator,
    get_recovery_service,
    get_session,
)
from app.domain.job_scenario import JobScenario
from app.domain.job_status import JobStatus
from app.main import app
from app.workflows.contracts import WorkflowSnapshot
from app.schemas.checkpoint import ResumePoint
from app.services.orchestrator import WorkflowExecutionNotFoundError
from tests.test_research_schema import valid_research_package


def job(tenant_id: uuid.UUID):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        topic="新闻主题",
        scenario=JobScenario.ASSISTED_WRITING,
        requirements={},
        status=JobStatus.CREATED,
        current_step=None,
        temporal_workflow_id="workflow-1",
        research_retries=0,
        review_rounds=0,
        progress_percent=0,
        sections_completed=0,
        sections_total=0,
        created_at=now,
        updated_at=now,
        external_publication_id=None,
        published_at=None,
    )


@pytest.fixture
def api_context():
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    stored_job = job(tenant_id)
    jobs = AsyncMock()
    jobs.create_or_get.return_value = (stored_job, True)
    jobs.get.return_value = stored_job
    jobs.list_for_operations.return_value = ([stored_job], 1)
    orchestrator = AsyncMock()
    recovery = AsyncMock()
    orchestrator.get_progress.return_value = WorkflowSnapshot(
        phase="research_review",
        completed_steps=1,
        review_round=0,
        waiting_gate="research",
        last_artifact_uri="s3://bucket/research.json",
    )
    recovery.build_recovery_plan.return_value = {
        "job_id": stored_job.id,
        "can_resume": True,
        "requires_human": True,
        "reason": "等待人工决策",
        "resume_point": ResumePoint(
            job_id=stored_job.id,
            status=JobStatus.WAITING_HUMAN,
            current_step=None,
            resumable_step_id=None,
            artifacts={},
        ),
    }
    session = AsyncMock()

    async def session_override():
        yield session

    async def jobs_override():
        return jobs

    async def orchestrator_override():
        return orchestrator

    async def recovery_override():
        return recovery

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_job_service] = jobs_override
    app.dependency_overrides[get_orchestrator] = orchestrator_override
    app.dependency_overrides[get_recovery_service] = recovery_override
    yield tenant_id, user_id, stored_job, jobs, orchestrator, session
    app.dependency_overrides.clear()


def headers(tenant_id, user_id):
    return {"X-Tenant-ID": str(tenant_id), "X-User-ID": str(user_id)}


@pytest.mark.asyncio
async def test_create_job_commits_before_starting_workflow(api_context) -> None:
    tenant_id, user_id, stored_job, jobs, orchestrator, session = api_context
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.post(
        "/api/v1/jobs",
        headers=headers(tenant_id, user_id),
        json={"topic": "新闻主题", "idempotency_key": "request-0001"},
    )
    await client.aclose()

    assert response.status_code == 201
    assert response.json()["id"] == str(stored_job.id)
    session.commit.assert_awaited_once()
    orchestrator.start_job.assert_awaited_once_with(stored_job)


@pytest.mark.asyncio
async def test_create_research_package_job_passes_scenario(api_context) -> None:
    tenant_id, user_id, stored_job, jobs, orchestrator, _ = api_context
    stored_job.scenario = JobScenario.RESEARCH_PACKAGE
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.post(
        "/api/v1/jobs",
        headers=headers(tenant_id, user_id),
        json={
            "topic": "某科技公司新品发布",
            "scenario": "research_package",
            "idempotency_key": "research-package-001",
        },
    )
    await client.aclose()

    assert response.status_code == 201
    assert response.json()["scenario"] == "research_package"
    request = jobs.create_or_get.await_args.kwargs["request"]
    assert request.scenario == JobScenario.RESEARCH_PACKAGE
    orchestrator.start_job.assert_awaited_once_with(stored_job)


@pytest.mark.asyncio
async def test_operations_can_list_jobs_waiting_for_human(api_context) -> None:
    tenant_id, user_id, stored_job, jobs, _, _ = api_context
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.get(
        "/api/v1/jobs?status=waiting_human&waiting_human_only=true&query=新闻",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == str(stored_job.id)
    jobs.list_for_operations.assert_awaited_once_with(
        ANY,
        tenant_id=tenant_id,
        statuses=[JobStatus.WAITING_HUMAN],
        waiting_human_only=True,
        query="新闻",
        limit=50,
        offset=0,
    )


@pytest.mark.asyncio
async def test_resume_endpoint_passes_supplemental_research_instruction(api_context) -> None:
    tenant_id, user_id, stored_job, _, orchestrator, session = api_context
    stored_job.status = JobStatus.WAITING_HUMAN
    recovery = await get_recovery_service_override_from_app()
    recovery.prepare_resume.return_value = stored_job
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.post(
        f"/api/v1/jobs/{stored_job.id}/resume",
        headers=headers(tenant_id, user_id),
        json={
            "resume_token": "research-fix-001",
            "recovery_action": "research",
            "instruction": "补充一手体育新闻来源",
        },
    )
    await client.aclose()

    assert response.status_code == 202
    session.commit.assert_awaited_once()
    orchestrator.start_job.assert_awaited_once_with(
        stored_job,
        recovery_action="research",
        recovery_instruction="补充一手体育新闻来源",
    )


@pytest.mark.asyncio
async def test_progress_and_human_decision(api_context) -> None:
    tenant_id, user_id, stored_job, jobs, orchestrator, _ = api_context
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    progress = await client.get(
        f"/api/v1/jobs/{stored_job.id}/progress",
        headers=headers(tenant_id, user_id),
    )
    decision = await client.post(
        f"/api/v1/jobs/{stored_job.id}/decisions",
        headers=headers(tenant_id, user_id),
        json={"gate": "research", "action": "approve"},
    )
    await client.aclose()

    assert progress.status_code == 200
    assert progress.json()["workflow"]["waiting_gate"] == "research"
    assert progress.json()["workflow_status"] == "available"
    assert decision.status_code == 202
    sent = orchestrator.submit_human_decision.await_args.args[1]
    assert sent.gate == "research"
    assert sent.action == "approve"


@pytest.mark.asyncio
async def test_missing_temporal_workflow_keeps_database_progress_visible(api_context) -> None:
    tenant_id, user_id, stored_job, _, orchestrator, _ = api_context
    stored_job.status = JobStatus.WAITING_HUMAN
    orchestrator.get_progress.side_effect = WorkflowExecutionNotFoundError(
        stored_job.temporal_workflow_id
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/progress",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["status"] == "waiting_human"
    assert response.json()["workflow"] is None
    assert response.json()["workflow_status"] == "missing"


@pytest.mark.asyncio
async def test_decision_for_missing_temporal_workflow_returns_recovery_conflict(api_context) -> None:
    tenant_id, user_id, stored_job, _, orchestrator, _ = api_context
    orchestrator.submit_human_decision.side_effect = WorkflowExecutionNotFoundError(
        stored_job.temporal_workflow_id
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.post(
        f"/api/v1/jobs/{stored_job.id}/decisions",
        headers=headers(tenant_id, user_id),
        json={"gate": "research", "action": "approve"},
    )
    await client.aclose()

    assert response.status_code == 409
    assert "恢复面板" in response.json()["detail"]
    orchestrator.submit_human_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_research_metrics_are_loaded_from_latest_artifact(api_context) -> None:
    tenant_id, user_id, stored_job, _, _, session = api_context
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
        logical_key="research_package_v1",
        version=1,
    )
    session.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: artifact
    )
    store = AsyncMock()
    payload = valid_research_package()
    payload["job_id"] = str(stored_job.id)
    store.get_json.return_value = payload

    async def store_override():
        return store

    app.dependency_overrides[get_artifact_store] = store_override
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/research-metrics",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["metrics"]["fact_count"] == 2
    assert response.json()["metrics"]["unique_source_count"] == 2
    store.get_json.assert_awaited_once_with(
        storage_uri=artifact.storage_uri,
        expected_sha256=artifact.content_sha256,
    )


@pytest.mark.asyncio
async def test_research_package_can_be_viewed_as_structured_json(api_context) -> None:
    tenant_id, user_id, stored_job, _, _, session = api_context
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
        logical_key="research_package_v1",
        version=1,
    )
    session.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: artifact
    )
    store = AsyncMock()
    payload = valid_research_package()
    payload["job_id"] = str(stored_job.id)
    store.get_json.return_value = payload

    async def store_override():
        return store

    app.dependency_overrides[get_artifact_store] = store_override
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/research-package",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["job_id"] == str(stored_job.id)
    assert response.json()["facts"][0]["fact_id"] == "F001"
    assert response.json()["metrics"]["citation_coverage_percent"] == 100.0


@pytest.mark.asyncio
async def test_research_package_can_be_exported_as_markdown(api_context) -> None:
    tenant_id, user_id, stored_job, _, _, session = api_context
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
    )
    session.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: artifact
    )
    store = AsyncMock()
    payload = valid_research_package()
    payload["job_id"] = str(stored_job.id)
    store.get_json.return_value = payload

    async def store_override():
        return store

    app.dependency_overrides[get_artifact_store] = store_override
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/research-package/export?format=markdown",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.text.startswith(f"# {payload['topic']}\n")
    assert "## 关键事实" in response.text
    assert "## 事件时间线" in response.text
    assert "## 证据冲突" in response.text
    assert "## 待核实问题" in response.text
    assert "## 候选选题角度" in response.text
    assert response.headers["content-disposition"].endswith('.md"')


@pytest.mark.asyncio
async def test_research_package_returns_404_before_artifact_exists(api_context) -> None:
    tenant_id, user_id, stored_job, _, _, session = api_context
    session.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: None
    )
    store = AsyncMock()

    async def store_override():
        return store

    app.dependency_overrides[get_artifact_store] = store_override
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/research-package",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 404
    assert response.json()["detail"] == "Research Artifact 尚未生成"
    store.get_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_article_can_be_exported_as_markdown(api_context) -> None:
    tenant_id, user_id, stored_job, _, _, session = api_context
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/final.json",
        content_sha256="b" * 64,
        logical_key="final_v1",
        version=1,
    )
    session.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: artifact
    )
    store = AsyncMock()
    store.get_json.return_value = {
        "job_id": str(stored_job.id),
        "title": "RAG 新闻终稿",
        "content": "有来源约束的正文。",
        "section_ids": ["S01"],
        "used_fact_ids": ["F001"],
        "citations": [
            {"fact_id": "F001", "source_url": "https://example.com/news/1"}
        ],
    }

    async def store_override():
        return store

    app.dependency_overrides[get_artifact_store] = store_override
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/export?format=markdown",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.text == "# RAG 新闻终稿\n\n有来源约束的正文。\n"
    assert response.headers["content-disposition"].endswith('.md"')


@pytest.mark.asyncio
async def test_final_approved_article_can_be_published(api_context) -> None:
    tenant_id, user_id, stored_job, _, _, session = api_context
    stored_job.status = JobStatus.FINAL_APPROVED
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/final.json",
        content_sha256="b" * 64,
    )
    session.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: stored_job),
        SimpleNamespace(scalar_one_or_none=lambda: artifact),
        SimpleNamespace(),
    ]
    store = AsyncMock()
    store.get_json.return_value = {
        "job_id": str(stored_job.id),
        "title": "可发布终稿",
        "content": "正文",
        "section_ids": ["S01"],
        "used_fact_ids": [],
        "citations": [],
    }
    publisher = AsyncMock()
    publisher.publish.return_value = "cms-article-001"

    async def store_override():
        return store

    async def publisher_override():
        return publisher

    app.dependency_overrides[get_artifact_store] = store_override
    app.dependency_overrides[get_cms_publisher] = publisher_override
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.post(
        f"/api/v1/jobs/{stored_job.id}/publish",
        headers=headers(tenant_id, user_id),
        json={"channel": "website"},
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["external_publication_id"] == "cms-article-001"
    assert stored_job.status == JobStatus.PUBLISHED
    session.commit.assert_awaited_once()
    publisher.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_identity_headers_are_rejected(api_context) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/jobs",
        json={"topic": "主题", "idempotency_key": "request-0001"},
    )
    await client.aclose()
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_existing_started_job_is_not_started_again(api_context) -> None:
    tenant_id, user_id, stored_job, jobs, orchestrator, _ = api_context
    stored_job.status = JobStatus.RESEARCHING
    jobs.create_or_get.return_value = (stored_job, False)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.post(
        "/api/v1/jobs",
        headers=headers(tenant_id, user_id),
        json={"topic": "新闻主题", "idempotency_key": "request-0001"},
    )
    await client.aclose()

    assert response.status_code == 200
    orchestrator.start_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_plan_endpoint(api_context) -> None:
    tenant_id, user_id, stored_job, *_ = api_context
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.get(
        f"/api/v1/jobs/{stored_job.id}/recovery",
        headers=headers(tenant_id, user_id),
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["requires_human"] is True


@pytest.mark.asyncio
async def test_resume_endpoint_starts_new_idempotent_workflow(api_context) -> None:
    tenant_id, user_id, stored_job, _, orchestrator, session = api_context
    stored_job.status = JobStatus.WAITING_HUMAN
    stored_job.temporal_workflow_id = (
        f"news-writing:{tenant_id}:{stored_job.id}:resume:operator-001"
    )
    recovery = await get_recovery_service_override_from_app()
    recovery.prepare_resume.return_value = stored_job
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.post(
        f"/api/v1/jobs/{stored_job.id}/resume",
        headers=headers(tenant_id, user_id),
        json={"resume_token": "operator-001"},
    )
    await client.aclose()

    assert response.status_code == 202
    session.commit.assert_awaited_once()
    orchestrator.start_job.assert_awaited_once_with(
        stored_job,
        recovery_action=None,
        recovery_instruction=None,
    )


async def get_recovery_service_override_from_app():
    override = app.dependency_overrides[get_recovery_service]
    return await override()
