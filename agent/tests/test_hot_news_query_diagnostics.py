"""Observable query failures and authoritative report counts at the chat boundary."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.conversation.tools import ConversationTools, render_tool_results
from app.schemas.conversation import ToolTrace
from tests.test_conversation_agent import RUN, TENANT, USER, runtime
from tests.test_conversation_hot_news_query import QueryTools, prepared


PRIVATE_DETAIL = "private model response with credential=test-only-secret"
BINDING = {"workflow_id": "stable-workflow", "sql_query_id": "saved-query", "run_id": RUN}


class FailingQueryTools(QueryTools):
    def __init__(self, error, *, bind):
        super().__init__()
        self.error = error
        self.bind = bind

    async def execute_hot_news_query(self, *, arguments, tenant_id, user_id, allowed, on_bound, **kwargs):
        assert allowed and tenant_id == TENANT and user_id == USER
        self.calls.append(("query_hot_news", arguments, tenant_id))
        if self.bind:
            await on_bound(BINDING)
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code,error_code", [
    (502, "query_workflow_failed"),
    (503, "query_backend_unavailable"),
    (504, "query_wait_timeout"),
])
@pytest.mark.parametrize("bind", [False, True])
async def test_http_failure_is_classified_without_reexecution_or_private_details(status_code, error_code, bind):
    service, repo, inference, _ = runtime()
    service._tools = tools = FailingQueryTools(
        HTTPException(status_code=status_code, detail=PRIVATE_DETAIL, headers={"X-Private": PRIVATE_DETAIL}),
        bind=bind,
    )
    conversation, kwargs = await prepared(service)
    response = await service.send(**kwargs, hot_news_query_allowed=True)

    assert len(tools.calls) == 1
    assert len(response.tools) == 1
    trace = response.tools[0]
    assert trace.status == "failed" and trace.attempts == 1
    assert trace.error_code == error_code
    assert trace.error_code != "query_in_progress"
    assert trace.result == (BINDING if bind else {})
    assert repo.checkpoint_calls[-1].tools[0] == trace
    expected_message = {502: "热点运行失败", 503: "热点查询服务暂不可用", 504: "等待热点运行超时"}[status_code]
    assert expected_message in response.assistant_content
    assert PRIVATE_DETAIL not in response.model_dump_json()
    assert "test-only-secret" not in response.assistant_content
    if bind:
        assert BINDING["workflow_id"] in response.assistant_content
        assert BINDING["sql_query_id"] in response.assistant_content

    # Replaying the same request must return its saved terminal turn, even when
    # the underlying command failed after binding a durable Workflow.
    model_call_count = len(inference.calls)
    replay = await service.send(**kwargs, hot_news_query_allowed=True)
    assert replay.id == response.id and replay.tools == response.tools
    assert len(tools.calls) == 1 and len(inference.calls) == model_call_count
    assert repo.turns[conversation.id] == [response]


@pytest.mark.asyncio
@pytest.mark.parametrize("bind,error_code", [
    (False, "hot_news_query_unavailable"),
    (True, "query_result_unavailable"),
])
async def test_unclassified_failure_preserves_binding_without_claiming_workflow_is_running(bind, error_code):
    service, _, _, _ = runtime()
    service._tools = tools = FailingQueryTools(ConnectionError(PRIVATE_DETAIL), bind=bind)
    _, kwargs = await prepared(service)
    response = await service.send(**kwargs, hot_news_query_allowed=True)

    assert len(tools.calls) == 1
    assert response.tools[0].error_code == error_code
    assert response.tools[0].result == (BINDING if bind else {})
    assert response.tools[0].attempts == 1
    assert PRIVATE_DETAIL not in response.model_dump_json()


class Snapshot:
    def __init__(self, values):
        self.values = values

    def model_dump(self, **kwargs):
        return dict(self.values)


def completed_detail(*, include_counts):
    start = datetime.fromisoformat("2026-10-09T00:00:00+08:00")
    run = SimpleNamespace(window_start=start, window_end=start + timedelta(hours=1))
    if include_counts:
        run.ranked_news_count = 6
        run.analyzed_news_count = 4
    items = [
        SimpleNamespace(
            rank=rank, news_id=f"news-{rank}", title=f"合成新闻 {rank}",
            metrics=Snapshot({"clicks": rank * 12, "ctr": "0.1200"}),
            hot_score=Snapshot({"score": "0.8000"}),
            analysis=SimpleNamespace(trend_assessment="关注度持续上升", dominant_driver="clicks", limitations=[])
            if rank <= 4 else None,
        )
        for rank in range(1, 7)
    ]
    return SimpleNamespace(run=run, ranked_news=items, sql_tool_trace=SimpleNamespace(query_id="saved-query"))


@pytest.mark.asyncio
@pytest.mark.parametrize("include_counts", [False, True])
async def test_read_and_query_counts_describe_full_report_instead_of_displayed_subset(include_counts):
    hot_news = SimpleNamespace(get_run_detail=AsyncMock(return_value=completed_detail(include_counts=include_counts)))
    tools = ConversationTools(hot_news=hot_news, knowledge_store=None, embedding=None, embedding_version="test")
    query_runner = SimpleNamespace(
        available_for=lambda tenant: tenant == TENANT,
        description=lambda: {"name": "query_hot_news"},
        execute=AsyncMock(return_value={**BINDING, "analyzed_news_count": 4, "query_model_provider": "local"}),
    )
    tools.enable_hot_news_query(query_runner)

    read = await tools.execute(name="read_hot_news", arguments={"run_id": RUN}, tenant_id=TENANT, trace_id="read")
    assert read["ranked_news_count"] == 6 and read["analyzed_news_count"] == 4
    assert len(read["items"]) == 5 and read["truncated"] is True
    selected = await tools.execute(name="read_hot_news", arguments={"run_id": RUN, "news_rank": 1},
                                   tenant_id=TENANT, trace_id="selected")
    assert len(selected["items"]) == 1
    assert selected["ranked_news_count"] == 6 and selected["analyzed_news_count"] == 4

    query = await tools.execute_hot_news_query(arguments={"question": "查询新闻前5条"}, tenant_id=TENANT,
                                              user_id=USER, trace_id="query", allowed=True)
    query_runner.execute.assert_awaited_once()
    assert query["ranked_news_count"] == 6 and query["analyzed_news_count"] == 4
    text = render_tool_results([ToolTrace(name="query_hot_news", status="completed", attempts=1,
                                         arguments={"question": "查询新闻前5条"}, result=query)])
    assert "已分析 4 篇新闻，已上榜 6 篇" in text
    assert "已上榜 0 篇" not in text
