from pathlib import Path
from types import SimpleNamespace
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.clients.enterprise.sql_warehouse import SqlQuery
from app.domain.errors import Text2SqlGuardError
from app.sql_assistant import warehouse


def test_frozen_contract_exposes_one_aggregate_view() -> None:
    assert warehouse.verify_schema_contract().startswith("# 本地热点新闻数仓契约 v1（冻结）")
    schema = warehouse.get_text2sql_schema()
    assert schema.table_names() == frozenset({"dw.news_behavior_aggregate"})
    assert len(schema.tables[0].columns) == 18
    assert "user_id" not in schema.column_names()
    assert "baseline_clicks" in schema.column_names()
    assert schema.max_rows == 1000


def test_changed_contract_is_rejected_before_sql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changed = tmp_path / "sql-warehouse-schema-v1.md"
    changed.write_bytes(warehouse.SCHEMA_PATH.read_bytes() + b"\nunauthorized change\n")
    monkeypatch.setattr(warehouse, "SCHEMA_PATH", changed)
    with pytest.raises(warehouse.SqlWarehouseContractError, match="SHA-256 mismatch"):
        warehouse.verify_schema_contract()
    with pytest.raises(warehouse.SqlWarehouseContractError, match="SHA-256 mismatch"):
        warehouse.get_text2sql_schema()


def test_synthetic_fixtures_have_hour_buckets_and_tenant_isolation() -> None:
    news, metrics, baselines = warehouse._fixture_rows()
    assert len(news) == 24
    assert len(metrics) == len(baselines) == 576
    assert {(row["tenant_id"], row["news_id"]) for row in news} == {
        (tenant, f"demo-news-{index:03d}")
        for tenant in (warehouse.DEMO_TENANT_ID, warehouse.ISOLATION_TENANT_ID)
        for index in range(1, 13)
    }
    assert {row["category"] for row in news} == {"科技", "财经", "体育", "社会"}
    assert {row["content_type"] for row in news} == {"article", "video"}
    assert all(warehouse.WINDOW_START <= row["event_time"] < warehouse.WINDOW_END for row in metrics)
    assert all(row["event_time"].minute == 0 for row in metrics)
    assert all(0 <= row["effective_consumptions"] <= row["clicks"] <= row["impressions"] for row in metrics)
    assert all("user_id" not in row and "ip" not in row for row in metrics)
    assert warehouse._fixture_rows() == (news, metrics, baselines)


@pytest.mark.asyncio
async def test_initialization_is_restricted_to_e2e() -> None:
    with pytest.raises(warehouse.SqlWarehouseContractError, match="environment=e2e"):
        await warehouse.initialize_demo_warehouse(SimpleNamespace(), environment="production")


def make_query(sql: str, **params) -> SqlQuery:
    return SqlQuery(
        sql=sql,
        params={
            "tenant_id": str(warehouse.DEMO_TENANT_ID),
            "window_start": warehouse.WINDOW_START,
            "window_end": warehouse.WINDOW_END,
            "row_limit": 10,
            **params,
        },
        timeout_ms=1000,
        max_rows=10,
    )


VALID_SQL = (
    "SELECT news_id, title, content_type, category, source, "
    "SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
    "SUM(effective_consumptions) AS effective_consumptions, SUM(interactions) AS interactions, "
    "COALESCE(ROUND(SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0), 4), 0) AS ctr, "
    "COALESCE(ROUND((SUM(clicks) * 0.4 + SUM(effective_consumptions) * 0.35 "
    "+ SUM(interactions) * 0.25) / NULLIF(SUM(impressions), 0), 4), 0) AS hot_score "
    "FROM dw.news_behavior_aggregate "
    "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
    "AND event_time < :window_end "
    "GROUP BY news_id, title, content_type, category, source "
    "ORDER BY clicks DESC, news_id ASC LIMIT :row_limit"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "message"),
    [
        (make_query(VALID_SQL, tenant_id=str(warehouse.ISOLATION_TENANT_ID)), "authenticated tenant"),
        (make_query(VALID_SQL.replace("dw.news_behavior_aggregate", "public.jobs")), "non-whitelisted"),
        (make_query(VALID_SQL.replace("tenant_id = :tenant_id", "tenant_id <> :tenant_id")), "WHERE"),
        (make_query(VALID_SQL.replace("event_time < :window_end", "event_time > :window_end")), "WHERE"),
        (make_query(VALID_SQL.replace(" GROUP BY", " OR 1=1 GROUP BY")), "WHERE"),
        (make_query(VALID_SQL.replace("LIMIT :row_limit", "LIMIT 10")), "row_limit"),
        (make_query(VALID_SQL, row_limit=11), "execution budget"),
    ],
)
async def test_warehouse_rejects_unsafe_query_before_opening_connection(query: SqlQuery, message: str) -> None:
    # No engine is provided: rejection must happen before attempting a connection.
    client = warehouse.LocalPostgresSqlWarehouseClient(
        SimpleNamespace(), tenant_id=str(warehouse.DEMO_TENANT_ID)
    )
    with pytest.raises((warehouse.SqlWarehouseContractError, Text2SqlGuardError), match=message):
        await client.execute(query)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_seed_is_idempotent_and_queries_are_isolated() -> None:
    url = os.getenv("SQL_WAREHOUSE_INTEGRATION_URL")
    if not url:
        pytest.skip("set SQL_WAREHOUSE_INTEGRATION_URL to the isolated local PostgreSQL")
    database = SimpleNamespace(engine=create_async_engine(url))
    try:
        first = await warehouse.initialize_demo_warehouse(database, environment="e2e")
        second = await warehouse.initialize_demo_warehouse(database, environment="e2e")
        assert first == second == warehouse.demo_dataset_info()
        primary = warehouse.LocalPostgresSqlWarehouseClient(
            database, tenant_id=str(warehouse.DEMO_TENANT_ID)
        )
        result = await primary.execute(make_query(VALID_SQL))
        assert len(result.rows) == 10
        assert not result.truncated
        assert all(row["title"].startswith("[合成样本]") for row in result.rows)
        _, metrics, _ = warehouse._fixture_rows()
        totals: dict[str, int] = {}
        for row in metrics:
            if row["tenant_id"] == warehouse.DEMO_TENANT_ID:
                totals[row["news_id"]] = totals.get(row["news_id"], 0) + row["clicks"]
        assert all(row["clicks"] == totals[row["news_id"]] for row in result.rows)
        assert result.rows[0]["clicks"] == max(totals.values())
        assert (await primary.execute(make_query(VALID_SQL))).rows == result.rows
        isolation = warehouse.LocalPostgresSqlWarehouseClient(
            database, tenant_id=str(warehouse.ISOLATION_TENANT_ID)
        )
        other = await isolation.execute(make_query(VALID_SQL, tenant_id=str(warehouse.ISOLATION_TENANT_ID)))
        assert all(row["title"].startswith("[隔离样本]") for row in other.rows)
        assert other.rows[0]["clicks"] > result.rows[0]["clicks"]
    finally:
        await database.engine.dispose()
