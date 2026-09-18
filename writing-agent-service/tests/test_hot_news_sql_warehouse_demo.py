import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.text2sql_metric_source import Text2SqlNewsMetricSource
from app.clients.enterprise.sql_warehouse import InMemorySqlWarehouseClient
from app.clients.fastgpt import AgentResult
from app.schemas.text2sql import Text2SqlPlan
from examples.hot_news_demo import load_scenario
from examples.hot_news_sql_warehouse_demo import (
    DEMO_TABLE,
    build_demo_metric_template,
    build_demo_rows,
    build_text2sql_schema,
)


def test_demo_rows_are_aggregated_and_include_click_metrics() -> None:
    scenario = load_scenario()

    rows = build_demo_rows(scenario)

    assert len(rows) == len(scenario["news"])
    first_news = scenario["news"][0]
    first = next(row for row in rows if row["news_id"] == first_news["news_id"])
    assert first["news_title"] == first_news["title"]
    assert first["clicks"] == first_news["current"]["clicks"]
    assert first["total_duration_seconds"] == (
        first_news["current"]["duration_seconds"]
        * first_news["current"]["effective_consumptions"]
    )
    assert "user_id" not in first
    assert "event_id" not in first


def test_demo_text2sql_schema_exposes_only_the_aggregate_table() -> None:
    schema = build_text2sql_schema()

    assert schema.table_names() == frozenset({DEMO_TABLE})
    assert {
        "tenant_id",
        "event_time",
        "news_id",
        "news_title",
        "clicks",
        "impressions",
    } <= schema.column_names()
    assert "user_id" not in schema.column_names()
    assert "CREATE TABLE dw.news_behavior_aggregate" in schema.render_ddl()


def test_demo_template_queries_aggregates_through_the_shared_guard() -> None:
    async def exercise() -> None:
        window_start = datetime(2026, 9, 3, 2, tzinfo=timezone.utc)
        window_end = window_start + timedelta(hours=1)
        warehouse = InMemorySqlWarehouseClient(
            (
                {
                    "news_id": "demo-news-1",
                    "content_type": "article",
                    "impressions": 100,
                    "clicks": 40,
                    "unique_users": 40,
                    "total_duration_seconds": 900,
                    "effective_consumptions": 30,
                    "interactions": 5,
                },
            )
        )
        source = Text2SqlNewsMetricSource(
            warehouse=warehouse,
            schema=build_text2sql_schema(),
            template=build_demo_metric_template(),
        )

        batch = await source.fetch_batch(
            HotNewsMetricQuery(
                tenant_id="demo-tenant",
                window_start=window_start,
                window_end=window_end,
            )
        )

        assert batch.source == "template"
        assert "SUM(clicks) AS clicks" in batch.sql
        assert batch.snapshots[0].clicks == 40
        assert warehouse.queries[0].params["tenant_id"] == "demo-tenant"

    asyncio.run(exercise())


def test_demo_schema_runs_through_text2sql_guard_and_warehouse_port() -> None:
    async def exercise() -> None:
        window_start = datetime(2026, 9, 3, 2, tzinfo=timezone.utc)
        window_end = window_start + timedelta(hours=1)
        row = {
            "news_id": "demo-news-1",
            "content_type": "article",
            "impressions": 100,
            "clicks": 40,
            "unique_users": 40,
            "total_duration_seconds": 900,
            "effective_consumptions": 30,
            "interactions": 5,
        }
        sql = (
            "SELECT news_id, content_type, impressions, clicks, unique_users, "
            "total_duration_seconds, effective_consumptions, interactions "
            "FROM dw.news_behavior_aggregate "
            "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
            "AND event_time < :window_end LIMIT :row_limit"
        )
        generator = AsyncMock()
        generator.run.return_value = AgentResult(
            value=Text2SqlPlan(sql=sql),
            request_id="demo-text2sql-request",
            usage={},
            raw_content=sql,
        )
        warehouse = InMemorySqlWarehouseClient((row,))
        source = Text2SqlNewsMetricSource(
            warehouse=warehouse,
            schema=build_text2sql_schema(),
            template=None,
            generator=generator,
        )

        batch = await source.fetch_batch(
            HotNewsMetricQuery(
                tenant_id="demo-tenant",
                window_start=window_start,
                window_end=window_end,
            )
        )

        assert batch.source == "text2sql"
        assert batch.request_id == "demo-text2sql-request"
        assert batch.snapshots[0].clicks == 40
        assert warehouse.queries[0].params["tenant_id"] == "demo-tenant"

    asyncio.run(exercise())
