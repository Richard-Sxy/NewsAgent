"""热点分析转交写作流程的服务与 API 测试。

服务层用替身验证 requirements 溯源、幂等键与工作流启动边界；
API 层用 TestClient 验证独立权限头、404/503 映射与响应结构。
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    HotNewsPermission,
    get_data_loop_gateway_token,
    get_hot_news_writing_handoff_service,
    get_session,
)
from app.domain.job_scenario import JobScenario
from app.domain.job_status import JobStatus
from app.main import create_app
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
)
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.services.hot_news_writing_handoff import (
    HANDOFF_IDEMPOTENCY_PREFIX,
    HotNewsAnalysisNotFoundError,
    HotNewsWritingHandoffOutcome,
    HotNewsWritingHandoffService,
)


TENANT_ID = uuid4()
USER_ID = uuid4()
RUN_ID = uuid4()
NEWS_ID = "news-001"
GATEWAY_TOKEN = "test-hot-news-gateway-token"
NOW = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)


def make_memory() -> HotNewsAnalysisMemory:
    analysis_input = HotNewsAnalysisInput(
        news_id=NEWS_ID,
        title="某地新能源政策落地引发关注",
        summary="政策发布后讨论量快速上升",
        content_excerpt="正文片段",
        content_type="article",
        window_start=NOW,
        window_end=NOW,
        hot_score=0.82,
        metrics=HotNewsMetrics(
            impressions=1000,
            clicks=120,
            ctr=0.12,
            unique_users=90,
            effective_consumptions=80,
            interactions=25,
        ),
        score_components=HotScoreComponents(
            click=0.30,
            consumption=0.35,
            interaction=0.10,
            growth=0.07,
        ),
        analysis_policy_version="hot-news-analysis-v1",
    )
    report = HotNewsAnalysisReport(
        news_id=NEWS_ID,
        trend_assessment="窗口内热度持续上升",
        dominant_driver="consumption",
        overall_confidence=0.72,
        limitations=["关联证据不足"],
        evidence_news_ids=["news-002"],
        applied_memory_ids=[],
    )
    return HotNewsAnalysisMemory(
        run_id=RUN_ID,
        run_idempotency_key="hot-news-run-key",
        tenant_id=str(TENANT_ID),
        news_id=NEWS_ID,
        rank=1,
        production_bundle_version="bundle-2026-09-12",
        workflow_version="hot-news-workflow-v1",
        payload_schema_version="2.0",
        analysis_input=analysis_input,
        analysis_report=report,
        fastgpt_request_id="req-1",
        usage={"total_tokens": 100},
        captured_at=NOW,
        validated_at=NOW,
        completed_at=NOW,
    )


def make_job(status: JobStatus = JobStatus.CREATED) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=TENANT_ID,
        topic="某地新能源政策落地引发关注",
        requirements={},
        status=status,
        current_step=None,
        temporal_workflow_id="news-writing:test",
        scenario=JobScenario.ASSISTED_WRITING,
        research_retries=0,
        review_rounds=0,
        progress_percent=0,
        sections_completed=0,
        sections_total=0,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_handoff_builds_traceable_requirements() -> None:
    memory = make_memory()
    run_store = SimpleNamespace(
        get_analysis_memory=AsyncMock(return_value=memory)
    )
    job = make_job()
    job_service = SimpleNamespace(
        create_or_get=AsyncMock(return_value=(job, True))
    )
    orchestrator = SimpleNamespace(start_job=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())

    service = HotNewsWritingHandoffService(
        run_store=run_store,
        job_service=job_service,
        orchestrator=orchestrator,
    )
    outcome = await service.handoff(
        session,
        tenant_id=TENANT_ID,
        created_by=USER_ID,
        run_id=RUN_ID,
        news_id=NEWS_ID,
    )

    assert outcome.created is True
    assert outcome.job is job
    request = job_service.create_or_get.await_args.kwargs["request"]
    assert request.topic == "某地新能源政策落地引发关注"
    assert request.scenario == JobScenario.ASSISTED_WRITING
    assert request.requirements["source"] == "hot_news"
    payload = request.requirements["hot_news"]
    assert payload["run_id"] == str(RUN_ID)
    assert payload["news_id"] == NEWS_ID
    assert payload["trend_assessment"] == "窗口内热度持续上升"
    assert payload["evidence_news_ids"] == ["news-002"]
    assert payload["metrics"]["impressions"] == 1000
    assert request.idempotency_key.startswith(HANDOFF_IDEMPOTENCY_PREFIX)
    orchestrator.start_job.assert_awaited_once_with(job)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_handoff_is_idempotent_per_scenario() -> None:
    memory = make_memory()
    run_store = SimpleNamespace(
        get_analysis_memory=AsyncMock(return_value=memory)
    )
    job_service = SimpleNamespace()
    service = HotNewsWritingHandoffService(
        run_store=run_store,
        job_service=job_service,
        orchestrator=SimpleNamespace(start_job=AsyncMock()),
    )

    first = service._build_request(
        memory=memory,
        scenario=JobScenario.ASSISTED_WRITING,
    )
    second = service._build_request(
        memory=memory,
        scenario=JobScenario.ASSISTED_WRITING,
    )
    other = service._build_request(
        memory=memory,
        scenario=JobScenario.RESEARCH_PACKAGE,
    )
    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key != other.idempotency_key


@pytest.mark.asyncio
async def test_handoff_does_not_restart_finished_job() -> None:
    memory = make_memory()
    run_store = SimpleNamespace(
        get_analysis_memory=AsyncMock(return_value=memory)
    )
    job = make_job(status=JobStatus.RESEARCH_REVIEW)
    job_service = SimpleNamespace(
        create_or_get=AsyncMock(return_value=(job, False))
    )
    orchestrator = SimpleNamespace(start_job=AsyncMock())

    service = HotNewsWritingHandoffService(
        run_store=run_store,
        job_service=job_service,
        orchestrator=orchestrator,
    )
    outcome = await service.handoff(
        SimpleNamespace(commit=AsyncMock()),
        tenant_id=TENANT_ID,
        created_by=USER_ID,
        run_id=RUN_ID,
        news_id=NEWS_ID,
    )

    assert outcome.created is False
    orchestrator.start_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_rejects_unknown_analysis() -> None:
    run_store = SimpleNamespace(
        get_analysis_memory=AsyncMock(return_value=None)
    )
    service = HotNewsWritingHandoffService(
        run_store=run_store,
        job_service=SimpleNamespace(create_or_get=AsyncMock()),
        orchestrator=SimpleNamespace(start_job=AsyncMock()),
    )

    try:
        await service.handoff(
            SimpleNamespace(commit=AsyncMock()),
            tenant_id=TENANT_ID,
            created_by=USER_ID,
            run_id=RUN_ID,
            news_id=NEWS_ID,
        )
    except HotNewsAnalysisNotFoundError:
        return
    raise AssertionError("expected HotNewsAnalysisNotFoundError")


async def fake_session():
    yield SimpleNamespace(commit=AsyncMock())


def client_with(
    *,
    service=None,
    permission: HotNewsPermission | None = HotNewsPermission.HANDOFF,
    bearer_token: str | None = GATEWAY_TOKEN,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_session] = fake_session
    if service is not None:
        app.dependency_overrides[get_hot_news_writing_handoff_service] = (
            lambda: service
        )

    headers: dict[str, str] = {}
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    headers["X-Tenant-ID"] = str(TENANT_ID)
    headers["X-User-ID"] = str(USER_ID)
    if permission is not None:
        headers["X-Hot-News-Roles"] = permission.value
    return TestClient(app, headers=headers)


def test_handoff_api_returns_job() -> None:
    job = make_job()
    service = SimpleNamespace(
        handoff=AsyncMock(
            return_value=HotNewsWritingHandoffOutcome(job=job, created=True)
        )
    )

    response = client_with(service=service).post(
        f"/api/v1/hot-news/runs/{RUN_ID}/news/{NEWS_ID}/handoff",
        json={"scenario": "assisted_writing"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["job"]["topic"] == "某地新能源政策落地引发关注"
    kwargs = service.handoff.await_args.kwargs
    assert kwargs["tenant_id"] == TENANT_ID
    assert kwargs["created_by"] == USER_ID
    assert kwargs["run_id"] == RUN_ID
    assert kwargs["news_id"] == NEWS_ID
    assert kwargs["scenario"] == JobScenario.ASSISTED_WRITING


def test_handoff_api_requires_handoff_permission() -> None:
    service = SimpleNamespace(handoff=AsyncMock())

    response = client_with(
        service=service,
        permission=HotNewsPermission.READ,
    ).post(
        f"/api/v1/hot-news/runs/{RUN_ID}/news/{NEWS_ID}/handoff",
        json={"scenario": "assisted_writing"},
    )

    assert response.status_code == 403
    service.handoff.assert_not_awaited()


def test_handoff_api_maps_missing_analysis_to_404() -> None:
    service = SimpleNamespace(
        handoff=AsyncMock(
            side_effect=HotNewsAnalysisNotFoundError("不存在")
        )
    )

    response = client_with(service=service).post(
        f"/api/v1/hot-news/runs/{RUN_ID}/news/{NEWS_ID}/handoff",
        json={"scenario": "assisted_writing"},
    )

    assert response.status_code == 404
