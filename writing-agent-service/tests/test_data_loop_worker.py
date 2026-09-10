from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.data_loop_worker import run_data_loop_worker
from app.workflows.data_loop import (
    HotNewsDataLoopActivationRecoveryWorkflow,
    HotNewsDataLoopWorkflow,
)


@pytest.mark.asyncio
async def test_worker_registers_data_loop_on_dedicated_queue() -> None:
    temporal_client = object()
    worker = SimpleNamespace(run=AsyncMock())
    settings = SimpleNamespace(
        temporal_address="temporal:7233",
        temporal_namespace="default",
        temporal_data_loop_task_queue="hot-news-data-loop",
    )
    handler = AsyncMock()

    with (
        patch(
            "app.data_loop_worker.Client.connect",
            AsyncMock(return_value=temporal_client),
        ),
        patch("app.data_loop_worker.Worker", return_value=worker) as factory,
    ):
        await run_data_loop_worker(handler, settings=settings)

    kwargs = factory.call_args.kwargs
    assert kwargs["task_queue"] == "hot-news-data-loop"
    assert kwargs["workflows"] == [
        HotNewsDataLoopWorkflow,
        HotNewsDataLoopActivationRecoveryWorkflow,
    ]
    assert len(kwargs["activities"]) == 1
    worker.run.assert_awaited_once()
