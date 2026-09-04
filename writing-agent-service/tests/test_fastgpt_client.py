import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.domain.errors import (
    AgentOutputValidationError,
    FastGPTAuthenticationError,
    FastGPTRateLimitError,
    FastGPTServerError,
)
from app.schemas.research import ResearchPackage
from app.services.agents.research import ResearchAgentRunner


def settings():
    return SimpleNamespace(
        fastgpt_base_url="https://fastgpt.example.com",
        fastgpt_api_key="secret",
    )


def client_with_handler(handler) -> FastGPTClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return FastGPTClient(settings(), http_client=http_client)


@pytest.mark.asyncio
async def test_structured_response_is_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["appId"] == "research-app"
        assert body["variables"]["mode"] == "research"
        content = {
            "job_id": "job-1",
            "topic": "新闻主题",
            "facts": [],
            "timeline": [],
            "conflicts": [],
            "evidence_gaps": [],
            "suggested_angles": [],
        }
        return httpx.Response(
            200,
            headers={"x-request-id": "req-1"},
            json={
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    client = client_with_handler(handler)
    result = await client.run_structured(
        app_id="research-app",
        payload={"job_id": "job-1", "topic": "新闻主题"},
        output_type=ResearchPackage,
        mode="research",
    )
    assert result.value.job_id == "job-1"
    assert result.request_id == "req-1"
    assert result.usage["completion_tokens"] == 20
    await client.http_client.aclose()


@pytest.mark.asyncio
async def test_json_code_fence_is_supported() -> None:
    content = """```json
    {"job_id":"job-1","topic":"主题","facts":[],"timeline":[],
     "conflicts":[],"evidence_gaps":[],"suggested_angles":[]}
    ```"""
    client = client_with_handler(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )
    )
    result = await client.run_structured(
        app_id="app",
        payload={},
        output_type=ResearchPackage,
        mode="research",
    )
    assert result.value.topic == "主题"
    await client.http_client.aclose()


@pytest.mark.asyncio
async def test_invalid_agent_output_is_not_accepted() -> None:
    client = client_with_handler(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "不是 JSON"}}]},
        )
    )
    with pytest.raises(AgentOutputValidationError) as error:
        await client.run_structured(
            app_id="app",
            payload={},
            output_type=ResearchPackage,
            mode="research",
        )
    assert error.value.retryable is False
    assert error.value.raw_content == "不是 JSON"
    await client.http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, FastGPTAuthenticationError),
        (500, FastGPTServerError),
    ],
)
async def test_http_errors_are_classified(status, error_type) -> None:
    client = client_with_handler(
        lambda request: httpx.Response(status, text="failed")
    )
    with pytest.raises(error_type):
        await client.run_app(app_id="app", messages=[])
    await client.http_client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_preserves_retry_after() -> None:
    client = client_with_handler(
        lambda request: httpx.Response(
            429,
            text="rate limited",
            headers={"retry-after": "3.5"},
        )
    )
    with pytest.raises(FastGPTRateLimitError) as error:
        await client.run_app(app_id="app", messages=[])
    assert error.value.retryable is True
    assert error.value.retry_after == 3.5
    await client.http_client.aclose()


@pytest.mark.asyncio
async def test_research_runner_selects_research_mode() -> None:
    package = ResearchPackage(job_id="job-1", topic="主题")
    client = SimpleNamespace()
    client.run_structured = AsyncMock(
        return_value=AgentResult(package, "req", {}, package.model_dump_json())
    )
    runner = ResearchAgentRunner(client, "research-app")
    result = await runner.run(
        job_id="job-1",
        topic="主题",
        requirements={"writing_type": "深度稿"},
    )
    assert result.value is package
    assert client.run_structured.await_args.kwargs["mode"] == "research"
