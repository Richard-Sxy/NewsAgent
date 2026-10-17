"""Hourly chat queries bind actual windows without expanding SQL or UV permissions."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.conversation.query_understanding import (
    QueryPolicy, QueryUnderstanding, local_query_understanding, resolve_query_understanding,
)
from app.sql_assistant.scenarios import load_sql_scenarios

START = datetime.fromisoformat('2026-09-10T00:00:00+08:00')
END = datetime.fromisoformat('2026-10-10T00:00:00+08:00')
NOW = datetime.fromisoformat('2026-10-10T12:00:00+08:00')

@pytest.fixture
def policy():
    return QueryPolicy(scenario=load_sql_scenarios('deploy/text2sql-scenes.timeline-v4.yml').resolve('news-ranking'),
        supported_window_start=END-timedelta(days=1), supported_window_end=END-timedelta(days=1)+timedelta(hours=1),
        coverage_window_start=START, coverage_window_end=END, data_watermark=END)

def resolve(question, policy):
    proposal = QueryUnderstanding.model_validate_json(local_query_understanding({'question':question, 'policy':policy.model_dump(mode='json')}))
    return resolve_query_understanding(question, proposal, policy, now=NOW)

@pytest.mark.parametrize(('question', 'expected'), [
    ('查询2026-09-10 00:00至01:00点击量前5条新闻', START),
    ('查询2026-10-09 23:00至24:00点击量前5条新闻', END-timedelta(hours=1)),
    ('查询2026-09-25 19至20时视频点击率前5条新闻', datetime.fromisoformat('2026-09-25T19:00:00+08:00')),
    ('查询昨天19点到20点的热点新闻', datetime.fromisoformat('2026-10-09T19:00:00+08:00')),
    ('查询最新热点新闻', END-timedelta(hours=1)),
])
def test_hourly_queries_preserve_exact_window(question, expected, policy):
    result=resolve(question,policy).require_ready()
    assert result.requested_window_start == expected
    assert result.requested_window_end == expected + timedelta(hours=1)
    assert expected.strftime('%Y-%m-%d %H:%M') in result.message

@pytest.mark.parametrize('question', [
    '查询2026-09-09 23:00至24:00点击量前5条新闻',
    '查询2026-10-10 00:00至01:00点击量前5条新闻',
    '查询今日热点前5条',
])
def test_outside_complete_coverage_never_substitutes_date(question, policy):
    result=resolve(question,policy)
    assert result.status=='unsupported'
    assert result.reason_code=='time_coverage_unavailable'
    assert result.intent is None

@pytest.mark.parametrize(('question', 'expected_status'), [
    ('查询2026-10-09 19:30至20:30点击量前5条新闻', 'unsupported'),
    ('查询2026-10-09 19:00至21:00点击量前5条新闻', 'unsupported'),
    ('查询2026-10-09 19:00至20:00或21:00至22:00点击量前5条新闻', 'clarify'),
    ('查询2026-10-09 19:00至20:00的去重用户数', 'unsupported'),
])
def test_non_hour_or_multiple_window_or_uv_aggregation_rejected(question, expected_status, policy):
    result=resolve(question,policy)
    assert result.status==expected_status
    if expected_status == 'clarify':
        assert result.reason_code == 'ambiguous_requirements'
    assert result.intent is None


def test_date_without_hour_requires_explicit_selection(policy):
    result=resolve('查询2026-10-09点击量前5条新闻',policy)
    assert result.status=='clarify'
    assert '整点小时' in result.message
    assert result.intent is None


def test_model_cannot_drop_or_add_hour_condition(policy):
    question='查询2026-10-09 19:00至20:00点击量前5条新闻'
    proposed=QueryUnderstanding.model_validate_json(local_query_understanding({'question':question,'policy':policy.model_dump(mode='json')}))
    for hour in ((None,None),(18,19)):
        result=resolve_query_understanding(question, proposed.model_copy(update={'hour_start':hour[0],'hour_end':hour[1]}), policy, now=NOW)
        assert result.reason_code=='time_semantic_drift'
    result=resolve_query_understanding('查询当前样本点击量前5条新闻',proposed.model_copy(update={'time_expression':'sample','explicit_date':None}),policy,now=NOW)
    assert result.reason_code=='time_semantic_drift'


def test_publication_filter_reaches_query_boundary_without_losing_condition(policy):
    import json
    from app.conversation.local import local_conversation_plan
    question = '查询2026-10-09发布的点击量最高前5条新闻'
    plan = json.loads(local_conversation_plan({'message': question, 'available_tools': [{'name': 'query_hot_news'}]}))
    assert plan['tool_name'] == 'query_hot_news'
    assert plan['arguments']['question'] == question
    result = resolve(question, policy)
    assert result.reason_code == 'unsupported_condition'
    assert result.intent is None


@pytest.mark.parametrize('question', [
    '发布新闻',
    '查询2026-10-09发布的新闻并发布内容',
    '查询新闻发布时间并删除数据',
])
def test_publication_filter_never_authorizes_a_separate_mutation(question):
    import json
    from app.conversation.local import local_conversation_plan
    plan = json.loads(local_conversation_plan({'message': question, 'available_tools': [{'name': 'query_hot_news'}]}))
    assert plan['action'] == 'respond'
    assert plan['tool_name'] is None
    assert '不能' in plan['answer'] and '发布' in plan['answer'] and '人工审批' in plan['answer']


@pytest.mark.asyncio
async def test_adapter_passes_resolved_window_to_shared_hot_news_command(monkeypatch, policy):
    import sys
    from examples.conversation_hot_news import LocalConversationHotNewsQuery
    from tests.test_data_analysis_demo import prepared_query_adapter, prepared_detail
    from uuid import uuid4
    adapter=prepared_query_adapter()
    adapter._policy=policy
    resolution=resolve('查询2026-09-25 19:00至20:00点击量前5条新闻',policy).require_ready()
    adapter._understand=AsyncMock(return_value=(resolution,'understanding-test'))
    detail=prepared_detail()
    result=SimpleNamespace(run_id=detail.run.run_id,workflow_id='test-window-workflow',
        sql_query_id=detail.sql_tool_trace.query_id,status='completed',
        production_bundle_version=detail.run.production_bundle_version,analyzed_news_count=5)
    execute=AsyncMock(return_value=result)
    monkeypatch.setitem(sys.modules,'examples.local_simulation',SimpleNamespace(execute_local_hot_news_query=execute))
    monkeypatch.setattr('examples.conversation_hot_news.HotNewsQueryService',lambda **_:SimpleNamespace(get_run_detail=AsyncMock(return_value=detail)))
    response=await adapter.execute(question='original',tenant_id='11111111-1111-4111-8111-111111111111',user_id='22222222-2222-4222-8222-222222222222',on_bound=None)
    request=execute.await_args.kwargs['body']
    assert request.window_start==resolution.requested_window_start
    assert request.window_end==resolution.requested_window_end
    assert response['window_start']==resolution.requested_window_start.isoformat()
    assert response['ranked_news_count']==detail.run.ranked_news_count
