"""The user's exact chat request must not be turned into an hourly clarification."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.modules.conversation.query_understanding import (
    QueryPolicy, QueryUnderstanding, local_query_understanding, resolve_query_understanding,
)
from app.modules.conversation.structured_query_resolution import resolve_structured_query
from app.modules.conversation.tools import ConversationTools, render_tool_results
from app.model_runtime.config_file import load_model_runtime_config
from app.sql_assistant.scenarios import load_sql_scenarios
from simulation.conversation_hot_news import LocalConversationHotNewsQuery
from tests.test_conversation_agent import runtime, TENANT, USER


QUESTION = "你好，帮我找一下今日新闻热点。"
NOW = datetime.fromisoformat("2026-10-27T10:25:00+08:00")


def policy(**changes):
    data = dict(
        scenario=load_sql_scenarios("deploy/text2sql-scenes.real-news-v8.yml").resolve("news-ranking"),
        supported_window_start="2026-10-14T00:00:00+08:00",
        supported_window_end="2026-10-14T01:00:00+08:00",
        coverage_window_start="2026-09-30T00:00:00+08:00",
        coverage_window_end="2026-10-14T20:00:00+08:00",
        data_watermark="2026-10-14T20:00:00+08:00",
    )
    data.update(changes)
    return QueryPolicy(**data)


def propose(question, selected, now=NOW):
    return QueryUnderstanding.model_validate_json(local_query_understanding({
        "question": question, "policy": selected.model_dump(mode="json"),
        "temporal_context": {"server_now": now.isoformat()},
    }))


@pytest.mark.parametrize("resolve", [resolve_query_understanding, resolve_structured_query])
def test_exact_question_understood_as_today_and_stale_coverage_is_not_ambiguity(resolve):
    selected = policy()
    proposal = propose(QUESTION, selected)
    assert proposal.status == "ready" and proposal.time_expression == "today"
    result = resolve(QUESTION, proposal, selected, now=NOW)
    assert result.status == "unsupported" and result.reason_code == "time_coverage_unavailable"
    assert result.requested_window_start.isoformat() == "2026-10-27T00:00:00+08:00"
    assert result.requested_window_end.isoformat() == "2026-10-27T10:00:00+08:00"
    assert result.intent is None and not result.canonical_question
    trace = SimpleNamespace(name="query_hot_news", status="completed",
                            result={"query_resolution": result.model_dump(mode="json")})
    text = render_tool_results([trace])
    assert "2026-10-14T20:00+08:00" in text
    assert "数据覆盖不足" in text and "没有执行 SQL" in text
    assert "每次查询一个整点小时" not in text
    assert "请补充" not in text


@pytest.mark.parametrize("resolve", [resolve_query_understanding, resolve_structured_query])
@pytest.mark.parametrize("question,hours", [
    ("查看2026年10月13日热点新闻", 24),
    ("查询2026年10月13日8点至10点的热点新闻", 2),
])
def test_known_calendar_scope_is_not_shortened_to_pass_hourly_adapter(resolve, question, hours):
    selected = policy()
    result = resolve(question, propose(question, selected), selected, now=NOW)
    assert result.reason_code == "window_not_approved" and result.status == "unsupported"
    assert (result.requested_window_end - result.requested_window_start).total_seconds() == hours * 3600
    assert "跨小时去重用户数" in result.message
    assert result.intent is None and not result.canonical_question


@pytest.mark.parametrize("resolve", [resolve_query_understanding, resolve_structured_query])
def test_today_uses_full_elapsed_day_to_watermark_never_last_hour(resolve):
    selected = policy(coverage_window_end="2026-10-27T09:00:00+08:00",
                      data_watermark="2026-10-27T09:00:00+08:00")
    result = resolve(QUESTION, propose(QUESTION, selected), selected, now=NOW)
    assert result.reason_code == "window_not_approved"
    assert result.requested_window_start.isoformat() == "2026-10-27T00:00:00+08:00"
    assert result.requested_window_end.isoformat() == "2026-10-27T09:00:00+08:00"


@pytest.mark.parametrize("resolve", [resolve_query_understanding, resolve_structured_query])
def test_single_completed_hour_of_today_can_still_execute_without_asking_user(resolve):
    selected = policy(coverage_window_end="2026-10-27T01:00:00+08:00",
                      data_watermark="2026-10-27T01:00:00+08:00")
    result = resolve(QUESTION, propose(QUESTION, selected), selected, now=NOW).require_ready()
    assert result.requested_window_start.isoformat() == "2026-10-27T00:00:00+08:00"
    assert result.requested_window_end.isoformat() == "2026-10-27T01:00:00+08:00"


@pytest.mark.parametrize("resolve", [resolve_query_understanding, resolve_structured_query])
def test_partial_day_coverage_does_not_become_complete_today(resolve):
    selected = policy(
        supported_window_start="2026-10-27T08:00:00+08:00",
        supported_window_end="2026-10-27T09:00:00+08:00",
        coverage_window_start="2026-10-27T08:00:00+08:00",
        coverage_window_end="2026-10-27T10:00:00+08:00",
        data_watermark="2026-10-27T10:00:00+08:00",
    )
    result = resolve(QUESTION, propose(QUESTION, selected), selected, now=NOW)
    assert result.reason_code == "time_coverage_unavailable"
    assert result.requested_window_start.hour == 0


@pytest.mark.parametrize("resolve", [resolve_query_understanding, resolve_structured_query])
def test_timezone_midnight_and_future_watermark_do_not_include_future_data(resolve):
    selected = policy(coverage_window_end="2026-10-28T00:00:00+08:00",
                      data_watermark="2026-10-28T00:00:00+08:00")
    now = datetime.fromisoformat("2026-10-26T16:05:00+00:00")
    result = resolve(QUESTION, propose(QUESTION, selected, now), selected, now=now)
    assert result.reason_code == "no_complete_hour"
    assert result.requested_window_start.isoformat() == "2026-10-27T00:00:00+08:00"
    assert result.requested_window_end == result.requested_window_start


@pytest.mark.asyncio
async def test_exact_question_through_conversation_service_has_no_sql_or_saved_report_fallback():
    service, repository, _, _ = runtime()
    state = SimpleNamespace(
        settings=SimpleNamespace(
            environment="e2e", conversation_hot_news_query_enabled=True,
            conversation_hot_news_query_scenario="news-ranking", conversation_hot_news_query_hour=0,
            sql_assistant_scenarios_path="deploy/text2sql-scenes.real-news-v8.yml",
            sql_assistant_dataset_profile="real-news-v8",
        ),
        sql_assistant=SimpleNamespace(agent=service._model, preview=AsyncMock()),
        temporal=SimpleNamespace(start_workflow=AsyncMock()),
    )
    reports = SimpleNamespace(get_run_detail=AsyncMock(), list_runs=AsyncMock())
    tools = ConversationTools(hot_news=reports, knowledge_store=None, embedding=None, embedding_version="test")
    tools.enable_hot_news_query(LocalConversationHotNewsQuery(state, clock=lambda: NOW))
    service._tools = tools
    conversation = await repository.create(TENANT, USER, "calendar regression")
    result = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                                request_id=uuid4(), content=QUESTION, hot_news_query_allowed=True)
    assert result.status == "completed"
    assert [trace.name for trace in result.tools] == ["query_hot_news"]
    assert result.tools[0].result["query_resolution"]["reason_code"] == "time_coverage_unavailable"
    assert "数据覆盖不足" in result.assistant_content
    assert "每次查询一个整点小时" not in result.assistant_content
    assert "请补充" not in result.assistant_content
    state.sql_assistant.preview.assert_not_awaited()
    state.temporal.start_workflow.assert_not_awaited()
    reports.get_run_detail.assert_not_awaited()
    reports.list_runs.assert_not_awaited()


@pytest.mark.parametrize("filename,prefix", [
    ("model-runtime.local.yml", "native"),
    ("model-runtime.compatible.example.yml", "compatible"),
])
def test_v6_prompt_preserves_clear_days_even_when_source_cannot_execute(filename, prefix):
    config = load_model_runtime_config("deploy/" + filename)
    binding = config.agent_scene("query_understanding")
    assert binding.prompt_version == prefix + "-query-understanding-v6"
    prompt = config.prompt_registry().resolve(scene="query_understanding", version=binding.prompt_version).system_prompt
    assert "日期本身是完整的时间要求，不因缺小时而clarify" in prompt
    assert "今日、全天、多小时请求仍输出ready提案" in prompt
    # Historical prompt remains registered so saved requests can be audited.
    old = config.prompt_registry().resolve(scene="query_understanding", version=prefix + "-query-understanding-v5")
    assert "当前仅支持一个整点小时" in old.system_prompt
