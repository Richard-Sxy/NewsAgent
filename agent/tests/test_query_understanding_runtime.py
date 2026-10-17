"""Scene isolation and semantic gates for the independent query model client.

All endpoints, credentials, clocks and responses below are test fixtures. These
tests never load the private compatible YAML, dotenv files or a live model.
"""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from app.modules.conversation.query_understanding import (
    QueryPolicy, QueryResolutionError, QueryUnderstanding,
    resolve_query_understanding,
)
from app.domain.errors import AgentOutputValidationError
from app.model_runtime.agent_client import model_request_context
from app.model_runtime.config_file import (
    AgentSceneConfig, EndpointConfig, ModelRuntimeConfig, PromptConfig,
)
from app.model_runtime.core import RawInferenceResult
from app.model_runtime.http import OpenAICompatibleInferenceClient
from app.model_runtime.query_understanding import (
    build_query_understanding_client, load_query_understanding_endpoint_config,
    query_understanding_config,
)
from app.sql_assistant.scenarios import SqlScenario


KEY_VARIABLE = "QUERY_RUNTIME_TEST_API_KEY"
MODEL = "existing-model-v7"
NOW = datetime.fromisoformat("2026-10-10T11:23:00+08:00")
DATE_QUESTION = "查询10月9号上午八点到九点热度最高的前五条新闻，帮我总结一下。"
RELATIVE_QUESTION = "查询昨天早上八点到九点热度最高的前五条新闻，帮我总结一下。"


@pytest.fixture
def configs():
    """An approved local application and a separately configured HTTP endpoint."""
    local_route = "local-main-model-v1"
    approved = ModelRuntimeConfig(
        schema_version=1,
        inference=EndpointConfig(provider="local", model_routes=(local_route,)),
        embedding=EndpointConfig(provider="local", model_routes=("local-embedding-v1",),
                                 local_embedding_dimensions=16),
        prompts=(
            PromptConfig(scene="query_understanding", version="native-query-understanding-v2",
                         system_prompt="Historical query prompt."),
            PromptConfig(scene="query_understanding", version="native-query-understanding-v4",
                         system_prompt="Approved query prompt v4; preserve the original question."),
            PromptConfig(scene="conversation", version="local-conversation-v5",
                         system_prompt="Keep the main conversation local."),
        ),
        agent_scenes=(
            AgentSceneConfig(scene="query_understanding", prompt_version="native-query-understanding-v4",
                             model_route=local_route),
            AgentSceneConfig(scene="conversation", prompt_version="local-conversation-v5",
                             model_route=local_route),
        ),
    )
    existing = ModelRuntimeConfig(
        schema_version=1,
        inference=EndpointConfig(
            provider="openai_compatible", model_routes=(MODEL,),
            url="https://configured-model.example/v1/chat/completions",
            api_key_env=KEY_VARIABLE, timeout_seconds=7, response_format="json_schema",
            extra_body={"test_option": "retain-existing-endpoint-option"},
        ),
        # Deliberately different: adopting this embedding would require another
        # credential and would change the local application's data path.
        embedding=EndpointConfig(
            provider="openai_compatible", model_routes=("unrelated-embedding-v2",),
            url="https://unrelated-embedding.example/v1/embeddings",
            api_key_env="UNRELATED_RUNTIME_TEST_KEY",
        ),
        prompts=(
            PromptConfig(scene="query_understanding", version="existing-query-v3",
                         system_prompt="Old endpoint query prompt; do not inherit this."),
            PromptConfig(scene="conversation", version="existing-conversation-v1",
                         system_prompt="Unrelated endpoint scene."),
        ),
        agent_scenes=(
            AgentSceneConfig(scene="query_understanding", prompt_version="existing-query-v3",
                             model_route=MODEL),
            AgentSceneConfig(scene="conversation", prompt_version="existing-conversation-v1",
                             model_route=MODEL),
        ),
    )
    return approved, existing


@pytest.fixture
def policy():
    start = datetime.fromisoformat("2026-10-09T00:00:00+08:00")
    return QueryPolicy(
        scenario=SqlScenario(
            id="news-ranking", name="新闻排行", description="批准的单小时查询",
            allowed_sort_metrics=("clicks", "hot_score"),
            sample_questions=("点击量前5条新闻",), max_limit=100,
        ),
        default_limit=5, max_limit=100,
        supported_window_start=start, supported_window_end=start + timedelta(hours=1),
        coverage_window_start=datetime.fromisoformat("2026-10-07T00:00:00+08:00"),
        coverage_window_end=datetime.fromisoformat("2026-10-10T00:00:00+08:00"),
        data_watermark=datetime.fromisoformat("2026-10-10T00:00:00+08:00"),
    )


def proposal(*, relative=False):
    return {
        "status": "ready",
        "intent": {"sort_by": "hot_score", "sort_direction": "desc", "row_limit": 5,
                   "content_type": None, "category": None, "explanation": "批准的候选热度排序"},
        "time_expression": "yesterday" if relative else "date",
        "explicit_date": None if relative else "2026-10-09",
        "hour_start": 8, "hour_end": 9,
        "time_basis": "behavior", "ranking_source": "unspecified",
        "response_style": "summary", "unsupported_conditions": [],
    }


def payload(question, policy):
    return {"question": question, "policy": policy.model_dump(mode="json"),
            "temporal_context": {"server_now": NOW.isoformat(), "timezone": policy.timezone}}


class RecordingInference:
    def __init__(self):
        self.requests = []
        self.close_calls = 0

    async def complete(self, request):
        self.requests.append(request)
        return RawInferenceResult(content=json.dumps(proposal(), ensure_ascii=False),
                                  request_id="recorded-query", model_version=MODEL)

    async def close(self):
        self.close_calls += 1


class RecordingEmbedding:
    def __init__(self):
        self.close_calls = 0

    async def embed(self, request):
        raise AssertionError("query understanding must not invoke embedding")

    async def close(self):
        self.close_calls += 1


def test_query_config_selects_approved_prompt_and_existing_endpoint_without_mutating_globals(configs):
    approved, existing = configs
    original_main, original_endpoint = approved.model_dump(), existing.model_dump()
    config = query_understanding_config(prompts_config=approved, endpoint_config=existing)

    assert config.inference == existing.inference
    assert config.embedding == approved.embedding
    assert config.embedding != existing.embedding
    assert config.agent_scene("query_understanding").model_route == MODEL
    assert config.agent_scene("query_understanding").prompt_version == "native-query-understanding-v4"
    assert [(item.scene, item.version) for item in config.prompts] == [
        ("query_understanding", "native-query-understanding-v4")
    ]
    assert config.prompts[0].system_prompt == approved.prompts[1].system_prompt
    assert [item.scene for item in config.agent_scenes] == ["query_understanding"]
    with pytest.raises(ValueError, match="not configured"):
        config.agent_scene("conversation")
    assert approved.model_dump() == original_main
    assert existing.model_dump() == original_endpoint


@pytest.mark.asyncio
async def test_built_client_cannot_call_other_scenes_and_closes_both_owned_ports(configs, monkeypatch, policy):
    approved, existing = configs
    inference, embedding = RecordingInference(), RecordingEmbedding()
    monkeypatch.setenv(KEY_VARIABLE, "test-key-only")
    inference_factory = Mock(return_value=inference)
    embedding_factory = Mock(return_value=embedding)
    monkeypatch.setattr("app.model_runtime.factory.OpenAICompatibleInferenceClient", inference_factory)
    monkeypatch.setattr("app.model_runtime.factory.LocalHashEmbedding", embedding_factory)
    client, ports, metadata = build_query_understanding_client(
        prompts_config=approved, endpoint_config=existing, environment="e2e",
    )
    try:
        assert metadata == {"provider": "openai_compatible", "model_route": MODEL,
                            "prompt_version": "native-query-understanding-v4"}
        inference_factory.assert_called_once_with(existing.inference, api_key="test-key-only")
        embedding_factory.assert_called_once_with(model_routes=approved.embedding.model_routes, dimensions=16)
        with model_request_context(tenant_id="synthetic-tenant", trace_id="query-runtime-test"):
            with pytest.raises(ValueError, match="not configured"):
                await client.run_structured(app_id="python:test", mode="conversation",
                                            payload={}, output_type=QueryUnderstanding)
            assert inference.requests == []
            result = await client.run_structured(app_id="python:test", mode="query_understanding",
                                                payload=payload(DATE_QUESTION, policy), output_type=QueryUnderstanding)
        assert result.request_id == "recorded-query"
        assert len(inference.requests) == 1
        request = inference.requests[0]
        assert request.scene == "query_understanding" and request.model_route == MODEL
        assert request.prompt_version == "native-query-understanding-v4"
        assert request.timeout_seconds == existing.inference.timeout_seconds
        assert request.output_json_schema == QueryUnderstanding.model_json_schema()
        assert json.loads(request.messages[1].content) == payload(DATE_QUESTION, policy)
    finally:
        await ports.close()
    assert inference.close_calls == embedding.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("question,relative", [(DATE_QUESTION, False), (RELATIVE_QUESTION, True)])
async def test_mock_http_preserves_natural_time_original_payload_and_complete_schema(
    configs, monkeypatch, policy, question, relative,
):
    approved, existing = configs
    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == str(existing.inference.url)
        assert request.headers["authorization"] == "Bearer test-key-only"
        assert body["model"] == MODEL and body["stream"] is False
        assert body["test_option"] == "retain-existing-endpoint-option"
        assert body["response_format"]["json_schema"]["schema"] == QueryUnderstanding.model_json_schema()
        assert json.loads(body["messages"][1]["content"]) == payload(question, policy)
        assert "Approved query prompt v4" in body["messages"][0]["content"]
        return httpx.Response(200, json={"model": MODEL, "choices": [{"message": {
            "content": json.dumps(proposal(relative=relative), ensure_ascii=False),
        }}]}, headers={"x-request-id": "mock-natural-time"})

    monkeypatch.setenv(KEY_VARIABLE, "test-key-only")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http_client:
        monkeypatch.setattr("app.model_runtime.factory.OpenAICompatibleInferenceClient",
                            lambda config, *, api_key: OpenAICompatibleInferenceClient(
                                config, api_key=api_key, client=http_client))
        client, ports, _ = build_query_understanding_client(
            prompts_config=approved, endpoint_config=existing, environment="e2e",
        )
        try:
            with model_request_context(tenant_id="synthetic-tenant", trace_id="natural-time-test"):
                result = await client.run_structured(app_id="python:test", mode="query_understanding",
                                                    payload=payload(question, policy), output_type=QueryUnderstanding)
            resolution = resolve_query_understanding(question, result.value, policy, now=NOW).require_ready()
            assert resolution.requested_window_start == datetime.fromisoformat("2026-10-09T08:00:00+08:00")
            assert resolution.requested_window_end == datetime.fromisoformat("2026-10-09T09:00:00+08:00")
            assert resolution.intent.row_limit == 5 and resolution.intent.sort_by == "hot_score"
            assert resolution.response_style == "summary"
            assert len(requests) == 1 and result.request_id == "mock-natural-time"
        finally:
            await ports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,reason", [
    ({"explicit_date": "2026-10-08"}, "time_semantic_drift"),
    ({"intent": {**proposal()["intent"], "row_limit": 6}}, "intent_semantic_drift"),
    ({"sql": "SELECT * FROM unapproved_table"}, "invalid_schema"),
])
async def test_model_date_count_and_sql_mutations_are_rejected_before_query_approval(
    configs, monkeypatch, policy, mutation, reason,
):
    approved, existing = configs
    inference = RecordingInference()
    bad = deepcopy(proposal())
    bad.update(mutation)

    async def complete(request):
        inference.requests.append(request)
        return RawInferenceResult(content=json.dumps(bad, ensure_ascii=False),
                                  request_id="mutated-query", model_version=MODEL)

    inference.complete = complete
    monkeypatch.setenv(KEY_VARIABLE, "test-key-only")
    monkeypatch.setattr("app.model_runtime.factory.OpenAICompatibleInferenceClient", lambda *_, **__: inference)
    client, ports, _ = build_query_understanding_client(
        prompts_config=approved, endpoint_config=existing, environment="e2e",
    )
    try:
        with model_request_context(tenant_id="synthetic-tenant", trace_id="mutation-test"):
            if reason == "invalid_schema":
                with pytest.raises(AgentOutputValidationError) as caught:
                    await client.run_structured(app_id="python:test", mode="query_understanding",
                                                payload=payload(DATE_QUESTION, policy), output_type=QueryUnderstanding)
                assert caught.value.request_id == "mutated-query"
            else:
                result = await client.run_structured(app_id="python:test", mode="query_understanding",
                                                    payload=payload(DATE_QUESTION, policy), output_type=QueryUnderstanding)
                resolution = resolve_query_understanding(DATE_QUESTION, result.value, policy, now=NOW)
                assert resolution.status == "unsupported" and resolution.reason_code == reason
                assert resolution.canonical_question == "" and resolution.intent is None
                with pytest.raises(QueryResolutionError):
                    resolution.require_ready()
        assert len(inference.requests) == 1
    finally:
        await ports.close()


def test_missing_credential_fails_before_constructing_ports_without_local_fallback(configs, monkeypatch):
    approved, existing = configs
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    factories = [Mock(), Mock(), Mock()]
    for name, factory in zip(("OpenAICompatibleInferenceClient", "LocalHashEmbedding", "_RoutedLocalInference"), factories):
        monkeypatch.setattr(f"app.model_runtime.factory.{name}", factory)
    with pytest.raises(ValueError, match="credential environment variable is missing"):
        build_query_understanding_client(prompts_config=approved, endpoint_config=existing, environment="e2e")
    for factory in factories:
        factory.assert_not_called()


@pytest.fixture
def endpoint_document(configs):
    _, existing = configs
    document = existing.model_dump(mode="json")
    document["prompts"].append(deepcopy(document["prompts"][1]))
    # Neither unrelated Prompt registrations nor embedding settings belong to
    # the projected query client's configuration boundary.
    document["embedding"] = {"unrelated_private_setting": "must-not-be-inherited"}
    return document


def write_endpoint_yaml(tmp_path, document):
    path = tmp_path / "fixture-endpoint.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_endpoint_loader_projects_despite_duplicate_unrelated_prompts(configs, endpoint_document, tmp_path, monkeypatch):
    approved, existing = configs
    original_main = approved.model_dump()
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    path = write_endpoint_yaml(tmp_path, endpoint_document)
    config = load_query_understanding_endpoint_config(path, prompts_config=approved)

    assert config == query_understanding_config(prompts_config=approved, endpoint_config=existing)
    assert config.inference.api_key_env == KEY_VARIABLE
    assert config.embedding == approved.embedding
    assert config.agent_scene("query_understanding").prompt_version == "native-query-understanding-v4"
    assert [item.scene for item in config.agent_scenes] == ["query_understanding"]
    assert len(config.prompts) == 1 and config.prompts[0].system_prompt == approved.prompts[1].system_prompt
    assert approved.model_dump() == original_main
    assert "must-not-be-inherited" not in config.model_dump_json()


@pytest.mark.parametrize("case", [
    "schema_version", "boolean_version", "string_version", "missing_query",
    "duplicate_query", "invalid_scenes", "unapproved_route", "missing_key_variable",
    "invalid_url", "missing_inference", "invalid_provider", "local_provider",
    "missing_binding_version", "blank_binding_version", "binding_extra_field",
])
def test_endpoint_loader_rejects_invalid_projected_configuration(configs, endpoint_document, tmp_path, case):
    approved, _ = configs
    query = endpoint_document["agent_scenes"][0]
    if case in {"schema_version", "boolean_version", "string_version"}:
        endpoint_document["schema_version"] = {"schema_version": 2, "boolean_version": True,
                                               "string_version": "1"}[case]
    elif case == "missing_query":
        endpoint_document["agent_scenes"] = endpoint_document["agent_scenes"][1:]
    elif case == "duplicate_query":
        endpoint_document["agent_scenes"].append(deepcopy(query))
    elif case == "invalid_scenes":
        endpoint_document["agent_scenes"] = {"query_understanding": query}
    elif case == "unapproved_route":
        query["model_route"] = "unapproved-model"
    elif case == "missing_key_variable":
        endpoint_document["inference"].pop("api_key_env")
    elif case == "invalid_url":
        endpoint_document["inference"]["url"] = "invalid-private-endpoint-value"
    elif case == "missing_inference":
        endpoint_document.pop("inference")
    elif case == "invalid_provider":
        endpoint_document["inference"]["provider"] = "unknown-provider"
    elif case == "local_provider":
        endpoint_document["inference"] = {"provider": "local", "model_routes": [MODEL]}
    elif case == "missing_binding_version":
        query.pop("prompt_version")
    elif case == "blank_binding_version":
        query["prompt_version"] = " "
    elif case == "binding_extra_field":
        query["sql"] = "SELECT * FROM unapproved_table"
    path = write_endpoint_yaml(tmp_path, endpoint_document)
    with pytest.raises(ValueError) as caught:
        load_query_understanding_endpoint_config(path, prompts_config=approved)
    assert "invalid-private-endpoint-value" not in str(caught.value)
    assert "unapproved_table" not in str(caught.value)


@pytest.mark.parametrize("contents", ["inference: [unterminated", "!!python/object:untrusted.Constructor {}", "[]"])
def test_endpoint_loader_rejects_invalid_or_unsafe_yaml(configs, tmp_path, contents):
    approved, _ = configs
    path = tmp_path / "invalid-endpoint.yml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        load_query_understanding_endpoint_config(path, prompts_config=approved)


def test_endpoint_loader_bounds_file_read_and_rejects_missing_file(configs, tmp_path):
    approved, _ = configs
    path = tmp_path / "endpoint.yml"
    with pytest.raises(ValueError, match="cannot be read"):
        load_query_understanding_endpoint_config(path, prompts_config=approved)
    path.write_bytes(b"x" * (1_048_576 + 1))
    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        load_query_understanding_endpoint_config(path, prompts_config=approved)


def policy_with_complete_hours_until_clock(policy, clock):
    local_clock = clock.astimezone(ZoneInfo(policy.timezone))
    return QueryPolicy.model_validate({
        **policy.model_dump(),
        "coverage_window_end": local_clock.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
        "data_watermark": local_clock.replace(minute=0, second=0, microsecond=0),
    })


@pytest.mark.parametrize("question,expression,model_date,hours,clock", [
    ("今天早上八点到九点热度最高的前五条新闻，帮我总结一下", "today", "2026-10-10", (8, 9), NOW),
    ("昨天下午一点到两点热度最高的前五条新闻，帮我总结一下", "yesterday", "2026-10-09", (13, 14), NOW),
    ("前天晚上十一点到十二点热度最高的前五条新闻，帮我总结一下", "day_before_yesterday", "2026-10-08", (23, 24), NOW),
    ("昨天早上八点到九点热度最高的前五条新闻，帮我总结一下", "yesterday", "2026-10-10", (8, 9),
     datetime.fromisoformat("2026-10-10T20:30:00+00:00")),
    ("前天晚上十一点到十二点热度最高的前五条新闻，帮我总结一下", "day_before_yesterday", "2026-10-09", (23, 24),
     datetime.fromisoformat("2026-10-10T20:30:00+00:00")),
    ("今天早上三点到四点热度最高的前五条新闻，帮我总结一下", "today", "2026-10-11", (3, 4),
     datetime.fromisoformat("2026-10-10T20:30:00+00:00")),
])
def test_equivalent_model_calendar_date_preserves_original_relative_day(
    policy, question, expression, model_date, hours, clock,
):
    approved_policy = policy_with_complete_hours_until_clock(policy, clock)
    absolute = {**proposal(), "explicit_date": model_date, "hour_start": hours[0], "hour_end": hours[1]}
    relative = {**absolute, "time_expression": expression, "explicit_date": None}
    expected = resolve_query_understanding(question, relative, approved_policy, now=clock).require_ready()
    result = resolve_query_understanding(question, absolute, approved_policy, now=clock).require_ready()
    assert result.time_expression == expression and result.explicit_date is None
    assert result.requested_window_start == datetime.fromisoformat(f"{model_date}T{hours[0]:02}:00:00+08:00")
    assert result.requested_window_end == result.requested_window_start + timedelta(hours=1)
    assert result.requested_window_start == expected.requested_window_start
    assert result.requested_window_end == expected.requested_window_end
    assert result.canonical_question == expected.canonical_question
    assert result.intent.model_dump() == expected.intent.model_dump()
    assert absolute["time_expression"] == "date" and absolute["explicit_date"] == model_date


@pytest.mark.parametrize("mutation,reason", [
    ({"explicit_date": "2026-10-08"}, "time_semantic_drift"),
    ({"time_expression": "sample", "explicit_date": None}, "time_semantic_drift"),
    ({"time_expression": "today", "explicit_date": None}, "time_semantic_drift"),
    ({"hour_start": None, "hour_end": None}, "time_semantic_drift"),
    ({"hour_start": 9, "hour_end": 10}, "time_semantic_drift"),
    ({"hour_start": 8, "hour_end": 10}, "time_semantic_drift"),
    ({"intent": {**proposal()["intent"], "row_limit": 6}}, "intent_semantic_drift"),
])
def test_relative_date_normalization_does_not_repair_wrong_date_or_changed_requirements(policy, mutation, reason):
    question = "昨天早上八点到九点热度最高的前五条新闻，帮我总结一下"
    response = {**proposal(), **mutation}
    result = resolve_query_understanding(question, response, policy, now=NOW)
    assert result.status == "unsupported" and result.reason_code == reason
    assert result.intent is None and not result.canonical_question
    with pytest.raises(QueryResolutionError):
        result.require_ready()


def test_equivalent_today_calendar_date_outside_coverage_still_denies_query_approval(policy):
    question = "今天早上八点到九点热度最高的前五条新闻，帮我总结一下"
    response = {**proposal(), "explicit_date": "2026-10-10"}
    result = resolve_query_understanding(question, response, policy, now=NOW)
    assert result.time_expression == "today" and result.explicit_date is None
    assert result.status == "unsupported" and result.reason_code == "time_coverage_unavailable"
    assert result.requested_window_start == datetime.fromisoformat("2026-10-10T08:00:00+08:00")
    assert result.intent is None and not result.canonical_question
    with pytest.raises(QueryResolutionError):
        result.require_ready()


@pytest.mark.parametrize("question,clock,response", [
    ("今天早上八点到九点热度最高的前五条新闻，帮我总结一下", NOW, proposal()),
    (RELATIVE_QUESTION, datetime.fromisoformat("2026-10-10T20:30:00+00:00"), proposal()),
])
def test_model_cannot_substitute_covered_or_utc_date_for_original_relative_date(policy, question, clock, response):
    result = resolve_query_understanding(question, response, policy, now=clock)
    assert result.status == "unsupported" and result.reason_code == "time_semantic_drift"
    assert result.intent is None and not result.canonical_question
    with pytest.raises(QueryResolutionError):
        result.require_ready()


def test_model_calendar_date_cannot_collapse_multiple_original_relative_days(policy):
    question = "昨天和前天早上八点到九点热度最高的前五条新闻，帮我总结一下"
    result = resolve_query_understanding(question, proposal(), policy, now=NOW)
    assert result.status == "clarify" and result.reason_code == "ambiguous_requirements"
    assert result.intent is None and not result.canonical_question
    with pytest.raises(QueryResolutionError):
        result.require_ready()
