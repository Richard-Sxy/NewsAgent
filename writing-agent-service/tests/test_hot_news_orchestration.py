import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from temporalio.converter import DataConverter
from sqlalchemy.dialects import postgresql

from app.analytics.baseline import NewsMetricBaseline
from app.analytics.data_source import InMemoryBehaviorDataSource
from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.analytics.news_content import NewsContent
from app.clients.fastgpt import AgentResult
from app.domain.errors import HotNewsDataQualityError
from app.schemas.hot_news import HotNewsAnalysisReport
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)
from app.services.hot_news_orchestration import (
    EmptyHotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
    HotNewsRunRequest,
)
from app.services.hot_news_run_store import PostgresHotNewsRunStore


START = datetime(2026, 9, 4, 10, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def record(
    event_id: str,
    event_type: EventType,
    *,
    news_id: str = "news-1",
    duration_seconds: int = 0,
) -> BehaviorRecord:
    return BehaviorRecord(
        event_id=event_id,
        user_id="private-user",
        news_id=news_id,
        event_type=event_type,
        event_time=START,
        content_type=ContentType.ARTICLE,
        duration_seconds=duration_seconds,
    )


def request() -> HotNewsRunRequest:
    return HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=START,
        window_end=END,
        production_bundle_version="bundle-v1",
    )


class RecordingEnrichmentService:
    def __init__(self, *, missing_content: bool = False) -> None:
        self.missing_content = missing_content
        self.calls = []

    async def enrich(
        self,
        ranked,
        *,
        related_limit: int,
        candidate_limit: int,
    ):
        self.calls.append((ranked, related_limit, candidate_limit))
        return [
            EnrichedHotNews(
                ranking=item,
                content=(
                    None
                    if self.missing_content
                    else NewsContent(
                        news_id=item.current.news_id,
                        title=f"热点 {item.current.news_id}",
                        summary="可信新闻摘要",
                        body="可信新闻正文",
                        content_type=item.current.content_type,
                        publish_time=START - timedelta(hours=1),
                        source_url=f"https://news.example.com/{item.current.news_id}",
                    )
                ),
                related_news=(),
            )
            for item in ranked
        ]


class RecordingRunner:
    def __init__(self) -> None:
        self.inputs = []

    async def run(self, analysis_input):
        self.inputs.append(analysis_input)
        report = HotNewsAnalysisReport(
            news_id=analysis_input.news_id,
            trend_assessment="当前窗口内形成热点。",
            dominant_driver="click",
            limitations=["当前没有关联新闻证据。"],
            overall_confidence=0.7,
        )
        return AgentResult(
            value=report,
            request_id=f"request-{analysis_input.news_id}",
            usage={"total_tokens": 10},
            raw_content=report.model_dump_json(),
        )


def orchestration(
    records: list[BehaviorRecord],
    *,
    enrichment: RecordingEnrichmentService | None = None,
    baseline_provider=None,
):
    resolved_enrichment = enrichment or RecordingEnrichmentService()
    runner = RecordingRunner()
    analysis = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder(),
        runner=runner,
        validator=HotNewsAnalysisValidator(),
    )
    service = HotNewsOrchestrationService(
        behavior_data_source=InMemoryBehaviorDataSource(records),
        baseline_provider=(
            baseline_provider or EmptyHotNewsBaselineProvider()
        ),
        enrichment_service=resolved_enrichment,
        analysis_service=analysis,
        policy=HotNewsOrchestrationPolicy(
            production_bundle_version="bundle-v1",
            ranking_limit=10,
            related_limit=2,
            candidate_limit=5,
        ),
    )
    return service, resolved_enrichment, runner


@pytest.mark.asyncio
async def test_runs_bounded_hot_news_chain_without_retaining_raw_user_data() -> None:
    records = [
        record("impression-1", EventType.IMPRESSION),
        record("click-1", EventType.CLICK),
        record("read-1", EventType.READ, duration_seconds=30),
    ]
    service, enrichment, runner = orchestration(records)

    result = await service.run(request())

    assert result.idempotency_key == request().idempotency_key
    assert result.fetched_record_count == 3
    assert result.metric_snapshots[0].clicks == 1
    assert result.ranked_news[0].current.news_id == "news-1"
    assert result.analyzed_news[0].analysis.value.news_id == "news-1"
    snapshot = result.analyzed_news[0]
    assert snapshot.analysis_input == runner.inputs[0]
    assert snapshot.analysis_input is not runner.inputs[0]
    assert snapshot.captured_at <= snapshot.validated_at
    assert runner.inputs[0].metrics.effective_consumptions == 1
    assert enrichment.calls[0][1:] == (2, 5)
    assert "private-user" not in repr(result)


@pytest.mark.asyncio
async def test_persistence_writes_snapshot_v2_and_returns_only_summary() -> None:
    service, _, runner = orchestration(
        [record("impression-1", EventType.IMPRESSION)]
    )
    result = await service.run(request())
    run_id = uuid4()
    session = SimpleNamespace(
        execute=AsyncMock(return_value=Mock(scalar_one_or_none=lambda: run_id))
    )

    @asynccontextmanager
    async def open_session():
        yield session

    store = PostgresHotNewsRunStore(SimpleNamespace(session=open_session))
    outcome = await store.save_completed(result=result)

    statement = session.execute.await_args.args[0]
    values = statement.compile(dialect=postgresql.dialect()).params
    assert values["payload_schema_version"] == "2.0"
    payload = values["result_payload"]
    saved = payload["analyzed_news"][0]
    assert saved["analysis_input"] == runner.inputs[0].model_dump(mode="json")
    assert saved["analysis"]["value"]["news_id"] == "news-1"
    assert saved["analysis"]["request_id"] == "request-news-1"
    assert saved["captured_at"] == result.analyzed_news[0].captured_at.isoformat()
    assert saved["validated_at"] == result.analyzed_news[0].validated_at.isoformat()
    assert payload["request"]["production_bundle_version"] == "bundle-v1"
    assert "private-user" not in json.dumps(payload)
    assert outcome.run_id == str(run_id)
    assert "analysis_input" not in asdict(outcome)
    assert "result_payload" not in asdict(outcome)


@pytest.mark.asyncio
async def test_empty_window_returns_without_enrichment_or_model_call() -> None:
    service, enrichment, runner = orchestration([])

    result = await service.run(request())

    assert result.metric_snapshots == ()
    assert result.ranked_news == ()
    assert result.analyzed_news == ()
    assert enrichment.calls == []
    assert runner.inputs == []


@pytest.mark.asyncio
async def test_missing_ranked_content_is_a_hard_data_quality_failure() -> None:
    enrichment = RecordingEnrichmentService(missing_content=True)
    service, _, runner = orchestration(
        [record("impression-1", EventType.IMPRESSION)],
        enrichment=enrichment,
    )

    with pytest.raises(HotNewsDataQualityError, match="content is missing"):
        await service.run(request())

    assert runner.inputs == []


@pytest.mark.asyncio
async def test_rejects_baseline_whose_key_does_not_match_identity() -> None:
    class InvalidBaselineProvider:
        def get_baselines(self, **kwargs):
            baseline = NewsMetricBaseline(
                news_id="news-1",
                content_type=ContentType.ARTICLE,
                sample_count=7,
                impressions=100,
                clicks=10,
                unique_users=10,
                total_duration_seconds=100,
                effective_consumptions=5,
                interactions=1,
                ctr=0.1,
            )
            return {("other-news", ContentType.ARTICLE): baseline}

    service, _, _ = orchestration(
        [record("impression-1", EventType.IMPRESSION)],
        baseline_provider=InvalidBaselineProvider(),
    )

    with pytest.raises(HotNewsDataQualityError, match="baseline key"):
        await service.run(request())


def test_run_request_has_stable_schedule_idempotency_key() -> None:
    first = request()
    second = request()

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key.startswith("hot-news-")
    assert len(first.idempotency_key) == len("hot-news-") + 64


def test_equivalent_utc_window_has_same_schedule_idempotency_key() -> None:
    utc_request = request()
    china_timezone = timezone(timedelta(hours=8))
    china_request = HotNewsRunRequest(
        tenant_id=utc_request.tenant_id,
        window_start=utc_request.window_start.astimezone(china_timezone),
        window_end=utc_request.window_end.astimezone(china_timezone),
        production_bundle_version=utc_request.production_bundle_version,
    )

    assert china_request.idempotency_key == utc_request.idempotency_key


@pytest.mark.asyncio
async def test_run_request_round_trips_through_temporal_data_converter() -> None:
    original = request()

    payloads = await DataConverter.default.encode([original])
    decoded = await DataConverter.default.decode(payloads, [HotNewsRunRequest])

    assert decoded == [original]


@pytest.mark.asyncio
async def test_rejects_request_when_loaded_production_bundle_is_different() -> None:
    service, _, runner = orchestration(
        [record("impression-1", EventType.IMPRESSION)]
    )
    mismatched = HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=START,
        window_end=END,
        production_bundle_version="bundle-v2",
    )

    with pytest.raises(HotNewsDataQualityError, match="production bundle"):
        await service.run(mismatched)

    assert runner.inputs == []


def test_run_request_rejects_invalid_schedule_window() -> None:
    invalid = HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=END,
        window_end=START,
        production_bundle_version="bundle-v1",
    )

    with pytest.raises(ValueError, match="window_start"):
        invalid.validate()
