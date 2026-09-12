"""Text2SQL 生成 Runner 的请求契约测试。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.fastgpt import AgentResult, FastGPTClient
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
async def test_runner_uses_existing_structured_fastgpt_client() -> None:
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
    with pytest.raises(ValueError, match="FASTGPT_TEXT2SQL_APP_ID"):
        Text2SqlAgentRunner(AsyncMock(), None)


@pytest.mark.asyncio
async def test_runner_round_trips_structured_plan_over_mock_transport() -> None:
    plan = Text2SqlPlan(
        sql=(
            "SELECT news_id FROM dw.news_behavior_aggregate "
            "WHERE tenant_id = :tenant_id LIMIT :row_limit"
        ),
        explanation="按租户取数",
        referenced_tables=["dw.news_behavior_aggregate"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["appId"] == "text2sql-app"
        assert body["variables"]["mode"] == "text2sql"
        assert "properties" in body["variables"]["output_schema"]
        model_input = json.loads(body["messages"][0]["content"])
        assert model_input["metric_columns"] == ("clicks", "impressions") or (
            model_input["metric_columns"] == ["clicks", "impressions"]
        )
        return httpx.Response(
            200,
            headers={"x-request-id": "req-sql"},
            json={
                "choices": [{"message": {"content": plan.model_dump_json()}}],
                "usage": {"total_tokens": 33},
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FastGPTClient(
        SimpleNamespace(
            fastgpt_base_url="https://fastgpt.example.com",
            fastgpt_api_key="test-secret",
        ),
        http_client=http_client,
    )
    runner = Text2SqlAgentRunner(client, "text2sql-app")

    try:
        result = await runner.run(generation_input())
    finally:
        await http_client.aclose()

    assert result.value == plan
    assert result.request_id == "req-sql"
    assert result.usage == {"total_tokens": 33}
