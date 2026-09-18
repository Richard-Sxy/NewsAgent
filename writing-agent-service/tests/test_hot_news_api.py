"""热点运营控制台 API 的鉴权、租户隔离与决策入口测试。

不访问真实数据库：查询服务与决策服务均以 AsyncMock 替身注入，
验证 HTTP 层的身份头契约（复用网关 Token、独立 X-Hot-News-Roles 角色头）、
错误映射与响应结构。
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import (
    HotNewsPermission,
    get_data_loop_gateway_token,
    get_hot_news_decision_service,
    get_hot_news_query_service,
)
from app.main import create_app
from app.schemas.hot_news_api import (
    HotNewsAnalysisSummaryView,
    HotNewsDecisionView,
    HotNewsMetricSnapshotView,
    HotNewsRankedItemView,
    HotNewsRunDetailResponse,
    HotNewsRunSummary,
    HotScoreView,
)
from app.services.hot_news_decision import (
    HotNewsDecisionConflictError,
    HotNewsDecisionTargetNotFoundError,
)
from app.services.hot_news_query import HotNewsQueryService


TENANT_ID = uuid4()
USER_ID = uuid4()
GATEWAY_TOKEN = "test-hot-news-gateway-token"
RUN_ID = uuid4()

WINDOW_START = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
COMPLETED_AT = datetime(2026, 9, 10, 9, 1, tzinfo=timezone.utc)


def make_run_summary() -> HotNewsRunSummary:
    return HotNewsRunSummary(
        run_id=RUN_ID,
        idempotency_key="hot-news-run-1",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        production_bundle_version="bundle-2026-09-10",
        workflow_version="hot-news-workflow-v1",
        status="completed",
        fetched_record_count=120,
        metric_snapshot_count=30,
        ranked_news_count=5,
        analyzed_news_count=5,
        completed_at=COMPLETED_AT,
    )


def make_ranked_item() -> HotNewsRankedItemView:
    return HotNewsRankedItemView(
        rank=1,
        news_id="news-001",
        title="AI 数据中心进入吉瓦时代",
        metrics=HotNewsMetricSnapshotView(
            news_id="news-001",
            content_type="article",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            impressions=1000,
            clicks=120,
            unique_users=90,
            total_duration_seconds=3600,
            effective_consumptions=80,
            interactions=25,
            ctr="0.12",
        ),
        baseline=None,
        hot_score=HotScoreView(
            score="0.8200",
            click_component="0.30",
            consumption_component="0.35",
            interaction_component="0.10",
            growth_component="0.07",
        ),
        analysis=HotNewsAnalysisSummaryView(
            trend_assessment="窗口内热度持续上升",
            dominant_driver="consumption",
            attention_reasons=(),
            operation_suggestions=(),
            limitations=("关联证据不足",),
            evidence_news_ids=("news-002",),
            overall_confidence=0.72,
            fastgpt_request_id="req-1",
            validated_at=COMPLETED_AT,
        ),
    )


def make_detail() -> HotNewsRunDetailResponse:
    return HotNewsRunDetailResponse(
        run=make_run_summary(),
        ranked_news=(make_ranked_item(),),
        decisions=(
            HotNewsDecisionView(
                decision_id=uuid4(),
                news_id="news-001",
                decision_type="accepted",
                reason="榜单与业务判断一致",
                correction_payload={},
                operator_id=str(USER_ID),
                idempotency_key="decision-1",
                supersedes_decision_id=None,
                created_at=COMPLETED_AT,
            ),
        ),
    )


def test_ranked_news_projects_title_from_analysis_input_snapshot() -> None:
    run = SimpleNamespace(
        payload_schema_version="2.0",
        result_payload={
            "ranked_news": [
                {
                    "rank": 1,
                    "current": {
                        "news_id": "news-001",
                        "content_type": "article",
                        "window_start": WINDOW_START,
                        "window_end": WINDOW_END,
                        "impressions": 1000,
                        "clicks": 120,
                        "unique_users": 90,
                        "total_duration_seconds": 3600,
                        "effective_consumptions": 80,
                        "interactions": 25,
                        "ctr": "0.12",
                    },
                    "hot_score": {
                        "score": "0.8200",
                        "click_component": "0.30",
                        "consumption_component": "0.35",
                        "interaction_component": "0.10",
                        "growth_component": "0.07",
                    },
                }
            ],
            "analyzed_news": [
                {
                    "news_id": "news-001",
                    "analysis_input": {
                        "title": "AI 数据中心进入吉瓦时代",
                    },
                    "analysis": {
                        "request_id": "req-1",
                        "value": {
                            "trend_assessment": "窗口内热度持续上升",
                            "dominant_driver": "consumption",
                            "attention_reasons": [],
                            "operation_suggestions": [],
                            "limitations": [],
                            "evidence_news_ids": [],
                            "overall_confidence": 0.72,
                        },
                    },
                    "validated_at": COMPLETED_AT,
                }
            ],
        },
    )

    items = HotNewsQueryService._parse_ranked_news(run)

    assert items[0].title == "AI 数据中心进入吉瓦时代"


def client_with(
    *,
    query_service=None,
    decision_service=None,
    permission: HotNewsPermission | None = HotNewsPermission.ADMIN,
    bearer_token: str | None = GATEWAY_TOKEN,
    include_identity: bool = True,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    if query_service is not None:
        app.dependency_overrides[get_hot_news_query_service] = (
            lambda: query_service
        )
    if decision_service is not None:
        app.dependency_overrides[get_hot_news_decision_service] = (
            lambda: decision_service
        )

    headers: dict[str, str] = {}
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if include_identity:
        headers["X-Tenant-ID"] = str(TENANT_ID)
        headers["X-User-ID"] = str(USER_ID)
    if permission is not None:
        headers["X-Hot-News-Roles"] = permission.value
    return TestClient(app, headers=headers)


def decision_payload(**overrides) -> dict:
    payload = {
        "run_id": str(RUN_ID),
        "news_id": "news-001",
        "decision_type": "accepted",
        "reason": "榜单与业务判断一致",
        "idempotency_key": "decision-request-1",
    }
    payload.update(overrides)
    return payload


def test_list_runs_returns_summaries() -> None:
    query_service = SimpleNamespace(
        list_runs=AsyncMock(return_value=(make_run_summary(),))
    )

    response = client_with(
        query_service=query_service,
        permission=HotNewsPermission.READ,
    ).get("/api/v1/hot-news/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["offset"] == 0
    assert body["limit"] == 20
    assert body["runs"][0]["run_id"] == str(RUN_ID)
    assert body["runs"][0]["ranked_news_count"] == 5
    kwargs = query_service.list_runs.await_args.kwargs
    assert kwargs["tenant_id"] == str(TENANT_ID)


def test_list_runs_rejects_missing_bearer_token() -> None:
    query_service = SimpleNamespace(list_runs=AsyncMock(return_value=()))

    response = client_with(
        query_service=query_service,
        bearer_token=None,
    ).get("/api/v1/hot-news/runs")

    assert response.status_code == 401
    query_service.list_runs.assert_not_awaited()


def test_list_runs_rejects_wrong_bearer_token() -> None:
    query_service = SimpleNamespace(list_runs=AsyncMock(return_value=()))

    response = client_with(
        query_service=query_service,
        bearer_token="not-the-gateway-token",
    ).get("/api/v1/hot-news/runs")

    assert response.status_code == 401


def test_list_runs_rejects_missing_roles_header() -> None:
    query_service = SimpleNamespace(list_runs=AsyncMock(return_value=()))

    response = client_with(
        query_service=query_service,
        permission=None,
    ).get("/api/v1/hot-news/runs")

    assert response.status_code == 403


def test_list_runs_rejects_data_loop_roles_header() -> None:
    """Data Loop 角色头不能替代热点独立角色头。"""

    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_hot_news_query_service] = (
        lambda: SimpleNamespace(list_runs=AsyncMock(return_value=()))
    )
    client = TestClient(
        app,
        headers={
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-User-ID": str(USER_ID),
            "X-Data-Loop-Roles": "data-loop:admin",
        },
    )

    response = client.get("/api/v1/hot-news/runs")

    assert response.status_code == 403


def test_get_run_detail_returns_ranking_and_decisions() -> None:
    query_service = SimpleNamespace(
        get_run_detail=AsyncMock(return_value=make_detail())
    )

    response = client_with(
        query_service=query_service,
        permission=HotNewsPermission.READ,
    ).get(f"/api/v1/hot-news/runs/{RUN_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["run_id"] == str(RUN_ID)
    item = body["ranked_news"][0]
    assert item["news_id"] == "news-001"
    assert item["title"] == "AI 数据中心进入吉瓦时代"
    assert item["metrics"]["ctr"] == "0.12"
    assert item["hot_score"]["score"] == "0.8200"
    assert item["analysis"]["dominant_driver"] == "consumption"
    assert body["decisions"][0]["decision_type"] == "accepted"


def test_get_run_detail_unknown_run_returns_404() -> None:
    """不存在的运行或跨租户访问都表现为 404，不泄露存在性。"""

    query_service = SimpleNamespace(
        get_run_detail=AsyncMock(return_value=None)
    )

    response = client_with(
        query_service=query_service,
        permission=HotNewsPermission.READ,
    ).get(f"/api/v1/hot-news/runs/{uuid4()}")

    assert response.status_code == 404


def test_record_decision_uses_authenticated_operator() -> None:
    decision = SimpleNamespace(id=uuid4(), decision_type="accepted")
    decision_service = SimpleNamespace(
        record_decision=AsyncMock(return_value=(decision, True))
    )

    response = client_with(
        decision_service=decision_service,
        permission=HotNewsPermission.DECIDE,
    ).post("/api/v1/hot-news/decisions", json=decision_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["decision_id"] == str(decision.id)
    assert body["created"] is True
    kwargs = decision_service.record_decision.await_args.kwargs
    assert kwargs["tenant_id"] == str(TENANT_ID)
    assert kwargs["operator_id"] == str(USER_ID)
    assert kwargs["command"].run_id == RUN_ID


def test_record_decision_rejects_read_only_role() -> None:
    decision_service = SimpleNamespace(record_decision=AsyncMock())

    response = client_with(
        decision_service=decision_service,
        permission=HotNewsPermission.READ,
    ).post("/api/v1/hot-news/decisions", json=decision_payload())

    assert response.status_code == 403
    decision_service.record_decision.assert_not_awaited()


def test_record_decision_idempotent_replay() -> None:
    decision = SimpleNamespace(id=uuid4(), decision_type="accepted")
    decision_service = SimpleNamespace(
        record_decision=AsyncMock(return_value=(decision, False))
    )

    response = client_with(
        decision_service=decision_service,
        permission=HotNewsPermission.DECIDE,
    ).post("/api/v1/hot-news/decisions", json=decision_payload())

    assert response.status_code == 201
    assert response.json()["created"] is False


def test_record_decision_conflict_returns_409() -> None:
    decision_service = SimpleNamespace(
        record_decision=AsyncMock(
            side_effect=HotNewsDecisionConflictError(
                "同一个 idempotency_key 对应不同的决策内容"
            )
        )
    )

    response = client_with(
        decision_service=decision_service,
        permission=HotNewsPermission.DECIDE,
    ).post("/api/v1/hot-news/decisions", json=decision_payload())

    assert response.status_code == 409


def test_record_decision_unknown_target_returns_404() -> None:
    decision_service = SimpleNamespace(
        record_decision=AsyncMock(
            side_effect=HotNewsDecisionTargetNotFoundError(
                "热点决策目标不存在"
            )
        )
    )

    response = client_with(
        decision_service=decision_service,
        permission=HotNewsPermission.DECIDE,
    ).post("/api/v1/hot-news/decisions", json=decision_payload())

    assert response.status_code == 404


def test_record_decision_corrected_requires_correction_payload() -> None:
    decision_service = SimpleNamespace(record_decision=AsyncMock())

    response = client_with(
        decision_service=decision_service,
        permission=HotNewsPermission.DECIDE,
    ).post(
        "/api/v1/hot-news/decisions",
        json=decision_payload(decision_type="corrected"),
    )

    assert response.status_code == 422
    decision_service.record_decision.assert_not_awaited()
