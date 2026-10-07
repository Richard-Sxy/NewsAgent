"""Complete input measurement agrees with captured inference and HTTP requests."""

from __future__ import annotations

import json
from hashlib import sha256

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.model_runtime.agent_client import NativeStructuredAgentClient, model_request_context
from app.model_runtime.config_file import AgentSceneConfig, EndpointConfig, ModelRuntimeConfig, PromptConfig
from app.model_runtime.core import InferenceRequest, RawInferenceResult, StructuredInferenceService
from app.model_runtime.http import OpenAICompatibleInferenceClient


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


class LargeSchemaAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(description="Schema中需要完整计量的说明。" * 1000)


class InputData(BaseModel):
    message: str
    history: list[str]


class CapturingInference:
    def __init__(self):
        self.calls: list[InferenceRequest] = []

    async def complete(self, request: InferenceRequest) -> RawInferenceResult:
        self.calls.append(request)
        return RawInferenceResult(content='{"answer":"done"}', model_version=request.model_route)


def config(*, provider="local", response_format="none", system_prompt="可信提示", extra_body=None):
    endpoint = {"url": "https://model.invalid/chat/completions", "api_key_env": "TEST_MODEL_KEY"}
    return ModelRuntimeConfig(
        schema_version=1,
        inference=EndpointConfig(
            provider=provider, model_routes=("approved-model",), response_format=response_format,
            extra_body=extra_body or {}, **(endpoint if provider == "openai_compatible" else {}),
        ),
        embedding=EndpointConfig(provider="local", model_routes=("local-embedding",)),
        prompts=(PromptConfig(scene="conversation", version="prompt-v1", system_prompt=system_prompt),),
        agent_scenes=(AgentSceneConfig(scene="conversation", prompt_version="prompt-v1", model_route="approved-model"),),
    )


def client_for(inference, model_config):
    return NativeStructuredAgentClient(
        StructuredInferenceService(inference=inference, prompts=model_config.prompt_registry()), model_config
    )


def measured_chars(body):
    return len(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False))


def body_from_captured_request(request, endpoint):
    # Independent expectation from the model Port's actual request, not the
    # production payload builder or rendering function under test.
    body = {
        "model": request.model_route,
        "messages": [{"role": message.role, "content": message.content} for message in request.messages],
        "stream": False,
        "temperature": 0,
    }
    if endpoint.response_format == "json_schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": request.scene, "strict": True, "schema": request.output_json_schema,
        }}
    elif endpoint.response_format == "json_object":
        body["response_format"] = {"type": "json_object"}
    return {**endpoint.extra_body, **body}


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", ["none", "json_object", "json_schema"])
async def test_local_measurement_matches_captured_request_without_context_or_model_call(response_format):
    inference = CapturingInference()
    model_config = config(response_format=response_format, system_prompt="可信提示\n" * 2500,
                          extra_body={"provider_options": {"notes": "额外字段\n" * 1200}})
    client = client_for(inference, model_config)
    payload = InputData(message='中文\n"引用"\\路径\t😀', history=["此前对话"])

    size = client.input_chars(mode="conversation", payload=payload, output_type=LargeSchemaAnswer)
    assert isinstance(size, int) and inference.calls == []
    with model_request_context(tenant_id="tenant", trace_id="turn"):
        await client.run_structured(app_id="python:conversation", mode="conversation", payload=payload,
                                    output_type=LargeSchemaAnswer)

    request = inference.calls[0]
    body = body_from_captured_request(request, model_config.inference)
    assert size == measured_chars(body)
    assert size > measured_chars(payload.model_dump()) + 20000
    assert body["messages"][0]["content"].startswith(model_config.prompts[0].system_prompt)
    assert json.loads(body["messages"][1]["content"]) == payload.model_dump()
    if response_format == "json_schema":
        assert body["response_format"]["json_schema"]["schema"] == LargeSchemaAnswer.model_json_schema()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", ["none", "json_object", "json_schema"])
async def test_measurement_matches_actual_http_body_and_preserves_identity(response_format):
    captured = []
    def handler(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json={"model": "approved-model",
            "choices": [{"message": {"content": '{"answer":"done"}'}}]})

    extra = {"provider_options": {"text": '中文\n"\\' * 1200},
             "model": "unapproved-override", "temperature": 9, "messages": [{"role": "user", "content": "ignored"}]}
    model_config = config(provider="openai_compatible", response_format=response_format,
                          system_prompt="完整system预算\n" * 1200, extra_body=extra)
    payload = {"message": '引号"\n反斜线\\制表\t😀', "history": ["较早内容"]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        inference = OpenAICompatibleInferenceClient(model_config.inference, api_key="public-test-fixture", client=transport)
        client = client_for(inference, model_config)
        size = client.input_chars(mode="conversation", payload=payload, output_type=LargeSchemaAnswer)
        assert captured == []
        with model_request_context(tenant_id="tenant", trace_id="turn"):
            await client.run_structured(app_id="python:conversation", mode="conversation", payload=payload,
                                        output_type=LargeSchemaAnswer)

    sent = captured[0]
    body = json.loads(sent.content)
    assert size == measured_chars(body)
    assert body["provider_options"] == extra["provider_options"]
    assert body["model"] == "approved-model" and body["temperature"] == 0
    assert json.loads(body["messages"][1]["content"]) == payload
    assert size > len(sent.content.decode("utf-8"))  # Conservative spaces, not bytes or tokenizer counts.
    identity_input = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    expected_identity = sha256(f"tenant\x1fturn\x1fconversation\x1f{identity_input}".encode("utf-8")).hexdigest()
    assert sent.headers["idempotency-key"] == f"agent-{expected_identity}"
    assert sent.headers["x-tenant-id"] == "tenant" and sent.headers["x-trace-id"] == "turn"
    assert extra["messages"][0]["content"] == "ignored"


def test_large_system_schema_and_extra_fields_each_contribute_to_measurement():
    inference = CapturingInference()
    payload = {"message": "hello"}
    baseline = client_for(inference, config()).input_chars(mode="conversation", payload=payload, output_type=Answer)
    assert client_for(inference, config(system_prompt="可信" * 5000)).input_chars(
        mode="conversation", payload=payload, output_type=Answer) > baseline + 9000
    assert client_for(inference, config()).input_chars(
        mode="conversation", payload=payload, output_type=LargeSchemaAnswer) > baseline + 10000
    assert client_for(inference, config(extra_body={"provider_context": "资料" * 5000})).input_chars(
        mode="conversation", payload=payload, output_type=Answer) > baseline + 10000
    schema_plain = client_for(inference, config()).input_chars(
        mode="conversation", payload=payload, output_type=LargeSchemaAnswer)
    schema_twice = client_for(inference, config(response_format="json_schema")).input_chars(
        mode="conversation", payload=payload, output_type=LargeSchemaAnswer)
    assert schema_twice > schema_plain + 10000
    assert inference.calls == []


def test_unknown_scene_and_nonfinite_payload_fail_without_fallback_or_model_call():
    inference = CapturingInference()
    client = client_for(inference, config())
    with pytest.raises(ValueError, match="agent scene is not configured"):
        client.input_chars(mode="missing-scene", payload={}, output_type=Answer)
    with pytest.raises(ValueError, match="Out of range float"):
        client.input_chars(mode="conversation", payload={"invalid": float("nan")}, output_type=Answer)
    assert inference.calls == []
