"""模板优先 + Text2SQL 兜底取数链路的单元与集成测试。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.news_content import NewsContent
from app.analytics.text2sql_metric_source import (
    Text2SqlNewsMetricSource,
    aggregate_metric_template,
)
from app.clients.enterprise.sql_warehouse import (
    InMemorySqlWarehouseClient,
    SqlQuery,
)
from app.clients.fastgpt import AgentResult
from app.domain.errors import HotNewsDataQualityError, Text2SqlGuardError
from app.schemas.hot_news import HotNewsAnalysisReport
from app.schemas.text2sql import (
    Text2SqlColumn,
    Text2SqlPlan,
    Text2SqlSchema,
    Text2SqlTable,
)
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


START = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
TABLE = "dw.news_behavior_aggregate"
METRIC_COLUMNS = (
    "impressions",
    "clicks",
    "unique_users",
    "total_duration_seconds",
    "effective_consumptions",
    "interactions",
)
ALL_COLUMNS = (
    "news_id",
    "content_type",
    "tenant_id",
    "event_time",
) + METRIC_COLUMNS


def schema() -> Text2SqlSchema:
    return Text2SqlSchema(
        dialect="postgres",
        tables=(
            Text2SqlTable(
                name=TABLE,
                description="窗口行为聚合视图",
                columns=tuple(
                    Text2SqlColumn(name=name, data_type="bigint")
                    for name in ALL_COLUMNS
                ),
            ),
        ),
        tenant_column="tenant_id",
        window_column="event_time",
        max_rows=1000,
    )


def template():
    return aggregate_metric_template(table=TABLE, window_column="event_time")


def query(**overrides) -> HotNewsMetricQuery:
    values = {
        "tenant_id": "tenant-1",
        "window_start": START,
        "window_end": END,
        "ranking_limit": 20,
    }
    values.update(overrides)
    return HotNewsMetricQuery(**values)


def row(
    news_id: str = "news-1",
    *,
    content_type: str = "article",
    impressions: int = 100,
    clicks: int = 40,
    unique_users: int = 60,
    total_duration_seconds: int = 900,
    effective_consumptions: int = 30,
    interactions: int = 6,
) -> dict:
    return {
        "news_id": news_id,
        "content_type": content_type,
        "impressions": impressions,
        "clicks": clicks,
        "unique_users": unique_users,
        "total_duration_seconds": total_duration_seconds,
        "effective_consumptions": effective_consumptions,
        "interactions": interactions,
    }


@pytest.mark.asyncio
async def test_template_path_maps_rows_and_binds_parameters() -> None:
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema(),
        template=template(),
    )

    batch = await source.fetch_batch(query())

    assert batch.source == "template"
    assert batch.request_id is None
    assert batch.sql_hash == batch.sql_hash.lower()
    snapshot = batch.snapshots[0]
    assert snapshot.news_id == "news-1"
    assert snapshot.content_type == ContentType.ARTICLE
    assert snapshot.clicks == 40
    assert snapshot.ctr == Decimal("0.4")
    assert snapshot.window_start == START
    sent = warehouse.queries[0]
    assert sent.params["tenant_id"] == "tenant-1"
    assert sent.params["window_start"] == START
    assert sent.params["row_limit"] == 1000
    assert isinstance(sent, SqlQuery)


@pytest.mark.asyncio
async def test_template_path_adds_content_type_filter_for_single_type() -> None:
    warehouse = InMemorySqlWarehouseClient((row(content_type="video"),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema(),
        template=template(),
    )

    await source.fetch_batch(
        query(content_types=frozenset({ContentType.VIDEO}))
    )

    sent = warehouse.queries[0]
    assert sent.params["content_type"] == "video"
    assert "content_type = :content_type" in sent.sql


@pytest.mark.asyncio
async def test_text2sql_fallback_uses_model_plan_when_template_cannot_cover() -> None:
    generated = (
        "SELECT news_id, content_type, impressions, clicks, unique_users, "
        "total_duration_seconds, effective_consumptions, interactions, "
        "(clicks * 2) AS growth "
        "FROM dw.news_behavior_aggregate "
        "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
        "AND event_time < :window_end LIMIT :row_limit"
    )
    generator = AsyncMock()
    generator.run.return_value = AgentResult(
        Text2SqlPlan(
            sql=generated,
            explanation="补充增长指标",
            referenced_tables=[TABLE],
        ),
        "req-sql",
        {"total_tokens": 42},
        "raw",
    )
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema(),
        template=template(),
        generator=generator,
    )

    batch = await source.fetch_batch(
        query(requested_metrics=frozenset({"growth"}))
    )

    assert batch.source == "text2sql"
    assert batch.request_id == "req-sql"
    assert batch.sql == generated
    sent_input = generator.run.await_args.args[0]
    assert "growth" in sent_input.metric_columns
    assert sent_input.required_placeholders == (
        "tenant_id",
        "window_start",
        "window_end",
    )
    assert "CREATE TABLE dw.news_behavior_aggregate" in sent_input.schema_ddl


@pytest.mark.asyncio
async def test_text2sql_fallback_rejects_sql_failing_guard() -> None:
    generator = AsyncMock()
    generator.run.return_value = AgentResult(
        Text2SqlPlan(
            sql=(
                "SELECT news_id FROM dw.news_behavior_aggregate "
                "LIMIT :row_limit"
            ),
            explanation="缺少租户过滤",
            referenced_tables=[TABLE],
        ),
        "req-unsafe",
        {},
        "raw",
    )
    source = Text2SqlNewsMetricSource(
        warehouse=InMemorySqlWarehouseClient((row(),)),
        schema=schema(),
        generator=generator,
    )

    with pytest.raises(Text2SqlGuardError, match="missing required parameters"):
        await source.fetch_batch(query())


@pytest.mark.asyncio
async def test_raises_when_template_cannot_cover_and_no_generator() -> None:
    source = Text2SqlNewsMetricSource(
        warehouse=InMemorySqlWarehouseClient((row(),)),
        schema=schema(),
        template=template(),
    )

    with pytest.raises(HotNewsDataQualityError, match="no deterministic template"):
        await source.fetch_batch(query(requested_metrics=frozenset({"growth"})))


@pytest.mark.asyncio
async def test_rejects_truncated_result() -> None:
    warehouse = InMemorySqlWarehouseClient((row("news-1"), row("news-2")))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema(),
        template=template(),
        max_rows=1,
    )

    with pytest.raises(HotNewsDataQualityError, match="row limit"):
        await source.fetch_batch(query())


@pytest.mark.asyncio
async def test_rejects_malformed_row() -> None:
    broken = row()
    broken["clicks"] = -1
    warehouse = InMemorySqlWarehouseClient((broken,))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema(),
        template=template(),
    )

    with pytest.raises(HotNewsDataQualityError, match="invalid"):
        await source.fetch_batch(query())


@pytest.mark.asyncio
async def test_snapshots_feed_existing_orchestration_unchanged() -> None:
    warehouse = InMemorySqlWarehouseClient((row(),))

    class RecordingEnrichment:
        async def enrich(self, ranked, *, tenant_id, related_limit, candidate_limit):
            return [
                EnrichedHotNews(
                    ranking=item,
                    content=NewsContent(
                        news_id=item.current.news_id,
                        title="热点新闻",
                        summary="摘要",
                        body="正文",
                        content_type=item.current.content_type,
                        publish_time=START - timedelta(hours=1),
                        source_url="https://news.example.com/news-1",
                    ),
                    related_news=(),
                )
                for item in ranked
            ]

    class RecordingRunner:
        async def run(self, analysis_input):
            report = HotNewsAnalysisReport(
                news_id=analysis_input.news_id,
                trend_assessment="形成热点。",
                dominant_driver="click",
                applied_memory_ids=[],
                limitations=["没有关联新闻证据。"],
                overall_confidence=0.6,
            )
            return AgentResult(report, "req-analysis", {}, report.model_dump_json())

    service = HotNewsOrchestrationService(
        metric_source=Text2SqlNewsMetricSource(
            warehouse=warehouse,
            schema=schema(),
            template=template(),
        ),
        baseline_provider=EmptyHotNewsBaselineProvider(),
        enrichment_service=RecordingEnrichment(),
        analysis_service=HotNewsAnalysisService(
            input_builder=HotNewsAnalysisInputBuilder(),
            runner=RecordingRunner(),
            validator=HotNewsAnalysisValidator(),
        ),
        policy=HotNewsOrchestrationPolicy(
            production_bundle_version="bundle-v1",
            ranking_limit=10,
        ),
    )

    result = await service.run(
        HotNewsRunRequest(
            tenant_id="tenant-1",
            window_start=START,
            window_end=END,
            production_bundle_version="bundle-v1",
        )
    )

    assert result.fetched_record_count == 1
    assert result.ranked_news[0].current.news_id == "news-1"
    assert result.analyzed_news[0].analysis.value.news_id == "news-1"


def test_orchestration_requires_exactly_one_metric_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        HotNewsOrchestrationService(
            baseline_provider=EmptyHotNewsBaselineProvider(),
            enrichment_service=object(),
            analysis_service=object(),
            policy=HotNewsOrchestrationPolicy(production_bundle_version="v1"),
        )


def test_bootstrap_factory_enables_generator_when_app_configured() -> None:
    import app.hot_news_bootstrap as bootstrap

    source = bootstrap.create_text2sql_metric_source(
        SimpleNamespace(
            fastgpt_text2sql_app_id="text2sql-app",
            text2sql_max_rows=500,
            text2sql_timeout_ms=1234,
        ),
        warehouse=InMemorySqlWarehouseClient((row(),)),
        schema=schema(),
        template=template(),
        fastgpt_client=object(),
    )

    assert isinstance(source, Text2SqlNewsMetricSource)
    assert source.generator is not None
    assert source.max_rows == 500
    assert source.timeout_ms == 1234


def test_bootstrap_factory_without_app_keeps_template_only() -> None:
    import app.hot_news_bootstrap as bootstrap

    source = bootstrap.create_text2sql_metric_source(
        SimpleNamespace(
            fastgpt_text2sql_app_id=None,
            text2sql_max_rows=1000,
            text2sql_timeout_ms=30000,
        ),
        warehouse=InMemorySqlWarehouseClient((row(),)),
        schema=schema(),
        template=template(),
    )

    assert source.generator is None
