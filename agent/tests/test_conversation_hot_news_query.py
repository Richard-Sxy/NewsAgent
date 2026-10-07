"""New query tool: permission, same Agent loop, compatible wire contract and cancel."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.api.conversations import router
from app.conversation.service import ConversationAgentService
from app.conversation.tools import ConversationTools, ConversationToolDenied
from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import build_model_runtime_ports
from app.model_runtime.http import OpenAICompatibleInferenceClient
from examples.conversation_hot_news import LocalConversationHotNewsQuery
from tests.test_conversation_agent import Repository, Tools, TENANT, USER, RUN, runtime, headers


QUESTION = "查询点击率最高的前五条视频新闻并分析原因"


class QueryTools(Tools):
    def query_description(self, tenant):
        return {"name": "query_hot_news", "arguments": {"question": "text"}} if tenant == TENANT else None

    async def execute_hot_news_query(self, *, arguments, tenant_id, user_id, allowed, on_bound, **kwargs):
        assert allowed and tenant_id == TENANT and user_id == USER
        self.calls.append(("query_hot_news", arguments, tenant_id))
        metadata = {"workflow_id": "stable-workflow", "sql_query_id": "saved-query", "run_id": RUN}
        await on_bound(metadata)
        if self.error:
            raise self.error
        report = await super().execute(name="read_hot_news", arguments={"run_id": RUN},
                                       tenant_id=tenant_id, trace_id="trace")
        return {**report, **metadata, "query_model_provider": "local", "analyzed_news_count": 1}


async def prepared(service):
    conversation = await service.repository.create(TENANT, USER, "query test")
    return conversation, {"tenant_id": TENANT, "user_id": USER, "conversation_id": conversation.id,
                          "request_id": uuid4(), "content": QUESTION}


@pytest.mark.asyncio
async def test_new_query_reenters_model_with_evidence_and_replay_never_executes_again():
    service, _, inference, _ = runtime()
    service._tools = tools = QueryTools()
    events = []
    conversation, kwargs = await prepared(service)
    claim = await service.prepare_turn(**kwargs)

    async def publish(name, data):
        events.append((name, data))

    response = await service.run_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                                    claim=claim, on_event=publish, hot_news_query_allowed=True)
    assert response.status == "completed"
    assert [trace.name for trace in response.tools] == ["query_hot_news"]
    assert len(inference.calls) == 2
    final_input = json.loads(inference.calls[-1].messages[1].content)
    # StructuredInferenceService wraps user input as data, never system content.
    assert "saved-query" in json.dumps(final_input)
    assert "0.1200" in response.assistant_content
    assert "query_hot_news" in [item[1].get("name") for item in events]
    count = len(tools.calls)
    assert (await service.send(**kwargs, hot_news_query_allowed=True)).id == response.id
    assert len(tools.calls) == count and len(inference.calls) == 2
    followup = await service.send(**{**kwargs, "request_id": uuid4(), "content": "解释第一条新闻"},
                                  hot_news_query_allowed=True)
    assert followup.tools[0].name == "read_hot_news" and followup.tools[0].arguments["run_id"] == RUN


@pytest.mark.asyncio
async def test_read_role_cannot_execute_even_if_model_requests_query():
    async def malicious(request, count):
        from app.model_runtime.core import RawInferenceResult
        return RawInferenceResult(content=json.dumps({"action": "tool", "tool_name": "query_hot_news",
                                                       "arguments": {"question": QUESTION}}))
    service, _, _, _ = runtime(behavior=malicious, max_tool_calls=1)
    service._tools = tools = QueryTools()
    _, kwargs = await prepared(service)
    response = await service.send(**kwargs)
    assert response.tools[0].status == "denied"
    assert tools.calls == []


@pytest.mark.asyncio
async def test_query_command_never_receives_generic_read_tool_retry():
    service, _, _, _ = runtime()
    service._tools = tools = QueryTools()
    tools.error = ConnectionError("private transport detail")
    _, kwargs = await prepared(service)
    response = await service.send(**kwargs, hot_news_query_allowed=True)
    assert len(tools.calls) == 1 and response.tools[0].attempts == 1
    assert response.tools[0].result["workflow_id"] == "stable-workflow"
    assert "private transport detail" not in response.assistant_content


@pytest.mark.asyncio
async def test_cancel_keeps_bound_workflow_reference_and_stops_waiting():
    service, repo, _, _ = runtime()
    bound = asyncio.Event()

    class Blocked(QueryTools):
        async def execute_hot_news_query(self, *, on_bound, **kwargs):
            await on_bound({"workflow_id": "known-workflow", "sql_query_id": "known-query"})
            bound.set()
            await asyncio.Event().wait()

    service._tools = Blocked()
    conversation, kwargs = await prepared(service)
    task = asyncio.create_task(service.send(**kwargs, hot_news_query_allowed=True))
    await bound.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    turn = repo.turns[conversation.id][-1]
    assert turn.error_code == "interrupted"
    assert turn.tools[0].result["workflow_id"] == "known-workflow"


@pytest.mark.asyncio
async def test_real_http_adapter_is_used_for_planning_and_post_query_answer_without_network():
    config = load_model_runtime_config("deploy/model-runtime.compatible.example.yml")
    model_route = config.inference.model_routes[0]
    calls = []

    def reply(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == model_route and payload["stream"] is False
        assert request.headers["authorization"] == "Bearer test-only-key"
        plan = ({"action": "tool", "tool_name": "query_hot_news", "arguments": {"question": QUESTION}}
                if len(calls) == 1 else {"action": "respond", "answer": "根据查询结果，建议结合内容价值核对点击表现。"})
        return httpx.Response(200, json={"model": model_route, "choices": [{"message": {"content": json.dumps(plan)}}]},
                              headers={"x-request-id": f"http-model-{len(calls)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        inference = OpenAICompatibleInferenceClient(config.inference, api_key="test-only-key", client=client)
        model = NativeStructuredAgentClient(StructuredInferenceService(inference=inference,
                                           prompts=config.prompt_registry()), config)
        service = ConversationAgentService(repository=Repository(), model=model, tools=QueryTools())
        _, kwargs = await prepared(service)
        response = await service.send(**kwargs, hot_news_query_allowed=True)
    assert response.status == "completed" and response.model_request_ids == ["http-model-1", "http-model-2"]
    assert "根据查询结果" in response.assistant_content
    assert "saved-query" in calls[1]["messages"][1]["content"]
    assert "本地规则模拟" not in response.assistant_content


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"question": QUESTION, "tenant_id": TENANT}, {"question": "x"},
                                      {"question": QUESTION, "sql": "DROP"}])
async def test_actual_query_tool_rejects_identity_and_sql_arguments(arguments):
    tools = ConversationTools(hot_news=None, knowledge_store=None, embedding=None, embedding_version="test")
    runner = SimpleNamespace(available_for=lambda _: True, description=lambda: {}, execute=AsyncMock())
    tools.enable_hot_news_query(runner)
    with pytest.raises(ConversationToolDenied):
        await tools.execute_hot_news_query(arguments=arguments, tenant_id=TENANT, user_id=USER,
                                          trace_id="trace", allowed=True)
    runner.execute.assert_not_called()


def test_runtime_info_is_authenticated_and_admin_sensitive():
    service, _, _, _ = runtime(local_simulation=True)
    service._tools = QueryTools()
    app = FastAPI()
    app.include_router(router)
    app.state.conversation_service = service
    app.state.settings = SimpleNamespace(data_loop_gateway_token="public-test-token")
    client = TestClient(app)
    assert client.get("/api/v1/conversations/runtime").status_code == 401
    assert client.get("/api/v1/conversations/runtime", headers=headers()).json()["query_enabled"] is False
    info = client.get("/api/v1/conversations/runtime", headers=headers(**{"X-Hot-News-Roles": "hot-news:admin"})).json()
    assert info["query_enabled"] is True and info["model_provider"] == "local"
    assert "url" not in info and "api_key" not in str(info)


def test_compatible_template_and_manifest_bindings_and_missing_key_fail_closed(monkeypatch):
    config = load_model_runtime_config("deploy/model-runtime.compatible.example.yml")
    overlay = yaml.safe_load(open("deploy/docker-compose.compatible-model.example.yml"))
    for name in ["api", "hot-news-worker"]:
        manifest = overlay["services"][name]["environment"]["HOT_NEWS_RUNTIME_MANIFEST_JSON"]
        registry = NativeProductionBundleRuntimeRegistry.validation_only(config.prompt_registry(), manifest,
                    allowed_model_routes=config.inference.model_routes)
        assert registry is not None
    monkeypatch.delenv("NEWSAGENT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="credential"):
        build_model_runtime_ports(config, environment="e2e")
    with pytest.raises(ValueError, match="only allowed in e2e"):
        build_model_runtime_ports(config, environment="production")


def test_local_query_accepts_only_explicit_analysis_suffix_without_dropping_conditions():
    from app.sql_assistant.planner import local_query_intent, SqlAssistantQuestionError
    from app.sql_assistant.scenarios import load_sql_scenarios
    scenario = load_sql_scenarios("deploy/text2sql-scenes.local.yml").resolve("news-ranking")
    result = local_query_intent({"question": "查询点击率最高的前5条视频新闻并分析原因",
                                 "scenario": scenario.model_dump(mode="json")})
    assert result.content_type == "video" and result.sort_by == "ctr" and result.row_limit == 5
    with pytest.raises(SqlAssistantQuestionError):
        local_query_intent({"question": "查询点击率最高的前5条视频新闻北京并分析原因",
                           "scenario": scenario.model_dump(mode="json")})


@pytest.mark.parametrize("environment,enabled", [("production", True), ("e2e", False)])
def test_local_query_adapter_cannot_be_enabled_in_production(environment, enabled):
    with pytest.raises(ValueError):
        LocalConversationHotNewsQuery(SimpleNamespace(settings=SimpleNamespace(
            environment=environment, conversation_hot_news_query_enabled=enabled)))
