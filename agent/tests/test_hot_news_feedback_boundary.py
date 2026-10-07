from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError

from app.activities.hot_news import HotNewsActivities
from app.domain.errors import HotNewsAnalysisAttemptError, HotNewsPersistenceError
from app.services.hot_news_orchestration import HotNewsRunRequest
from app.workflows.contracts import HotNewsActivityOutcome


def request() -> HotNewsRunRequest:
    start = datetime(2026, 9, 9, tzinfo=timezone.utc)
    return HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=start,
        window_end=start + timedelta(hours=1),
        production_bundle_version="bundle-v1",
    )


def completed(req: HotNewsRunRequest) -> HotNewsActivityOutcome:
    return HotNewsActivityOutcome(
        run_id="run-1",
        idempotency_key=req.idempotency_key,
        status="completed",
        fetched_record_count=1,
        metric_snapshot_count=1,
        ranked_news_count=1,
        analyzed_news_count=1,
    )


@pytest.mark.asyncio
async def test_completed_run_is_collected_after_durable_run_snapshot() -> None:
    req = request()
    result = SimpleNamespace(request=req)
    store = SimpleNamespace(
        get_completed=AsyncMock(return_value=None),
        save_completed=AsyncMock(return_value=completed(req)),
    )
    sink = SimpleNamespace(
        collect_completed_run=AsyncMock(),
        collect_persisted_run=AsyncMock(),
        collect_analysis_failure=AsyncMock(),
    )
    activities = HotNewsActivities(
        SimpleNamespace(run=AsyncMock(return_value=result)),
        store,
        feedback_sink=sink,
    )

    assert await activities.run_hot_news_window(req) == completed(req)

    sink.collect_completed_run.assert_awaited_once_with(
        result=result,
        run_id="run-1",
    )


@pytest.mark.asyncio
async def test_activity_replay_repairs_idempotent_feedback_collection() -> None:
    req = request()
    sink = SimpleNamespace(
        collect_completed_run=AsyncMock(),
        collect_persisted_run=AsyncMock(),
        collect_analysis_failure=AsyncMock(),
    )
    activities = HotNewsActivities(
        SimpleNamespace(run=AsyncMock()),
        SimpleNamespace(
            get_completed=AsyncMock(return_value=completed(req)),
            save_completed=AsyncMock(),
        ),
        feedback_sink=sink,
    )

    await activities.run_hot_news_window(req)

    sink.collect_persisted_run.assert_awaited_once_with(
        tenant_id="tenant-1",
        idempotency_key=req.idempotency_key,
        run_id="run-1",
    )


@pytest.mark.asyncio
async def test_validation_failure_is_sent_to_feedback_sink_before_blocking() -> None:
    req = request()
    failure = HotNewsAnalysisAttemptError(
        "unknown evidence id",
        analysis_input=object(),
        raw_content="{}",
        request_id="request-1",
    )
    sink = SimpleNamespace(
        collect_completed_run=AsyncMock(),
        collect_persisted_run=AsyncMock(),
        collect_analysis_failure=AsyncMock(),
    )
    activities = HotNewsActivities(
        SimpleNamespace(run=AsyncMock(side_effect=failure)),
        SimpleNamespace(get_completed=AsyncMock(return_value=None)),
        feedback_sink=sink,
    )

    with pytest.raises(ApplicationError) as captured:
        await activities.run_hot_news_window(req)

    assert captured.value.non_retryable is True
    sink.collect_analysis_failure.assert_awaited_once_with(
        request=req,
        error=failure,
    )


@pytest.mark.asyncio
async def test_transient_feedback_failure_requests_at_most_workflow_retry() -> None:
    req = request()
    failure = HotNewsAnalysisAttemptError(
        "invalid schema",
        analysis_input=object(),
        raw_content="{}",
    )
    sink = SimpleNamespace(
        collect_completed_run=AsyncMock(),
        collect_persisted_run=AsyncMock(),
        collect_analysis_failure=AsyncMock(
            side_effect=HotNewsPersistenceError("feedback unavailable")
        ),
    )
    activities = HotNewsActivities(
        SimpleNamespace(run=AsyncMock(side_effect=failure)),
        SimpleNamespace(get_completed=AsyncMock(return_value=None)),
        feedback_sink=sink,
    )

    with pytest.raises(ApplicationError) as captured:
        await activities.run_hot_news_window(req)

    assert captured.value.non_retryable is False
    assert captured.value.type == "HotNewsPersistenceError"
