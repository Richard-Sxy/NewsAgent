"""YAML and raw enterprise model protocol contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.model_runtime.config_file import (
    EndpointConfig,
    load_model_runtime_config,
)
from app.model_runtime.core import (
    EmbeddingRequest,
    InferenceMessage,
    InferenceRequest,
)
from app.model_runtime.factory import build_model_runtime_ports
from app.model_runtime.http import (
    ModelTransportError,
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleInferenceClient,
)


LOCAL_CONFIG = Path(__file__).resolve().parents[1] / "deploy/model-runtime.local.yml"
ENTERPRISE_EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "deploy/model-runtime.enterprise.example.yml"
)


def endpoint(*, path: str, model: str, dimensions: int | None = None) -> EndpointConfig:
    return EndpointConfig(
        provider="openai_compatible",
        url=f"https://internal-model.example/v1/{path}",
        api_key_env="ENTERPRISE_MODEL_API_KEY",
        model_routes=(model,),
        expected_embedding_dimensions=dimensions,
    )


def inference_request(model: str = "model-v1") -> InferenceRequest:
    return InferenceRequest(
        tenant_id="tenant-1",
        trace_id="trace-1",
        idempotency_key="run-1",
        scene="hot_news_analysis",
        prompt_version="prompt-v1",
        model_route=model,
        messages=(
            InferenceMessage("system", "trusted prompt and Schema"),
            InferenceMessage("user", '{"news_id":"news-1"}'),
        ),
        output_json_schema={"type": "object"},
    )


def test_yaml_owns_prompt_and_routes_but_not_credentials() -> None:
    config = load_model_runtime_config(LOCAL_CONFIG)
    prompt = config.prompt_registry().resolve(
        scene="hot_news_analysis", version="native-e2e-prompt-v1"
    )
    assert "不可信" in prompt.system_prompt
    assert config.inference.model_routes == ("native-local-model-v1",)
    assert config.embedding.model_routes == ("native-local-embedding-v1",)
    assert config.inference.api_key_env is None


@pytest.mark.asyncio
async def test_local_yaml_ports_are_isolated_and_rejected_in_production() -> None:
    config = load_model_runtime_config(LOCAL_CONFIG)
    with pytest.raises(ValueError, match="only allowed in e2e"):
        build_model_runtime_ports(config, environment="production")
    ports = build_model_runtime_ports(config, environment="e2e")
    result = await ports.embedding.embed(
        EmbeddingRequest(
            tenant_id="tenant-1",
            trace_id="trace-1",
            model_route="native-local-embedding-v1",
            texts=("新闻一", "新闻二"),
        )
    )
    assert len(result.vectors) == 2
    assert len(result.vectors[0]) == 32
    assert result.vectors[0] != result.vectors[1]
    await ports.close()


@pytest.mark.asyncio
async def test_enterprise_yaml_builds_python_http_ports_from_injected_secret_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_model_runtime_config(ENTERPRISE_EXAMPLE)
    monkeypatch.delenv("ENTERPRISE_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("ENTERPRISE_EMBEDDING_API_KEY", raising=False)
    with pytest.raises(ValueError, match="credential environment variable is missing"):
        build_model_runtime_ports(config, environment="production")
    monkeypatch.setenv("ENTERPRISE_MODEL_API_KEY", "inference-test-secret")
    monkeypatch.setenv("ENTERPRISE_EMBEDDING_API_KEY", "embedding-test-secret")
    ports = build_model_runtime_ports(config, environment="production")
    try:
        assert isinstance(ports.inference, OpenAICompatibleInferenceClient)
        assert isinstance(ports.embedding, OpenAICompatibleEmbeddingClient)
        assert config.inference.api_key_env == "ENTERPRISE_MODEL_API_KEY"
    finally:
        await ports.close()


@pytest.mark.asyncio
async def test_python_inference_adapter_calls_raw_enterprise_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.headers["x-tenant-id"] == "tenant-1"
        assert request.headers["x-trace-id"] == "trace-1"
        assert request.headers["idempotency-key"] == "run-1"
        payload = json.loads(request.content)
        assert payload["model"] == "model-v1"
        assert "response_format" not in payload
        assert payload["messages"][0]["role"] == "system"
        return httpx.Response(
            200,
            headers={"x-request-id": "enterprise-req-1"},
            json={
                "id": "completion-1",
                "model": "model-v1",
                "choices": [{"message": {"content": '{"news_id":"news-1"}'}}],
                "usage": {"total_tokens": 25},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleInferenceClient(
            endpoint(path="chat/completions", model="model-v1"),
            api_key="test-secret",
            client=client,
        )
        result = await adapter.complete(inference_request())
    assert result.content == '{"news_id":"news-1"}'
    assert result.request_id == "enterprise-req-1"
    assert result.usage["total_tokens"] == 25


@pytest.mark.asyncio
async def test_inference_model_version_drift_fails_closed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "model": "different-model",
                    "choices": [{"message": {"content": "{}"}}],
                },
            )
        )
    ) as client:
        adapter = OpenAICompatibleInferenceClient(
            endpoint(path="chat/completions", model="model-v1"),
            api_key="test-secret",
            client=client,
        )
        with pytest.raises(ModelTransportError, match="model version"):
            await adapter.complete(inference_request())


@pytest.mark.asyncio
async def test_python_embedding_adapter_preserves_index_and_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content)["input"] == ["甲", "乙"]
        return httpx.Response(
            200,
            json={
                "model": "embedding-v1",
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleEmbeddingClient(
            endpoint(path="embeddings", model="embedding-v1", dimensions=2),
            api_key="test-secret",
            client=client,
        )
        result = await adapter.embed(
            EmbeddingRequest("tenant-1", "trace-1", "embedding-v1", ("甲", "乙"))
        )
    assert result.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert result.model_version == "embedding-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(401, False), (429, True), (503, True)],
)
async def test_http_status_failures_are_classified(status: int, retryable: bool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        adapter = OpenAICompatibleInferenceClient(
            endpoint(path="chat/completions", model="model-v1"),
            api_key="test-secret",
            client=client,
        )
        with pytest.raises(ModelTransportError) as error:
            await adapter.complete(inference_request())
    assert error.value.retryable is retryable


@pytest.mark.asyncio
async def test_wrong_embedding_version_fails_closed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "model": "changed-without-approval",
                    "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                },
            )
        )
    ) as client:
        adapter = OpenAICompatibleEmbeddingClient(
            endpoint(path="embeddings", model="embedding-v1", dimensions=2),
            api_key="test-secret",
            client=client,
        )
        with pytest.raises(ModelTransportError, match="model version"):
            await adapter.embed(
                EmbeddingRequest("tenant-1", "trace-1", "embedding-v1", ("甲",))
            )
