"""Persist safe semantic rejections through the ordinary conversation loop."""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.conversation.local import has_dated_news_request, local_conversation_plan
from app.conversation.query_understanding import (
    QueryPolicy, QueryResolutionError, local_query_understanding,
    resolve_query_understanding,
)
from app.conversation.tools import render_tool_results
from app.model_runtime.core import RawInferenceResult
from app.schemas.conversation import ToolTrace
from app.sql_assistant.input_boundary import SqlAssistantQuestionError
from app.sql_assistant.scenarios import load_sql_scenarios
from tests.test_conversation_agent import TENANT, USER, runtime


QUESTION = "你好，你能告诉我今日热点新闻有哪些吗？"


def policy():
    scenario = load_sql_scenarios("deploy/text2sql-scenes.enterprise-v2.yml").resolve("news-ranking")
    return QueryPolicy(
        scenario=scenario,
        supported_window_start="2026-10-03T00:00:00+08:00",
        supported_window_end="2026-10-03T01:00:00+08:00",
        data_watermark="2026-10-03T01:00:00+08:00",
    )


class ResolutionTools:
    descriptions = []

    def __init__(self, rejection):
        self.rejection = rejection
        self.calls = []
        self.bound = False

    def query_description(self, tenant):
        return {"name": "query_hot_news", "arguments": {"question": "original question"}}

    async def execute_hot_news_query(self, *, arguments, **kwargs):
        self.calls.append(arguments)
        raise self.rejection


@pytest.mark.asyncio
async def test_date_rejection_is_checkpointed_and_rendered_without_query_retry():
    approved = policy()
    proposal = local_query_understanding({"question": QUESTION, "policy": approved.model_dump(mode="json")})
    resolution = resolve_query_understanding(
        QUESTION, json.loads(proposal), approved, now=datetime(2026, 10, 7, 4, tzinfo=timezone.utc)
    )
    assert resolution.reason_code == "time_coverage_unavailable"
    service, repository, inference, _ = runtime()
    service._tools = tools = ResolutionTools(QueryResolutionError(resolution, request_id="understanding-test-request"))
    conversation = await repository.create(TENANT, USER, "date query")
    request = dict(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                   request_id=uuid4(), content=QUESTION, hot_news_query_allowed=True)
    turn = await service.send(**request)
    assert turn.status == "completed"
    assert len(tools.calls) == 1 and tools.calls[0]["question"] == QUESTION
    assert turn.tools[0].status == "failed"
    assert turn.tools[0].result["query_resolution"]["reason_code"] == "time_coverage_unavailable"
    assert turn.tools[0].result["understanding_model_request_id"] == "understanding-test-request"
    assert "understanding-test-request" in turn.model_request_ids
    assert "2026-10-03" in turn.assistant_content
    assert "没有执行 SQL 或启动热点运行" in turn.assistant_content
    assert repository.checkpoint_calls[0].tools[0] == turn.tools[0]
    assert not tools.bound
    before = len(inference.calls)
    assert (await service.send(**request)).id == turn.id
    assert len(tools.calls) == 1 and len(inference.calls) == before


@pytest.mark.asyncio
async def test_first_planner_cannot_remove_date_before_the_understanding_port():
    async def changed_question(request, count):
        plan = ({"action": "tool", "tool_name": "query_hot_news",
                 "arguments": {"question": "查询点击量最高的前5条新闻"}}
                if count == 1 else {"action": "respond", "answer": "本轮条件未确认，具体说明见下方。"})
        return RawInferenceResult(content=json.dumps(plan, ensure_ascii=False))

    service, repository, _, _ = runtime(behavior=changed_question)
    service._tools = tools = ResolutionTools(AssertionError("must not reach the query Port"))
    conversation = await repository.create(TENANT, USER, "original conditions")
    turn = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                              request_id=uuid4(), content=QUESTION, hot_news_query_allowed=True)
    assert tools.calls == []
    assert turn.tools[0].status == "denied" and turn.tools[0].attempts == 0
    assert turn.tools[0].error_code == "query_question_mismatch"
    assert "没有执行 SQL 或启动热点运行" in turn.assistant_content
    assert turn.user_content == QUESTION


@pytest.mark.asyncio
async def test_input_boundary_is_a_safe_explanation_not_a_transport_exception():
    service, repository, _, _ = runtime()
    service._tools = ResolutionTools(SqlAssistantQuestionError("untrusted transport text secret"))
    conversation = await repository.create(TENANT, USER, "boundary")
    turn = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                              request_id=uuid4(), content="看看热点新闻", hot_news_query_allowed=True)
    assert turn.tools[0].status == "denied"
    assert "超出新闻聚合数据的查询范围" in turn.assistant_content
    assert "secret" not in turn.assistant_content


@pytest.mark.parametrize("question", [QUESTION, "今日热点", "帮我看看热门新闻", "看看热点新闻"])
def test_natural_question_routes_to_query_without_erasing_the_original(question):
    proposal = json.loads(local_conversation_plan({
        "message": question, "history": [], "tool_results": [],
        "available_tools": [{"name": "query_hot_news"}],
    }))
    assert proposal["tool_name"] == "query_hot_news"
    assert proposal["arguments"] == {"question": question}


def test_read_role_dated_question_does_not_read_old_runs_as_today():
    proposal = json.loads(local_conversation_plan({
        "message": QUESTION, "history": [], "tool_results": [],
        "available_tools": [{"name": "list_hot_news"}],
    }))
    assert proposal["action"] == "respond"
    assert "已有报告不能冒充" in proposal["answer"]


def test_run_identifiers_are_not_misclassified_as_calendar_dates():
    assert not has_dated_news_request("读取热点运行 11111111-1111-4111-8111-111111111111")
    assert has_dated_news_request("查看2026-10-03热点新闻")


@pytest.mark.asyncio
async def test_model_cannot_substitute_saved_reports_for_a_dated_query():
    async def saved_reports(request, count):
        plan = ({"action": "tool", "tool_name": "list_hot_news", "arguments": {"limit": 5}}
                if count == 1 else {"action": "respond", "answer": "无法确认指定日期的热点，说明见下方。"})
        return RawInferenceResult(content=json.dumps(plan, ensure_ascii=False))

    service, repository, _, tools = runtime(behavior=saved_reports)
    conversation = await repository.create(TENANT, USER, "no stale substitution")
    turn = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                              request_id=uuid4(), content=QUESTION)
    assert tools.calls == []
    assert turn.tools[0].error_code == "dated_request_requires_query"
    assert "未使用历史榜单代替请求结果" in turn.assistant_content


def test_invalid_resolution_is_not_rendered_as_authoritative_data():
    rendered = render_tool_results([ToolTrace(
        name="query_hot_news", status="failed", attempts=1,
        arguments={},
        result={"query_resolution": {"message": "SELECT secret FROM company"}},
        error_code="query_invalid_proposal",
    )])
    assert "未通过结构校验" in rendered
    assert "secret" not in rendered


def test_ready_resolution_is_rendered_with_the_same_report():
    approved = policy()
    understanding = json.loads(local_query_understanding({
        "question": "看看热点新闻", "policy": approved.model_dump(mode="json"),
    }))
    resolution = resolve_query_understanding("看看热点新闻", understanding, approved)
    assert resolution.status == "ready"
    result = {"query_resolution": resolution.model_dump(mode="json"),
              "sql_query_id": "saved-query", "run_id": str(uuid4()),
              "window_start": "2026-10-03T00:00:00+08:00",
              "window_end": "2026-10-03T01:00:00+08:00", "items": []}
    rendered = render_tool_results([ToolTrace(name="query_hot_news", status="completed", attempts=1,
                                             arguments={}, result=result)])
    assert "2026-10-03" in rendered and "最多5条候选" in rendered
    assert "项目计算榜" in rendered and "saved-query" in rendered
