"""Text2SQL 生成 Runner 的请求契约测试。"""

from unittest.mock import AsyncMock

import pytest

from app.model_runtime.result import AgentResult
from app.schemas.text2sql import Text2SqlGenerationInput, Text2SqlPlan
from app.services.agents.text2sql import Text2SqlAgentRunner


def generation_input() -> Text2SqlGenerationInput:
    return Text2SqlGenerationInput(
        dialect="postgres",
        schema_ddl="CREATE TABLE dw.t (news_id bigint);",
        metric_columns=("clicks", "impressions"),
        content_types=("article",),
        ranking_limit=20,
        tenant_column="tenant_id",
        window_column="event_time",
        required_placeholders=("tenant_id", "window_start", "window_end"),
        row_limit_placeholder="row_limit",
    )


@pytest.mark.asyncio
async def test_runner_uses_structured_python_client() -> None:
    expected = AgentResult(
        Text2SqlPlan(sql="SELECT 1", explanation="", referenced_tables=[]),
        "req-1",
        {"total_tokens": 10},
        "{}",
    )
    client = AsyncMock()
    client.run_structured.return_value = expected
    runner = Text2SqlAgentRunner(client, "text2sql-app")

    result = await runner.run(generation_input())

    assert result is expected
    assert client.run_structured.await_args.kwargs == {
        "app_id": "text2sql-app",
        "payload": generation_input(),
        "output_type": Text2SqlPlan,
        "mode": "text2sql",
    }


def test_runner_requires_app_id() -> None:
    with pytest.raises(ValueError, match="execution identity"):
        Text2SqlAgentRunner(AsyncMock(), None)
