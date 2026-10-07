"""Concurrency guarantees tested with barriers, without network or timing races."""

import asyncio
from types import SimpleNamespace

import pytest

from app.model_runtime.http import ModelTransportError
from app.services.hot_news_analysis_batch import HotNewsAnalysisBatch


@pytest.mark.asyncio
async def test_overlaps_up_to_limit_and_preserves_input_order():
    started = [asyncio.Event() for _ in range(6)]
    release = [asyncio.Event() for _ in range(6)]
    finished = []

    async def analyze(item):
        started[item].set()
        await release[item].wait()
        finished.append(item)
        return item

    batch = HotNewsAnalysisBatch(max_concurrency=2)
    task = asyncio.create_task(batch.analyze(list(range(6)), service=SimpleNamespace(analyze_with_snapshot=analyze)))
    async with asyncio.timeout(5):
        await started[0].wait()
        await started[1].wait()
        assert not started[2].is_set()
        # Keep the first item waiting while the other worker processes all others.
        for index in range(1, 6):
            release[index].set()
            if index < 5:
                await started[index + 1].wait()
        release[0].set()
        result, stats = await task
    assert result == list(range(6))
    assert finished[:4] == [1, 2, 3, 4]
    assert stats.observed_concurrency == 2
    assert stats.task_count == 6


@pytest.mark.asyncio
async def test_two_runs_share_one_worker_limit():
    ready, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def analyze(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            ready.set()
        try:
            await release.wait()
            return item
        finally:
            active -= 1

    batch = HotNewsAnalysisBatch(max_concurrency=2)
    service = SimpleNamespace(analyze_with_snapshot=analyze)
    tasks = [asyncio.create_task(batch.analyze(items, service=service))
             for items in (["a", "b", "c"], ["d", "e", "f"])]
    async with asyncio.timeout(5):
        await ready.wait()
        assert active == 2
        release.set()
        results = await asyncio.gather(*tasks)
    assert peak == 2
    assert [result for result, _ in results] == [["a", "b", "c"], ["d", "e", "f"]]


@pytest.mark.asyncio
async def test_failure_cancels_siblings_drains_cleanup_and_preserves_typed_error():
    sibling_started, cleaned = asyncio.Event(), asyncio.Event()
    called = []
    error = ModelTransportError("test failure", retryable=True, request_id="request-failed")

    async def analyze(item):
        called.append(item)
        if item == "fail":
            await sibling_started.wait()
            raise error
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Cleanup must finish before the error escapes to Temporal.
            await asyncio.sleep(0)
            cleaned.set()

    batch = HotNewsAnalysisBatch(max_concurrency=2)
    async with asyncio.timeout(5):
        with pytest.raises(ModelTransportError) as caught:
            await batch.analyze(["fail", "blocked", "never-started"],
                                service=SimpleNamespace(analyze_with_snapshot=analyze))
    assert caught.value is error
    assert called == ["fail", "blocked"]
    assert cleaned.is_set()

    async def succeeds(item):
        await asyncio.sleep(0)
        return item

    # Failed batches release every permit; subsequent runs still make progress.
    async with asyncio.timeout(5):
        result, stats = await batch.analyze(["retry-a", "retry-b"],
                                           service=SimpleNamespace(analyze_with_snapshot=succeeds))
    assert result == ["retry-a", "retry-b"]
    assert stats.observed_concurrency == 2


@pytest.mark.asyncio
async def test_parent_cancellation_leaves_no_running_analysis():
    ready, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = 0
    cleanup_count = 0

    async def analyze(item):
        nonlocal active, cleanup_count
        active += 1
        if active == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_count += 1
            if cleanup_count == 2:
                cleaning.set()
            await release_cleanup.wait()
            active -= 1

    batch = HotNewsAnalysisBatch(max_concurrency=2)
    task = asyncio.create_task(batch.analyze([1, 2, 3], service=SimpleNamespace(analyze_with_snapshot=analyze)))
    async with asyncio.timeout(5):
        await ready.wait()
        task.cancel()
        await cleaning.wait()
        assert not task.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0


@pytest.mark.asyncio
async def test_one_keeps_serial_execution_and_empty_input_calls_no_model():
    active = peak = 0

    async def analyze(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return item

    batch = HotNewsAnalysisBatch(max_concurrency=1)
    service = SimpleNamespace(analyze_with_snapshot=analyze)
    result, stats = await batch.analyze([1, 2, 3], service=service)
    assert result == [1, 2, 3]
    assert peak == stats.observed_concurrency == 1
    result, stats = await batch.analyze([], service=service)
    assert result == []
    assert stats.observed_concurrency == stats.task_count == 0


@pytest.mark.parametrize("limit", [0, 17, True, 1.5])
def test_invalid_concurrency_fails_before_any_model_call(limit):
    with pytest.raises(ValueError, match="max_concurrency"):
        HotNewsAnalysisBatch(max_concurrency=limit)
