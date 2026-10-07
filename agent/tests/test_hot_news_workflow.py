from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.converter import DataConverter

from app.services.hot_news_orchestration import HotNewsRunRequest
from app.workflows.contracts import HotNewsActivityOutcome
from app.workflows.hot_news import (
    HOT_NEWS_WORKFLOW_NAME,
    HotNewsMonitorWorkflow,
)


START = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def build_request() -> HotNewsRunRequest:
    return HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=START,
        window_end=END,
        production_bundle_version="bundle-v1",
        workflow_version=HOT_NEWS_WORKFLOW_NAME,
    )


def build_outcome(
    request: HotNewsRunRequest,
) -> HotNewsActivityOutcome:
    return HotNewsActivityOutcome(
        run_id="run-1",
        idempotency_key=request.idempotency_key,
        status="completed",
        fetched_record_count=100,
        metric_snapshot_count=10,
        ranked_news_count=5,
        analyzed_news_count=5,
    )


def test_workflow_name_is_stable() -> None:
    definition = (
        HotNewsMonitorWorkflow
        .__temporal_workflow_definition
    )

    assert definition.name == "hot-news-workflow-v1"


@pytest.mark.asyncio
async def test_workflow_executes_one_bounded_activity() -> None:
    request = build_request()
    expected = build_outcome(request)
    execute_activity = AsyncMock(return_value=expected)

    with patch(
        "app.workflows.hot_news.workflow.execute_activity",
        execute_activity,
    ):
        result = await HotNewsMonitorWorkflow().run(request)

    assert result == expected

    execute_activity.assert_awaited_once()

    call = execute_activity.await_args

    assert call.args == (
        "run_hot_news_window",
        request,
    )
    assert call.kwargs["start_to_close_timeout"] == timedelta(
        minutes=45
    )
    assert call.kwargs["schedule_to_close_timeout"] == timedelta(
        minutes=90
    )
    assert call.kwargs["heartbeat_timeout"] == timedelta(
        seconds=30
    )
    assert call.kwargs["retry_policy"].maximum_attempts == 2
    assert call.kwargs["result_type"] is HotNewsActivityOutcome


@pytest.mark.asyncio
async def test_workflow_contracts_round_trip_through_temporal() -> None:
    request = build_request()
    outcome = build_outcome(request)

    request_payloads = await DataConverter.default.encode([request])
    decoded_request = await DataConverter.default.decode(
        request_payloads,
        [HotNewsRunRequest],
    )

    outcome_payloads = await DataConverter.default.encode([outcome])
    decoded_outcome = await DataConverter.default.decode(
        outcome_payloads,
        [HotNewsActivityOutcome],
    )

    assert decoded_request == [request]
    assert decoded_outcome == [outcome]