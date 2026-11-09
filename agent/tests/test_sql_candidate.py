"""Candidate adapter contracts; no live model, warehouse, dotenv or framework."""
import asyncio
import json

import pytest

from app.model_runtime.config_file import AgentSceneConfig, EndpointConfig, ModelRuntimeConfig, PromptConfig
from app.model_runtime.core import RawInferenceResult
from app.model_runtime.http import ModelTransportError
from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.candidate import (
    DbgptTemplateCandidateGenerator, SqlCandidateConfig, SqlCandidateError,
    SqlCandidateResponse, build_sql_candidate_generator, candidate_model_config,
    load_sql_candidate_config, parse_candidate_response,
)
from app.sql_assistant.scenarios import SqlScenario


@pytest.fixture
def runtime():
    return ModelRuntimeConfig(
        schema_version=1,
        inference=EndpointConfig(provider="local", model_routes=("test-route-v1",)),
        embedding=EndpointConfig(provider="local", model_routes=("unused-embedding",)),
        prompts=(PromptConfig(scene="text2sql_assistant", version="old-v1", system_prompt="Original."),),
        agent_scenes=(AgentSceneConfig(scene="text2sql_assistant", prompt_version="old-v1", model_route="test-route-v1"),),
    )


@pytest.fixture
def request_args():
    return dict(
        intent=SqlAssistantIntent(sort_by="ctr", row_limit=10, content_type="article", category="科技", explanation="confirmed"),
        scenario=SqlScenario(id="news-ranking", name="Ranking", description="Approved contract",
                             allowed_sort_metrics=("clicks", "ctr"), sample_questions=("点击率前10",)),
        schema_ddl="CREATE VIEW dw.news_behavior_aggregate AS approved schema metadata",
        schema_version="news-warehouse-v1", tenant_id="tenant-a", request_id="query-a",
    )


class RecordingInference:
    def __init__(self, content=None):
        self.requests = []
        self.content = content or json.dumps({"status": "proposed", "sql": "SELECT candidate", "reason_code": None})

    async def complete(self, request):
        self.requests.append(request)
        return RawInferenceResult(content=self.content, request_id="provider-request", model_version="actual-model-v7",
                                  usage={"prompt_tokens": 20, "completion_tokens": 15, "secret": "excluded"})


def generator(runtime, inference, **config):
    return DbgptTemplateCandidateGenerator(config=SqlCandidateConfig(mode="dbgpt_candidate", **config),
                                          model_config=runtime, inference=inference)


def test_native_requires_no_route_or_port():
    assert build_sql_candidate_generator(config=SqlCandidateConfig(), model_config=None, inference=None) is None
    assert load_sql_candidate_config().mode == "native"


def test_config_file_is_bounded_and_strict(tmp_path):
    file = tmp_path / "candidate.yml"
    file.write_text("mode: dbgpt_shadow\nmax_output_tokens: 1024\n", encoding="utf-8")
    assert load_sql_candidate_config(str(file)).max_output_tokens == 1024
    file.write_text("mode: dbgpt_shadow\nwarehouse_password: forbidden\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_sql_candidate_config(str(file))
    file.write_text("x" * 65537)
    with pytest.raises(ValueError):
        load_sql_candidate_config(str(file))
    for invalid in ({"deadline_seconds": float("nan")}, {"max_output_tokens": True},
                    {"max_model_attempts": 2}, {"max_repair_attempts": 1}, {"allow_native_fallback": True}):
        with pytest.raises(ValueError):
            SqlCandidateConfig(**invalid)


def test_runtime_view_keeps_original_config_and_records_source(runtime):
    before = runtime.model_dump()
    config = SqlCandidateConfig(mode="dbgpt_shadow", max_output_tokens=1024)
    selected = candidate_model_config(runtime, config)
    assert runtime.model_dump() == before
    assert selected.agent_scenes[0].scene == "sql_candidate_shadow"
    assert selected.inference.extra_body["max_tokens"] == 1024
    instance = DbgptTemplateCandidateGenerator(config=config, model_config=runtime, inference=RecordingInference())
    identity = instance.identity()
    assert identity["upstream_commit"] == "83609c471266607490b24e72188311efa23a0d16"
    assert identity["reuse_scope"] == "prompt_template_only"
    assert identity["model_route"] == "test-route-v1"
    for field in ("adapter_sha256", "validator_sha256", "prompt_sha256", "catalog_sha256", "examples_sha256", "response_schema_sha256"):
        assert len(identity[field]) == 64
    identity["config"]["mode"] = "native"
    assert instance.identity()["config"]["mode"] == "dbgpt_shadow"


@pytest.mark.asyncio
async def test_generates_once_with_identity_budget_and_no_bound_values(runtime, request_args):
    inference = RecordingInference()
    result = await generator(runtime, inference, max_output_tokens=1024).generate(**request_args)
    assert result.sql == "SELECT candidate"  # Guard/semantic comparison is the caller's responsibility.
    assert result.metadata["attempts"] == 1
    assert result.metadata["model_version"] == "actual-model-v7"
    assert result.metadata["usage"] == {"prompt_tokens": 20, "completion_tokens": 15}
    assert len(inference.requests) == 1
    sent = inference.requests[0]
    assert sent.max_output_tokens == 1024
    assert sent.tenant_id == "tenant-a"
    assert sent.model_route == "test-route-v1"
    assert all("tenant-a" not in message.content for message in sent.messages)
    assert all("query-a" not in message.content for message in sent.messages)
    payload = json.loads(sent.messages[1].content)
    assert "explanation" not in payload["confirmed_intent"]
    assert payload["order_by"] == ["ctr DESC", "news_id ASC"]
    assert payload["required_predicates"][-2:] == ["content_type = :content_type", "category = :category"]
    assert "warehouse" not in vars(generator(runtime, inference))


@pytest.mark.parametrize("content", [
    "SELECT 1", "```json\n{}\n```", "prefix {}", "[]",
    '{"status":"proposed","status":"unsupported","sql":null,"reason_code":"unsupported_contract"}',
    '{"status":"proposed","sql":" ","reason_code":null}',
    '{"status":"proposed","sql":"SELECT 1","reason_code":null,"metadata":{"approved":true}}',
    '{"status":"unsupported","sql":"SELECT 1","reason_code":"unsupported_contract"}',
    '{"status":"proposed","sql":123,"reason_code":null}',
    '{"status":"proposed","sql":"SELECT 1","reason_code":NaN}',
    '{"status":"unsupported","sql":null,"reason_code":null}',
])
def test_strict_response_rejects_free_text_duplicates_and_metadata(content):
    with pytest.raises(SqlCandidateError):
        parse_candidate_response(content, max_output_chars=16000)


@pytest.mark.asyncio
async def test_unsupported_is_explicit_and_never_falls_back(runtime, request_args):
    raw = json.dumps({"status": "unsupported", "sql": None, "reason_code": "unsupported_contract"})
    result = await generator(runtime, RecordingInference(raw)).generate(**request_args)
    assert result.status == "unsupported"
    assert result.sql is None


@pytest.mark.asyncio
async def test_failed_response_is_sanitized_counted_and_not_retried(runtime, request_args):
    inference = RecordingInference("PRIVATE SECRET invalid output")
    with pytest.raises(SqlCandidateError) as caught:
        await generator(runtime, inference).generate(**request_args)
    assert caught.value.code == "invalid_json"
    assert "PRIVATE" not in str(caught.value)
    assert "PRIVATE" not in json.dumps(caught.value.metadata)
    assert caught.value.metadata["attempts"] == 1
    assert caught.value.metadata["usage"]["completion_tokens"] == 15
    assert len(inference.requests) == 1


@pytest.mark.asyncio
async def test_input_limit_blocks_before_model_call(runtime, request_args):
    inference = RecordingInference()
    request_args["schema_ddl"] = "x" * 40001
    with pytest.raises(SqlCandidateError) as caught:
        await generator(runtime, inference).generate(**request_args)
    assert caught.value.code == "input_too_large"
    assert caught.value.metadata["attempts"] == 0
    assert inference.requests == []


@pytest.mark.asyncio
async def test_output_limit_rejects_without_extracting_sql(runtime, request_args):
    inference = RecordingInference("x" * 257)
    with pytest.raises(SqlCandidateError) as caught:
        await generator(runtime, inference, max_output_chars=256).generate(**request_args)
    assert caught.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_deadline_cancels_single_attempt(runtime, request_args):
    cancelled = asyncio.Event()
    class Slow:
        async def complete(self, request):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
    with pytest.raises(SqlCandidateError) as caught:
        await generator(runtime, Slow(), deadline_seconds=.01).generate(**request_args)
    assert caught.value.code == "deadline_exceeded"
    assert caught.value.metadata["attempts"] == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_external_cancellation_propagates(runtime, request_args):
    entered = asyncio.Event()
    class Slow:
        async def complete(self, request):
            entered.set()
            await asyncio.sleep(60)
    task = asyncio.create_task(generator(runtime, Slow()).generate(**request_args))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_rate_limit_is_classified_without_transport_retry(runtime, request_args):
    class RateLimited:
        calls = 0
        async def complete(self, request):
            self.calls += 1
            raise ModelTransportError("PRIVATE endpoint", retryable=True, failure_kind="rate_limit")
    inference = RateLimited()
    with pytest.raises(SqlCandidateError) as caught:
        await generator(runtime, inference).generate(**request_args)
    assert caught.value.code == "rate_limit"
    assert caught.value.retryable is False
    assert inference.calls == 1


@pytest.mark.asyncio
async def test_concurrent_tenants_have_distinct_context_and_response_metadata(runtime, request_args):
    class Concurrent:
        async def complete(self, request):
            await asyncio.sleep(.001)
            return RawInferenceResult(content=json.dumps({"status":"proposed", "sql":"SELECT candidate", "reason_code":None}),
                                      model_version=request.tenant_id, request_id=request.trace_id)
    shared = generator(runtime, Concurrent())
    other = {**request_args, "tenant_id":"tenant-b", "request_id":"query-b"}
    a, b = await asyncio.gather(shared.generate(**request_args), shared.generate(**other))
    assert a.metadata["model_version"] == "tenant-a"
    assert b.metadata["model_version"] == "tenant-b"
    assert a.metadata["model_request_id"] != b.metadata["model_request_id"]
