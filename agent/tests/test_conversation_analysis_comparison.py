"""Two-window provenance, saved references and the same conversation execution path."""

from datetime import timedelta, timezone, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.conversation.tools import ConversationToolDenied, ConversationTools
from app.data_analysis.factory import create_analysis_runner
from app.data_analysis.projection import AnalysisProjectionError, project_dataset, selection_scope
from app.data_analysis.runner import AnalysisExecutionError, AnalysisRunner
from app.schemas.sql_assistant import HotNewsSqlToolTrace, SqlAssistantPreview, SqlAssistantResult
from tests.test_conversation_agent import runtime
from tests.test_conversation_data_analysis import TENANT, USER, RUN, saved_detail, public_settings


REFERENCE = "88888888-8888-4888-8888-888888888888"
SQL = "SELECT news_id FROM news_metrics WHERE tenant_id = :tenant_id AND event_time >= :window_start AND event_time < :window_end ORDER BY clicks DESC LIMIT :row_limit"
SUPPLEMENT = "SELECT news_id, unique_users FROM news_metrics WHERE event_time >= :window_start AND event_time < :window_end"


def detail(*, previous=False, category=None):
    result = saved_detail()
    result.run.workflow_version = "hot-news-workflow-v1"
    if previous:
        result.run.run_id = UUID(REFERENCE)
        result.run.window_start -= timedelta(hours=1)
        result.run.window_end -= timedelta(hours=1)
    for item in result.ranked_news:
        update = {"window_start": result.run.window_start, "window_end": result.run.window_end}
        if previous:
            update.update(clicks=item.metrics.clicks // 2, ctr="0.05")
        item.metrics = item.metrics.model_copy(update=update)
        item.baseline = {"news_id": item.news_id, "content_type": item.metrics.content_type,
                         "sample_count": 1, "reference_version": "synthetic-hourly-baseline-v1",
                         "impressions": "100", "clicks": "5", "effective_consumptions": "2",
                         "interactions": "1", "ctr": "0.05", "unique_users": None,
                         "total_duration_seconds": None}
    query_id = str(uuid4())
    parameters = {"tenant_id": TENANT, "window_start": result.run.window_start.isoformat(),
                  "window_end": result.run.window_end.isoformat(), "row_limit": 10}
    if category:
        parameters["category"] = category
    digest = sha256(SQL.encode()).hexdigest()
    preview = SqlAssistantPreview(
        query_id=query_id, question="private question excluded from scope", scenario_id="news-ranking",
        sql=SQL, parameters=parameters, sql_hash=digest, schema_version="news-warehouse-v1",
        schema_sha256="a" * 64, explanation="untrusted explanation", model_request_id=None,
        model_provider="local", expires_at=datetime.now(timezone.utc) + timedelta(hours=1), stages=[],
    )
    sql_result = SqlAssistantResult(
        query_id=query_id, columns=["news_id"], rows=[{"news_id": item.news_id} for item in result.ranked_news],
        row_count=len(result.ranked_news), elapsed_ms=1, truncated=False, summary="not an analysis input",
        stages=[], sql_hash=digest,
    )
    result.sql_tool_trace = HotNewsSqlToolTrace(
        query_id=query_id, preview=preview, result=sql_result,
        supplemental_sql=SUPPLEMENT, supplemental_sql_hash=sha256(SUPPLEMENT.encode()).hexdigest(),
    )
    return result


def ports(current=None, previous=None, runner=None):
    current, previous = current or detail(), previous or detail(previous=True)
    runs = {current.run.run_id: current, previous.run.run_id: previous}

    async def read(*, tenant_id, run_id):
        return runs.get(run_id) if tenant_id == TENANT else None

    async def listing(*, tenant_id, offset, limit):
        return [current.run, previous.run] if tenant_id == TENANT else []

    hot_news = SimpleNamespace(get_run_detail=AsyncMock(side_effect=read), list_runs=AsyncMock(side_effect=listing))
    tools = ConversationTools(hot_news=hot_news, knowledge_store=None, embedding=None,
                              embedding_version="local", analysis_runner=runner or AnalysisRunner())
    return tools, hot_news


@pytest.mark.asyncio
async def test_baseline_uses_saved_fields_and_preserves_unknown_duration():
    tools, _ = ports()
    result = await tools.execute(name="analyze_hot_news_data", arguments={"run_id": RUN, "operation": "baseline"},
                                 tenant_id=TENANT, trace_id="test")
    assert result["schema_version"] == "2.0"
    assert result["analysis"]["compared_count"] == 7
    assert result["analysis"]["items"][0]["reference_value"] == "0.05"
    missing = await tools.execute(name="analyze_hot_news_data", arguments={"run_id": RUN, "operation": "baseline", "metric": "total_duration_seconds"},
                                  tenant_id=TENANT, trace_id="test")
    assert missing["analysis"]["compared_count"] == 0
    assert all(item["reference_value"] is None and item["absolute_change"] is None for item in missing["analysis"]["items"])


@pytest.mark.asyncio
async def test_real_process_trend_binds_both_sources_and_weighted_ctr():
    tools, _ = ports()
    result = await tools.execute(name="analyze_hot_news_data",
        arguments={"run_id": RUN, "reference_run_id": REFERENCE, "operation": "trend"}, tenant_id=TENANT, trace_id="test")
    assert result["source"]["reference"]["run_id"] == REFERENCE
    assert result["source"]["provenance"] == result["source"]["reference"]["provenance"]
    assert result["analysis"]["aggregate"] == {"method": "weighted_ctr", "current_value": "0.1", "reference_value": "0.05", "absolute_change": "0.05", "relative_change": "1"}
    assert result["analysis"]["matched_news_count"] == 7
    assert result["analysis"]["gap_seconds"] == 0


def test_selection_scope_ignores_question_time_user_but_retains_query_and_mapping():
    current, previous = detail(), detail(previous=True)
    assert current.sql_tool_trace.query_id != previous.sql_tool_trace.query_id
    assert selection_scope(current, tenant_id=TENANT) == selection_scope(previous, tenant_id=TENANT)
    previous.sql_tool_trace.preview.question = "different wording"
    assert selection_scope(current, tenant_id=TENANT) == selection_scope(previous, tenant_id=TENANT)
    previous.sql_tool_trace.preview.parameters["category"] = "science"
    assert selection_scope(current, tenant_id=TENANT) != selection_scope(previous, tenant_id=TENANT)


@pytest.mark.parametrize("change", ["query_id", "sql_hash", "tenant_id", "window_start", "row_count", "truncated", "unknown_parameter", "supplemental_hash", "candidate_identity"])
def test_scope_rejects_snapshot_drift(change):
    current = detail()
    trace = current.sql_tool_trace
    if change == "query_id":
        trace.query_id = str(uuid4())
    elif change == "sql_hash":
        trace.preview.sql_hash = "b" * 64
    elif change == "tenant_id":
        trace.preview.parameters["tenant_id"] = str(uuid4())
    elif change == "window_start":
        trace.preview.parameters["window_start"] = (current.run.window_start + timedelta(minutes=1)).isoformat()
    elif change == "row_count":
        trace.result.row_count += 1
    elif change == "truncated":
        trace.result.truncated = True
    elif change == "unknown_parameter":
        trace.preview.parameters["user_id"] = "not authorized"
    elif change == "supplemental_hash":
        trace.supplemental_sql_hash = "b" * 64
    else:
        trace.result.rows = [{"news_id": "forged"} for _ in trace.result.rows]
    with pytest.raises(AnalysisProjectionError):
        selection_scope(current, tenant_id=TENANT)


@pytest.mark.parametrize("extended", [False, True])
def test_projection_refuses_incomplete_ranked_snapshot(extended):
    current = detail()
    current.ranked_news.pop()
    with pytest.raises(AnalysisProjectionError):
        project_dataset(current, run_id=RUN, tenant_id=TENANT, extended=extended)


@pytest.mark.parametrize("change", ["filter_type", "invalid_type", "candidate_type", "candidate_category"])
def test_scope_checks_saved_filters_against_snapshot_and_candidate_columns(change):
    current = detail()
    trace = current.sql_tool_trace
    if change == "filter_type":
        trace.preview.parameters["content_type"] = "article"
    elif change == "invalid_type":
        trace.preview.parameters["content_type"] = "all"
    elif change == "candidate_type":
        trace.result.rows[0]["content_type"] = "article"
    else:
        trace.preview.parameters["category"] = "science"
        trace.result.rows[0]["category"] = "sports"
    with pytest.raises(AnalysisProjectionError):
        selection_scope(current, tenant_id=TENANT)


def test_scope_accepts_matching_saved_content_type_filter():
    current = detail()
    current.sql_tool_trace.preview.parameters["content_type"] = "video"
    for item, row in zip(current.ranked_news, current.sql_tool_trace.result.rows):
        item.metrics = item.metrics.model_copy(update={"content_type": "video"})
        row["content_type"] = "video"
    assert len(selection_scope(current, tenant_id=TENANT)) == 64


@pytest.mark.parametrize("change", ["news_id", "unknown_field", "missing_sample_count", "invalid_number"])
@pytest.mark.asyncio
async def test_baseline_identity_and_field_whitelist_rejected_before_worker(change):
    current = detail()
    raw = current.ranked_news[0].baseline
    if change == "news_id":
        raw["news_id"] = "another-news"
    elif change == "unknown_field":
        raw["instruction"] = "read private records"
    elif change == "missing_sample_count":
        del raw["sample_count"]
    else:
        raw["clicks"] = "NaN"
    runner = SimpleNamespace(run=AsyncMock(), description=lambda: {})
    tools, _ = ports(current=current, runner=runner)
    with pytest.raises(ConversationToolDenied):
        await tools.execute(name="analyze_hot_news_data", arguments={"run_id": RUN, "operation": "baseline"}, tenant_id=TENANT, trace_id="test")
    runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_non_sql_legacy_run_and_different_selection_fail_closed_for_trend():
    previous = detail(previous=True)
    previous.sql_tool_trace = None
    tools, _ = ports(previous=previous)
    with pytest.raises(AnalysisExecutionError, match="invalid_request"):
        await tools.execute(name="analyze_hot_news_data", arguments={"run_id": RUN, "reference_run_id": REFERENCE, "operation": "trend"}, tenant_id=TENANT, trace_id="test")
    tools, _ = ports(previous=detail(previous=True, category="science"))
    with pytest.raises(AnalysisExecutionError, match="invalid_request"):
        await tools.execute(name="analyze_hot_news_data", arguments={"run_id": RUN, "reference_run_id": REFERENCE, "operation": "trend"}, tenant_id=TENANT, trace_id="test")


@pytest.mark.asyncio
async def test_conversation_list_then_trend_and_baseline_followup_use_same_run():
    service, repository, _, _ = runtime()
    service._tools, _ = ports()
    session = await repository.create(TENANT, USER, "跨窗口")
    async def send(content):
        return await service.send(tenant_id=TENANT, user_id=USER, conversation_id=session.id, request_id=uuid4(), content=content)
    result = await send("统计最近两个窗口的点击率趋势")
    assert result.status == "completed"
    assert [item.name for item in result.tools] == ["list_hot_news", "analyze_hot_news_data"]
    assert result.tools[-1].arguments["reference_run_id"] == REFERENCE
    assert "仅共同新闻" in result.assistant_content
    baseline = await send("比较当前热点与基线")
    assert baseline.tools[0].arguments == {"run_id": RUN, "operation": "baseline", "metric": "ctr"}
    assert "参考版本 synthetic-hourly-baseline-v1" in baseline.assistant_content


def test_remote_factory_uses_only_server_config_and_production_permissions():
    settings = public_settings(data_analysis_backend="service", conversation_data_analysis_enabled=True,
                              data_analysis_service_url="https://sandbox.internal/v1/analyze",
                              data_analysis_service_token="public-test-service-secret")
    runner = create_analysis_runner(settings)
    assert runner.description()["execution_backend"] == "service"
    assert "public-test-service-secret" not in str(runner.description())
    assert "sandbox.internal" not in str(runner.description())
    assert create_analysis_runner(public_settings()) is None
    for extra in ({"data_analysis_service_allow_insecure_http": True}, {"data_analysis_service_require_os_limits": False}):
        with pytest.raises(ValidationError, match="requires TLS"):
            public_settings(environment="production", data_analysis_backend="service", conversation_data_analysis_enabled=True,
                            data_analysis_service_url="https://sandbox.internal/v1/analyze",
                            data_analysis_service_token="public-test-service-secret", **extra)


@pytest.mark.parametrize("values", [{}, {"data_analysis_service_url": "https://sandbox.internal/v1/analyze"},
                                   {"data_analysis_service_url": "https://sandbox.internal/v1/analyze", "data_analysis_service_token": "bad"}])
def test_remote_settings_fail_closed_without_valid_connection(values):
    with pytest.raises(ValidationError):
        public_settings(data_analysis_backend="service", conversation_data_analysis_enabled=True, **values)
