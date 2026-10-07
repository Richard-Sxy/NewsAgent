from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError
from temporalio.exceptions import ApplicationError

from app.activities.data_loop import DataLoopActivities
from app.workflows.data_loop_contracts import DataLoopStepCommand


@pytest.mark.asyncio  # 这个注解表示 在pytest配置的模式下 默认是strict的 必须要配置这个注解
async def test_transient_sql_failure_remains_retryable_for_temporal() -> None:
    handler = AsyncMock()
    handler.execute.side_effect = OperationalError(
        "SELECT 1",
        {},
        ConnectionError("database temporarily unavailable"),
    )

    with pytest.raises(ApplicationError) as captured:
        await DataLoopActivities(handler).run_data_loop_step(
            DataLoopStepCommand(
                tenant_id="tenant-1",
                run_idempotency_key="run-1",
                step_type="evaluate_candidate",
                step_key="evaluate-v1",
            )
        )

    assert captured.value.non_retryable is False

