from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.clients.fastgpt import AgentResult
from app.domain.errors import HotNewsAnalysisAttemptError
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
)
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.services.data_loop.automatic_feedback import (
    AutomaticHotNewsFeedbackSink,
)
from app.services.hot_news_orchestration import (
    AnalyzedHotNews,
    HotNewsRunRequest,
    HotNewsRunResult,
)


NOW = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeDatabase:
    def __init__(self) -> None:
        self.session_value = object()

    @asynccontextmanager
    async def session(self):
        yield self.session_value


def analysis_input(*, hot_score: float = 0.8) -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id="news-1",
        title="测试新闻",
        content_type="article",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        hot_score=hot_score,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=20,
            ctr=0.2,
            unique_users=18,
            effective_consumptions=12,
            interactions=4,
        ),
        score_components=HotScoreComponents(
            click=0.8,
            consumption=0.6,
            interaction=0.3,
            growth=0.9,
        ),
        related_news=[],
        analysis_policy_version="analysis-v1",
    )


def report(*, confidence: float = 0.55) -> HotNewsAnalysisReport:
    return HotNewsAnalysisReport(
        news_id="news-1",
        trend_assessment="热度上升",
        dominant_driver="growth",
        applied_memory_ids=[],
        limitations=["无关联新闻证据"],
        overall_confidence=confidence,
    )


def run_result() -> HotNewsRunResult:
    request = HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        production_bundle_version="bundle-v1",
    )
    analyzed = AnalyzedHotNews(
        news_id="news-1",
        rank=1,
        analysis=AgentResult(
            value=report(),
            request_id="request-1",
            usage={"total_tokens": 10},
            raw_content="sensitive-model-output",
        ),
        analysis_input=analysis_input(),
        captured_at=NOW,
        validated_at=NOW,
    )
    return HotNewsRunResult(
        request=request,
        idempotency_key=request.idempotency_key,
        fetched_record_count=1,
        metric_snapshots=(),
        baselines=(),
        ranked_news=(),
        analyzed_news=(analyzed,),
    )


@pytest.mark.asyncio
async def test_completed_run_collects_low_confidence_and_retrieval_gap() -> None:
    collector = SimpleNamespace(collect_from_analysis_memory=AsyncMock())
    sink = AutomaticHotNewsFeedbackSink(
        database=FakeDatabase(),
        run_store=SimpleNamespace(),
        collector=collector,
    )

    await sink.collect_completed_run(result=run_result(), run_id=str(RUN_ID))

    assert collector.collect_from_analysis_memory.await_count == 2
    calls = collector.collect_from_analysis_memory.await_args_list
    assert {call.kwargs["source_type"] for call in calls} == {
        "low_confidence",
        "retrieval_error",
    }
    assert all(call.kwargs["memory"].run_id == RUN_ID for call in calls)
    assert all("sensitive-model-output" not in str(call) for call in calls)


@pytest.mark.asyncio
async def test_analysis_failure_keeps_snapshot_but_discards_raw_output() -> None:
    collector = SimpleNamespace(collect=AsyncMock())
    sink = AutomaticHotNewsFeedbackSink(
        database=FakeDatabase(),
        run_store=SimpleNamespace(),
        collector=collector,
    )
    request = run_result().request
    failure = HotNewsAnalysisAttemptError(
        "bad output containing should-not-be-stored",
        analysis_input=analysis_input(),
        raw_content="raw-secret-output",
        request_id="request-1",
    )

    await sink.collect_analysis_failure(request=request, error=failure)

    command = collector.collect.await_args.kwargs["command"]
    assert command.analysis_input_snapshot.news_id == "news-1"
    assert command.analysis_output_snapshot is None
    serialized = command.model_dump_json()
    assert "raw-secret-output" not in serialized
    assert "should-not-be-stored" not in serialized


@pytest.mark.asyncio
async def test_replay_repairs_automatic_cases_from_persisted_memories() -> None:
    memory = HotNewsAnalysisMemory(
        run_id=RUN_ID,
        tenant_id="tenant-1",
        news_id="news-1",
        rank=1,
        production_bundle_version="bundle-v1",
        workflow_version="workflow-v1",
        payload_schema_version="2.0",
        analysis_input=analysis_input(),
        analysis_report=report(),
        usage={},
        captured_at=NOW,
        validated_at=NOW,
        completed_at=NOW,
    )
    run_store = SimpleNamespace(
        list_analysis_memories=AsyncMock(return_value=(memory,))
    )
    collector = SimpleNamespace(collect_from_analysis_memory=AsyncMock())
    sink = AutomaticHotNewsFeedbackSink(
        database=FakeDatabase(),
        run_store=run_store,
        collector=collector,
    )

    await sink.collect_persisted_run(
        tenant_id="tenant-1",
        idempotency_key="hot-news-run-1",
        run_id=str(RUN_ID),
    )

    run_store.list_analysis_memories.assert_awaited_once()
    assert collector.collect_from_analysis_memory.await_count == 2
