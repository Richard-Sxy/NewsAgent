"""Model diagnostics classify quality failures and never export model-visible text."""

from __future__ import annotations

import json

import httpx
import pytest
from opentelemetry import baggage, context
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict

from app.model_runtime.core import (
    EmbeddingRequest, InferenceMessage, InferenceRequest, PromptRegistry, PromptSpec,
    RawInferenceResult, StructuredInferenceService, StructuredOutputError,
    PromptNotRegisteredError, model_attempt_context,
)
from app.model_runtime.config_file import EndpointConfig
from app.model_runtime.http import (
    ModelTransportError, OpenAICompatibleEmbeddingClient, OpenAICompatibleInferenceClient,
)
from app.observability import telemetry
from app.observability.settings import ObservabilitySettings


class DiagnosticReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    news_id: str
    summary: str


@pytest.fixture
def observed(monkeypatch):
    # Do not mutate the process-global OTel providers or use an external collector.
    monkeypatch.setattr(telemetry, "_runtime", None)
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    logs = InMemoryLogRecordExporter()
    runtime = telemetry.configure_telemetry(
        settings=ObservabilitySettings(enabled=True, json_logs=False),
        span_exporter=spans, metric_reader=metrics, log_exporter=logs,
    )
    try:
        yield spans, metrics, logs
    finally:
        runtime.shutdown()


def points(reader, name):
    data = reader.get_metrics_data()
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics if metric.name == name
        for point in metric.data.data_points
    ]


def service_for(content, *, usage=None):
    class Port:
        async def complete(self, request):
            return RawInferenceResult(
                content=content, request_id="provider-req-1", model_version="model-v1",
                usage=usage or {},
            )

    return StructuredInferenceService(
        inference=Port(),
        prompts=PromptRegistry([PromptSpec("diagnostic", "prompt-v1", "PROMPT_PRIVATE")]),
    )


async def run(service):
    return await service.run(
        tenant_id="tenant-private", trace_id="business-trace-private", idempotency_key="request-private",
        scene="diagnostic", prompt_version="prompt-v1", model_route="model-v1",
        payload={"article": "ARTICLE_PRIVATE"}, output_type=DiagnosticReport,
        model_profile_id="profile-v1", output_schema_version="schema-v1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("content", "stage", "kind", "failed_span"), [
    ("OUTPUT_PRIVATE", "response_decode", "invalid_json", "model.response_decode"),
    (json.dumps({"news_id": "n1", "summary": "OUTPUT_PRIVATE", "extra": "OUTPUT_PRIVATE"}),
     "schema_validation", "schema_validation", "model.schema_validate"),
])
async def test_http_success_does_not_hide_decode_or_schema_failure(observed, content, stage, kind, failed_span):
    exporter, reader, logs = observed
    with model_attempt_context(2):
        with pytest.raises(StructuredOutputError):
            await run(service_for(content, usage={"total_tokens": 17}))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["model.structured"]
    assert root.status.status_code is StatusCode.ERROR
    assert spans[failed_span].status.status_code is StatusCode.ERROR
    assert root.attributes["error.stage"] == stage
    assert root.attributes["error.kind"] == kind
    assert root.attributes["model.attempt"] == 2
    assert root.attributes["model.profile_id"] == "profile-v1"
    assert root.attributes["model.schema_version"] == "schema-v1"
    assert len(root.attributes["model.schema_hash"]) == 64
    assert root.attributes["model.provider_request_id"] == "provider-req-1"
    calls = points(reader, "newsagent.model.calls")
    assert len(calls) == 1 and calls[0].value == 1
    assert calls[0].attributes["newsagent.outcome"] == "error"
    assert calls[0].attributes["newsagent.stage"] == stage
    tokens = points(reader, "newsagent.model.tokens")
    assert len(tokens) == 1 and tokens[0].value == 17
    assert tokens[0].attributes["gen_ai.token.type"] == "total"
    exported = str([(span.attributes, span.events, span.status.description) for span in spans.values()])
    exported += str(logs.get_finished_logs())
    for private in ("PROMPT_PRIVATE", "ARTICLE_PRIVATE", "OUTPUT_PRIVATE", "tenant-private", "request-private"):
        assert private not in exported
    for point in calls + tokens:
        assert not any("version" in key or "request_id" in key or key == "model.attempt" for key in point.attributes)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [{}, {"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "21"}])
async def test_unknown_or_invalid_token_usage_stays_unknown(observed, usage):
    exporter, reader, _ = observed
    await run(service_for('{"news_id":"n1","summary":"ok"}', usage=usage))
    root = next(span for span in exporter.get_finished_spans() if span.name == "model.structured")
    assert not any(key.startswith("gen_ai.usage.") for key in root.attributes)
    assert points(reader, "newsagent.model.tokens") == []


@pytest.mark.asyncio
async def test_prompt_configuration_failure_is_located_before_inference(observed):
    exporter, reader, _ = observed
    service = StructuredInferenceService(inference=object(), prompts=PromptRegistry([]))
    with pytest.raises(PromptNotRegisteredError):
        await run(service)
    root = next(span for span in exporter.get_finished_spans() if span.name == "model.structured")
    assert root.attributes["error.stage"] == "prompt_render"
    assert root.attributes["error.kind"] == "prompt_not_registered"
    assert points(reader, "newsagent.model.calls")[0].attributes["newsagent.stage"] == "prompt_render"


def endpoint(path="chat/completions", *, model="model-v1", dimensions=None):
    return EndpointConfig(
        provider="openai_compatible", url=f"https://provider.invalid/{path}",
        api_key_env="UNUSED_TEST_MODEL_KEY", model_routes=(model,),
        expected_embedding_dimensions=dimensions,
    )


def request():
    return InferenceRequest(
        tenant_id="tenant-private", trace_id="business-trace-private", idempotency_key="request-private",
        scene="diagnostic", prompt_version="prompt-v1", model_route="model-v1",
        messages=(InferenceMessage("system", "PROMPT_PRIVATE"), InferenceMessage("user", "ARTICLE_PRIVATE")),
        output_json_schema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_real_http_200_can_still_be_a_model_schema_failure(observed):
    exporter, reader, _ = observed
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json={
        "model": "model-v1", "choices": [{"message": {"content": '{"summary":"OUTPUT_PRIVATE"}'}}],
        "usage": {"total_tokens": 9},
    }))) as client:
        adapter = OpenAICompatibleInferenceClient(endpoint(), api_key="API_KEY_PRIVATE", client=client)
        service = StructuredInferenceService(inference=adapter, prompts=PromptRegistry([
            PromptSpec("diagnostic", "prompt-v1", "PROMPT_PRIVATE"),
        ]))
        with pytest.raises(StructuredOutputError):
            await run(service)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["model.http.request"].attributes["http.response.status_code"] == 200
    assert spans["model.http.request"].attributes["newsagent.outcome"] == "success"
    assert spans["model.structured"].status.status_code is StatusCode.ERROR
    assert spans["model.schema_validate"].attributes["error.stage"] == "schema_validation"
    assert points(reader, "newsagent.model.calls")[0].attributes["newsagent.outcome"] == "error"


@pytest.mark.asyncio
async def test_http_injects_child_w3c_context_without_baggage(observed):
    exporter, _, _ = observed
    sent = {}

    def handler(outgoing):
        sent.update(outgoing.headers)
        return httpx.Response(200, headers={"x-request-id": "provider-http-1"}, json={
            "model": "model-v1", "choices": [{"message": {"content": "{}"}}],
        })

    token = context.attach(baggage.set_baggage("private-user", "IDENTITY_PRIVATE"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAICompatibleInferenceClient(endpoint(), api_key="API_KEY_PRIVATE", client=client)
            with telemetry.operation("test.parent") as parent:
                await adapter.complete(request())
    finally:
        context.detach(token)

    spans = {span.name: span for span in exporter.get_finished_spans()}
    http = spans["model.http.request"]
    provider = spans["model.provider.inference"]
    assert http.parent.span_id == provider.context.span_id
    assert provider.parent.span_id == parent.get_span_context().span_id
    assert sent["traceparent"] == f"00-{http.context.trace_id:032x}-{http.context.span_id:016x}-{int(http.context.trace_flags):02x}"
    assert "baggage" not in sent
    assert http.attributes["model.provider_request_id"] == "provider-http-1"
    assert "API_KEY_PRIVATE" not in str([(span.attributes, span.events) for span in spans.values()])


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "stage", "kind", "retryable"), [
    ("timeout", "inference", "timeout", True),
    ("connection", "inference", "connection", True),
    (401, "inference", "http_error", False),
    (429, "inference", "rate_limit", True),
    (503, "inference", "http_error", True),
    ("invalid_json", "response_decode", "invalid_json", False),
    ("missing_content", "response_decode", "response_validation", False),
    ("model_drift", "model_binding", "model_version_mismatch", False),
])
async def test_provider_failures_have_safe_precise_stage(observed, mode, stage, kind, retryable):
    exporter, _, logs = observed

    def handler(outgoing):
        if mode == "timeout":
            raise httpx.ReadTimeout("EXCEPTION_PRIVATE", request=outgoing)
        if mode == "connection":
            raise httpx.ConnectError("EXCEPTION_PRIVATE", request=outgoing)
        if type(mode) is int:
            return httpx.Response(mode, text="OUTPUT_PRIVATE", headers={"x-request-id": "failed-request-1"})
        if mode == "invalid_json":
            return httpx.Response(200, content=b"OUTPUT_PRIVATE")
        if mode == "missing_content":
            return httpx.Response(200, json={"model": "model-v1"})
        return httpx.Response(200, json={"model": "model-drift", "choices": [{"message": {"content": "OUTPUT_PRIVATE"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleInferenceClient(endpoint(), api_key="API_KEY_PRIVATE", client=client)
        with pytest.raises(ModelTransportError) as caught:
            await adapter.complete(request())

    assert caught.value.retryable is retryable
    assert caught.value.failure_kind == kind
    spans = exporter.get_finished_spans()
    provider = next(span for span in spans if span.name == "model.provider.inference")
    assert provider.status.status_code is StatusCode.ERROR
    assert provider.attributes["error.stage"] == stage
    assert provider.attributes["error.kind"] == kind
    safe = str([(span.attributes, span.events, span.status.description) for span in spans]) + str(logs.get_finished_logs())
    assert "EXCEPTION_PRIVATE" not in safe and "OUTPUT_PRIVATE" not in safe and "API_KEY_PRIVATE" not in safe


@pytest.mark.asyncio
async def test_embedding_dimension_failure_is_model_error_with_usage(observed):
    exporter, reader, _ = observed
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json={
        "model": "embedding-v1", "data": [{"index": 0, "embedding": [1.0, 0.0]}],
        "usage": {"prompt_tokens": 8, "total_tokens": 8},
    }))) as client:
        adapter = OpenAICompatibleEmbeddingClient(
            endpoint("embeddings", model="embedding-v1", dimensions=3), api_key="API_KEY_PRIVATE", client=client,
        )
        with pytest.raises(ModelTransportError):
            await adapter.embed(EmbeddingRequest("tenant-private", "trace-private", "embedding-v1", ("ARTICLE_PRIVATE",)))
    span = next(span for span in exporter.get_finished_spans() if span.name == "model.embedding")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.kind"] == "embedding_dimension"
    calls = points(reader, "newsagent.model.calls")
    assert calls[0].attributes["gen_ai.operation.name"] == "embeddings"
    assert calls[0].attributes["newsagent.outcome"] == "error"
    assert {point.attributes["gen_ai.token.type"] for point in points(reader, "newsagent.model.tokens")} == {"input", "total"}
