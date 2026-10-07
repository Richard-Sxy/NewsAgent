"""Scaled v2 is streamed, immutable and preserves bounded operational cases."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import heapq
import os
from pathlib import Path
from types import GeneratorType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.clients.enterprise.sql_warehouse import SqlQuery
from app.config import Settings
from app.sql_assistant import warehouse
from app.sql_assistant.scaled_profiles import (
    SCALED_PROFILE, TABLES, ScaledTableHasher, _metrics, _cached_manifest,
    iter_scaled_rows, scaled_manifest, scaled_scenarios, validate_news_count,
)
from app.sql_assistant.scenarios import load_sql_scenarios


def top_hour(hour, count):
    return heapq.nsmallest(100, ((index, _metrics(index, hour, count)) for index in range(1, count + 1)),
                          key=lambda item: (-item[1]["clicks"], item[0]))


def test_v2_contract_is_frozen_and_v1_defaults_are_unchanged():
    old = warehouse.warehouse_contract()
    new = warehouse.warehouse_contract(SCALED_PROFILE)
    assert old.version == "news-warehouse-v1"
    assert old.sha256 == "e4498800da447c72beb3be13ad375d9efa458605f8accf90af56b531c0aee3d0"
    assert warehouse.verify_schema_contract().startswith("# 本地热点新闻数仓契约 v1")
    assert warehouse.verify_schema_contract(SCALED_PROFILE).startswith("# 本地热点新闻数仓契约 v2")
    assert new.path.name == "sql-warehouse-schema-v2.md" and new.sha256 != old.sha256
    assert new.path.stat().st_mode & 0o222 == 0
    assert warehouse.get_text2sql_schema(SCALED_PROFILE).column_names() == warehouse.get_text2sql_schema().column_names()
    assert warehouse.get_text2sql_schema(SCALED_PROFILE).max_rows == 1000
    assert warehouse.demo_news_ids() == tuple(f"demo-news-{index:03d}" for index in range(1, 13))
    assert len(warehouse._fixture_rows()[0]) == 24
    with pytest.raises(ValueError, match="must be streamed"):
        warehouse._fixture_rows(SCALED_PROFILE)
    with pytest.raises(Exception):
        new.version = "unapproved"


def test_v2_changed_file_is_rejected_without_touching_frozen_files(tmp_path, monkeypatch):
    altered = tmp_path / "untrusted-schema.md"
    altered.write_bytes(warehouse.SCHEMA_PATH_V2.read_bytes() + b"\nchanged\n")
    monkeypatch.setattr(warehouse, "SCHEMA_PATH_V2", altered)
    with pytest.raises(warehouse.SqlWarehouseContractError, match="SHA-256 mismatch"):
        warehouse.verify_schema_contract(SCALED_PROFILE)
    assert warehouse.verify_schema_contract().startswith("# 本地热点新闻数仓契约 v1")


@pytest.mark.parametrize("bad", [12, 100, 0, 1201, 120000, True, "1200", None])
def test_size_whitelist_is_strict_and_generator_does_not_guess(bad):
    with pytest.raises(ValueError, match="120, 1200 or 12000"):
        validate_news_count(bad)
    with pytest.raises(ValueError):
        warehouse.demo_news_ids(SCALED_PROFILE, news_per_tenant=bad)


def settings_input():
    return dict(_env_file=None, environment="e2e", database_url="postgresql+psycopg://test:test@127.0.0.1/test",
                redis_url="redis://127.0.0.1/0", temporal_address="127.0.0.1:7233", temporal_namespace="default",
                artifact_bucket="test", model_runtime_config_path="deploy/model-runtime.local.yml",
                conversation_data_analysis_enabled=False)


@pytest.mark.parametrize("value", ["120", "1200", "12000"])
def test_approved_environment_size_selects_exact_integer_without_env_file(monkeypatch, value):
    monkeypatch.setenv("SQL_ASSISTANT_NEWS_PER_TENANT", value)
    parsed = Settings(**settings_input())
    assert parsed.sql_assistant_news_per_tenant == int(value)
    assert type(parsed.sql_assistant_news_per_tenant) is int


@pytest.mark.parametrize("value", ["01200", "+1200", "1200.0", " 1200", "1200 ", "1.2e3", "1000", "true", ""])
def test_other_environment_size_spellings_are_rejected(monkeypatch, value):
    monkeypatch.setenv("SQL_ASSISTANT_NEWS_PER_TENANT", value)
    with pytest.raises(ValidationError, match="synthetic dataset news count"):
        Settings(**settings_input())


@pytest.mark.parametrize("value", [True, False, 120.0, 1200.0, 12000.0, None])
def test_python_size_is_also_strict_not_bool_or_float(value):
    with pytest.raises(ValidationError, match="synthetic dataset news count"):
        Settings(**settings_input(), sql_assistant_news_per_tenant=value)


def test_default_1200_shape_is_real_generation_and_contains_no_raw_behavior():
    info = warehouse.demo_dataset_info(SCALED_PROFILE)
    assert info["news_per_tenant"] == info["news_count"] == 1200
    assert info["metric_row_count"] == info["baseline_row_count"] == 28800
    assert info["total_tenants"] == 2 and info["hours_per_news"] == 24
    assert info["total_news_count"] == 2400
    assert info["total_metric_row_count"] == info["total_baseline_row_count"] == 57600
    manifest = scaled_manifest()
    for table, expected_count in zip(TABLES, (2400, 57600, 57600)):
        generated = iter_scaled_rows(table)
        assert isinstance(generated, GeneratorType)
        hasher = ScaledTableHasher(table)
        previous = None
        for row in generated:
            key = (str(row["tenant_id"]), row["news_id"], row.get("event_time"))
            if previous is not None:
                assert previous < key
            previous = key
            assert not {"user_id", "device_id", "ip", "event_id"} & set(row)
            hasher.update(row)
        assert hasher.count == expected_count
        assert hasher.digest() == manifest["tables"][table]["sha256"]
    assert warehouse.demo_news_ids(SCALED_PROFILE)[0] == "scale-news-000001"
    assert warehouse.demo_news_ids(SCALED_PROFILE)[-1] == "scale-news-001200"


def test_small_cached_manifest_is_a_copy_and_does_not_cache_physical_rows():
    first = scaled_manifest(120)
    hits = _cached_manifest.cache_info().hits
    second = scaled_manifest(120)
    assert first == second and _cached_manifest.cache_info().hits > hits
    second["enterprise_scenarios"][0]["label"] = "not persisted"
    second["tables"]["dim_news"]["rows"] = 1
    assert scaled_manifest(120) == first
    assert set(first["tables"]["dim_news"]) == {"sha256", "rows"}
    assert first["dataset_sha256"] != scaled_manifest(1200)["dataset_sha256"]


@pytest.mark.parametrize("count", [120, 1200, 12000])
def test_every_operational_case_keeps_a_real_top100_intersection(count):
    for case in scaled_scenarios():
        reference_hour = int(case["reference_start"][11:13])
        current_hour = int(case["current_start"][11:13])
        reference = top_hour(reference_hour, count)
        current = top_hour(current_hour, count)
        assert len(reference) == len(current) == case["row_limit"] == 100
        assert case["question"] == "查询点击量最高的前100条新闻"
        assert {item[0] for item in reference} & {item[0] for item in current}
        if case["id"] in {"breaking", "content-mix"}:
            assert set(range(1, 41)) <= ({item[0] for item in reference} & {item[0] for item in current})
        if case["id"] == "low-volume":
            assert sum(item[1]["impressions"] == 0 for item in reference) == 50
            assert sum(item[1]["impressions"] == 0 for item in current) == 50
        if case["id"] == "ranking-churn":
            intersection = {item[0] for item in reference} & {item[0] for item in current}
            assert len(intersection) == (80 if count == 120 else 60)
            assert {item[0] for item in reference} != {item[0] for item in current}


def test_largest_size_still_preserves_counters_isolation_and_numeric_precision():
    for index in (1, 40, 50, 100, 1200, 12000):
        for hour in range(24):
            row = _metrics(index, hour, 12000)
            assert 0 <= row["unique_users"] <= row["clicks"] <= row["impressions"]
            assert 0 <= row["effective_consumptions"] <= row["clicks"]
            assert max(row.values()) * 8 <= 2**63 - 1
            assert row["total_duration_seconds"] * 8 <= Decimal("999999999999999999.99")
    for index in (1, 1200, 12000):
        previous, current = _metrics(index, 20, 12000), _metrics(index, 21, 12000)
        assert current["impressions"] > 2**53
        assert current["impressions"] - previous["impressions"] == 55_537 + index * 13
        assert current["clicks"] - previous["clicks"] == 7_919 + index * 7
    metrics = iter_scaled_rows("news_metric_hourly", 120)
    first = next(metrics)
    other = next(row for row in metrics if row["tenant_id"] == warehouse.ISOLATION_TENANT_ID)
    assert other["news_id"] == first["news_id"]
    assert other["impressions"] == first["impressions"] * 8


def test_manifest_hash_binds_row_values_order_count_and_table_domain():
    original = next(iter_scaled_rows("news_metric_hourly", 120))
    changed = {**original, "clicks": original["clicks"] + 1}
    first, second = ScaledTableHasher("news_metric_hourly"), ScaledTableHasher("news_metric_hourly")
    first.update(original)
    second.update(changed)
    assert first.digest() != second.digest()
    second = ScaledTableHasher("news_metric_hourly")
    second.update(original)
    second.update(original)
    assert first.digest() != second.digest()
    foreign = ScaledTableHasher("news_metric_baseline_hourly")
    foreign.update(original)
    assert first.digest() != foreign.digest()


def test_scaled_insert_batches_never_exceed_500_and_have_all_rows():
    lengths = [len(batch) for batch in warehouse._scaled_batches("news_metric_hourly", 120)]
    assert max(lengths) == 500 and sum(lengths) == 5760
    assert lengths[-1] == 260
    with pytest.raises(ValueError):
        list(warehouse._scaled_batches("dim_news", 120, batch_size=501))


def test_v2_scene_keeps_existing_safe_query_ceiling():
    source = Path(__file__).resolve().parents[1] / "deploy" / "text2sql-scenes.enterprise-v2.yml"
    scenes = load_sql_scenarios(source)
    assert scenes.warehouse_schema_version == warehouse.SCHEMA_VERSION_V2
    assert scenes.resolve("news-ranking").max_limit == 100
    assert scenes.resolve("news-ranking").default_limit == 100
    old = load_sql_scenarios(source.with_name("text2sql-scenes.local.yml"))
    assert old.warehouse_schema_version == warehouse.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_real_initializer_uses_bounded_batches_and_owned_data_is_never_reseeded(monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock())
    @asynccontextmanager
    async def begin():
        yield connection
    database = SimpleNamespace(engine=SimpleNamespace(begin=begin))
    monkeypatch.setattr(warehouse, "_profile_contract", AsyncMock(return_value=False))
    monkeypatch.setattr(warehouse, "_ensure_scaled_schema", AsyncMock())
    verifier = AsyncMock()
    monkeypatch.setattr(warehouse, "_verify_scaled_rows", verifier)
    result = await warehouse._initialize_scaled_warehouse(database, news_per_tenant=120)
    assert result["total_news_count"] == 240
    inserts = [call for call in connection.execute.call_args_list if str(call.args[0]).startswith("INSERT INTO dw.")]
    assert inserts and all(1 <= len(call.args[1]) <= 500 for call in inserts)
    assert sum(len(call.args[1]) for call in inserts) == 240 + 5760 * 2
    verifier.assert_awaited_once()
    connection.execute.reset_mock()
    monkeypatch.setattr(warehouse, "_profile_contract", AsyncMock(return_value=True))
    await warehouse._initialize_scaled_warehouse(database, news_per_tenant=120)
    assert not any(str(call.args[0]).startswith("INSERT INTO dw.") for call in connection.execute.call_args_list)
    assert verifier.await_count == 2


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.close = AsyncMock()
    def mappings(self):
        return self
    async def partitions(self, size):
        assert size == 500
        for offset in range(0, len(self.rows), size):
            yield self.rows[offset:offset + size]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "missing", "extra", "value"])
async def test_server_cursor_verifies_full_data_and_closes_on_drift(change):
    tables = [list(iter_scaled_rows(table, 120)) for table in TABLES]
    if change == "missing":
        tables[1].pop()
    elif change == "extra":
        tables[1].append(deepcopy(tables[1][-1]))
    elif change == "value":
        tables[1][100]["clicks"] += 1
    cursors = [Cursor(rows) for rows in tables]
    connection = SimpleNamespace(stream=AsyncMock(side_effect=cursors))
    if change is None:
        await warehouse._verify_scaled_rows(connection, scaled_manifest(120))
        assert connection.stream.await_count == 3
    else:
        with pytest.raises(warehouse.SqlWarehouseContractError, match="missing or changed"):
            await warehouse._verify_scaled_rows(connection, scaled_manifest(120))
    for cursor in cursors[:connection.stream.await_count]:
        cursor.close.assert_awaited_once()
    assert all("ORDER BY tenant_id, news_id" in str(call.args[0]) for call in connection.stream.call_args_list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_scaled_1200_counts_hash_reuse_tenants_and_rollback_drift():
    url = os.getenv("SCALE_WAREHOUSE_INTEGRATION_URL")
    if not url:
        pytest.skip("set SCALE_WAREHOUSE_INTEGRATION_URL to the new isolated v2 PostgreSQL")
    database = SimpleNamespace(engine=create_async_engine(url))
    count = validate_news_count(int(os.getenv("SCALE_WAREHOUSE_NEWS_PER_TENANT", "1200")))
    expected = scaled_manifest(count)
    try:
        first = await warehouse.initialize_demo_warehouse(database, dataset_profile=SCALED_PROFILE, news_per_tenant=count)
        second = await warehouse.initialize_demo_warehouse(database, dataset_profile=SCALED_PROFILE, news_per_tenant=count)
        assert first == second == warehouse.demo_dataset_info(SCALED_PROFILE, news_per_tenant=count)
        async with database.engine.connect() as connection:
            counts = (await connection.execute(text("""SELECT
                (SELECT COUNT(*) FROM dw.dim_news) AS news,
                (SELECT COUNT(*) FROM dw.news_metric_hourly) AS metrics,
                (SELECT COUNT(*) FROM dw.news_metric_baseline_hourly) AS baselines"""))).mappings().one()
            assert dict(counts) == {"news": count * 2, "metrics": count * 48, "baselines": count * 48}
        with pytest.raises(warehouse.SqlWarehouseContractError, match="profile/version/hash mismatch"):
            await warehouse.initialize_demo_warehouse(database, dataset_profile=SCALED_PROFILE,
                                                      news_per_tenant=120 if count != 120 else 1200)
        with pytest.raises(warehouse.SqlWarehouseContractError, match="profile/version/hash mismatch"):
            await warehouse.initialize_demo_warehouse(database)
        from tests.test_sql_assistant_warehouse import VALID_SQL
        params = {"tenant_id": str(warehouse.DEMO_TENANT_ID), "window_start": warehouse.WINDOW_START + timedelta(hours=21),
                  "window_end": warehouse.WINDOW_START + timedelta(hours=22), "row_limit": 100}
        main = warehouse.LocalPostgresSqlWarehouseClient(database, tenant_id=str(warehouse.DEMO_TENANT_ID),
                                                        dataset_profile=SCALED_PROFILE, news_per_tenant=count)
        other = warehouse.LocalPostgresSqlWarehouseClient(database, tenant_id=str(warehouse.ISOLATION_TENANT_ID),
                                                         dataset_profile=SCALED_PROFILE, news_per_tenant=count)
        result = await main.execute(SqlQuery(VALID_SQL, params, 10000, 100))
        other_query = SqlQuery(VALID_SQL, {**params, "tenant_id": str(warehouse.ISOLATION_TENANT_ID)}, 10000, 100)
        other_result = await other.execute(other_query)
        with pytest.raises(warehouse.SqlWarehouseContractError, match="authenticated tenant"):
            await main.execute(other_query)
        assert len(result.rows) == len(other_result.rows) == 100
        main_rows, other_rows = ({row["news_id"]: row for row in outcome.rows} for outcome in (result, other_result))
        assert set(main_rows) == set(other_rows)
        assert all(other_rows[identity]["impressions"] == main_rows[identity]["impressions"] * 8 for identity in main_rows)
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("""UPDATE dw.news_metric_hourly SET clicks=clicks+1
                    WHERE tenant_id=:tenant_id AND news_id='scale-news-000001' AND event_time=:event_time"""),
                    {"tenant_id": warehouse.DEMO_TENANT_ID, "event_time": params["window_start"]})
                with pytest.raises(warehouse.SqlWarehouseContractError, match="missing or changed"):
                    await warehouse._verify_scaled_rows(connection, expected)
            finally:
                await transaction.rollback()
        async with database.engine.connect() as connection:
            await warehouse._verify_scaled_rows(connection, expected)
    finally:
        await database.engine.dispose()
