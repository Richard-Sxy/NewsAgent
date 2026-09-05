from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError

from app.activities.hot_news import HotNewsActivities
from app.domain.errors import FastGPTTimeoutError
from app.services.hot_news_orchestration import HotNewsRunRequest
from app.workflows.contracts import HotNewsActivityOutcome


START = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def build_request() -> HotNewsRunRequest:
    return HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=START,
        window_end=END,
        production_bundle_version="bundle-v1",
    )


def build_outcome(request: HotNewsRunRequest) -> HotNewsActivityOutcome:
    return HotNewsActivityOutcome(
        run_id="run-1",
        idempotency_key=request.idempotency_key,
        status="completed",
        fetched_record_count=100,
        metric_snapshot_count=10,
        ranked_news_count=5,
        analyzed_news_count=5,
    )


@pytest.mark.asyncio
async def test_replay_skips_orchestration_and_persistence() -> None:
    request = build_request()
    outcome = build_outcome(request)
    service = SimpleNamespace(run=AsyncMock())
    store = SimpleNamespace(
        get_completed=AsyncMock(return_value=outcome),
        save_completed=AsyncMock(),
    )

    result = await HotNewsActivities(
        service, store
    ).run_hot_news_window(request)

    assert result == outcome
    service.run.assert_not_awaited()
    store.save_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_window_runs_and_persists() -> None:
    request = build_request()
    outcome = build_outcome(request)
    orchestration_result = object()
    service = SimpleNamespace(
        run=AsyncMock(return_value=orchestration_result)
    )
    store = SimpleNamespace(
        get_completed=AsyncMock(return_value=None),
        save_completed=AsyncMock(return_value=outcome),
    )

    result = await HotNewsActivities(
        service, store
    ).run_hot_news_window(request)

    assert result == outcome
    service.run.assert_awaited_once_with(request)
    store.save_completed.assert_awaited_once_with(
        result=orchestration_result
    )


@pytest.mark.asyncio
async def test_retryable_error_remains_retryable_for_temporal() -> None:
    request = build_request()
    service = SimpleNamespace(
        run=AsyncMock(side_effect=FastGPTTimeoutError("timeout"))
    )
    store = SimpleNamespace(
        get_completed=AsyncMock(return_value=None),
        save_completed=AsyncMock(),
    )

    with pytest.raises(ApplicationError) as captured:
        await HotNewsActivities(
            service, store
        ).run_hot_news_window(request)

    assert captured.value.non_retryable is False
    store.save_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_window_is_non_retryable() -> None:
    request = HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=END,
        window_end=START,
        production_bundle_version="bundle-v1",
    )
    service = SimpleNamespace(run=AsyncMock())
    store = SimpleNamespace(
        get_completed=AsyncMock(),
        save_completed=AsyncMock(),
    )

    with pytest.raises(ApplicationError) as captured:
        await HotNewsActivities(
            service, store
        ).run_hot_news_window(request)

    assert captured.value.non_retryable is True
    service.run.assert_not_awaited()


def test_activity_name_is_stable() -> None:
    definition = (
        HotNewsActivities.run_hot_news_window
        .__temporal_activity_definition
    )
    assert definition.name == "run_hot_news_window"
