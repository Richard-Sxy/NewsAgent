"""Enterprise situations preserve v1 and cannot overwrite or silently repair data."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.clients.enterprise.sql_warehouse import SqlQuery
from app.data_analysis.engine import analyze
from app.sql_assistant import warehouse
from app.sql_assistant.service import SqlAssistantService
from app.sql_assistant.synthetic_profiles import (
    CLASSIC_PROFILE, ENTERPRISE_PROFILE, ENTERPRISE_DATASET_VERSION,
    enterprise_fixture_rows, enterprise_manifest, enterprise_scenarios,
    fixture_fingerprint, validate_profile,
)


def hourly_rows(hour: int, tenant=warehouse.DEMO_TENANT_ID):
    return [row for row in warehouse._fixture_rows(ENTERPRISE_PROFILE)[1]
            if row["tenant_id"] == tenant and row["event_time"].hour == hour]


def sum_metric(hour, metric):
    return sum(row[metric] for row in hourly_rows(hour))


def weighted_ctr(hour):
    return Decimal(sum_metric(hour, "clicks")) / Decimal(sum_metric(hour, "impressions"))


def analysis_request(case_id, *, operation="trend", metric="clicks"):
    case = next(item for item in enterprise_scenarios() if item["id"] == case_id)
    news, metrics, baselines = warehouse._fixture_rows(ENTERPRISE_PROFILE)
    types = {row["news_id"]: row["content_type"] for row in news if row["tenant_id"] == warehouse.DEMO_TENANT_ID}
    baseline_by_key = {(row["news_id"], row["event_time"]): row for row in baselines
                       if row["tenant_id"] == warehouse.DEMO_TENANT_ID}

    def dataset(start, identity):
        start = datetime.fromisoformat(start)
        candidates = sorted((row for row in metrics if row["tenant_id"] == warehouse.DEMO_TENANT_ID
                             and row["event_time"] == start), key=lambda row: (-row["clicks"], row["news_id"]))[:case["row_limit"]]
        rows = []
        for rank, candidate in enumerate(candidates, 1):
            row = {key: candidate[key] for key in ("news_id", "impressions", "clicks", "unique_users",
                                                   "effective_consumptions", "interactions")}
            row.update(rank=rank, content_type=types[row["news_id"]],
                       total_duration_seconds=int(candidate["total_duration_seconds"]), hot_score="0.5",
                       ctr=str(Decimal(row["clicks"]) / row["impressions"] if row["impressions"] else 0))
            if operation == "baseline":
                stored = baseline_by_key[(row["news_id"], start)]
                baseline = {key: str(stored[f"baseline_{key}"]) for key in
                            ("impressions", "clicks", "effective_consumptions", "interactions")}
                baseline.update(sample_count=1, reference_version="synthetic-hourly-baseline-v1",
                                total_duration_seconds=None,
                                ctr=str(Decimal(baseline["clicks"]) / Decimal(baseline["impressions"]))
                                if Decimal(baseline["impressions"]) else "0")
                row["baseline"] = baseline
            rows.append(row)
        result = {"run_id": identity, "window_start": start.isoformat(),
                  "window_end": (start + timedelta(hours=1)).isoformat(),
                  "bundle_version": "profile-contract-test-v1", "rows": rows}
        if operation in {"trend", "baseline"}:
            result["provenance"] = {"workflow_version": "fixture-contract-test-v1", "selection_scope_sha256": "a" * 64}
        return result

    result = {"schema_version": "2.0" if operation in {"trend", "baseline"} else "1.0",
              "operation": operation, "metric": metric,
              "dataset": dataset(case["current_start"], "11111111-1111-4111-8111-111111111111")}
    if operation == "trend":
        result["reference_dataset"] = dataset(case["reference_start"], "22222222-2222-4222-8222-222222222222")
    return result


def test_catalog_is_bounded_fixed_and_has_the_right_window_scope():
    cases = enterprise_scenarios()
    assert [item["id"] for item in cases] == [
        "steady", "breaking", "fatigue", "funnel", "content-mix", "low-volume",
        "zero-baseline", "ranking-churn", "recovery-gap", "precision",
    ]
    for case in cases:
        start = datetime.fromisoformat(case["reference_start"])
        current = datetime.fromisoformat(case["current_start"])
        end = datetime.fromisoformat(case["current_end"])
        assert warehouse.WINDOW_START <= start < current < end <= warehouse.WINDOW_END
        assert end - current == timedelta(hours=1)
        assert case["row_limit"] == (5 if case["id"] == "ranking-churn" else 12)
        assert f"前{case['row_limit']}条" in case["question"]
        assert all(type(signal) is str for signal in case["expected_signals"])
    cases[0]["expected_signals"].append("not-persisted")
    assert "not-persisted" not in enterprise_scenarios()[0]["expected_signals"]


def test_enterprise_preserves_shape_defaults_and_all_v1_relationships():
    classic = warehouse._fixture_rows()
    before = deepcopy(classic)
    enterprise = enterprise_fixture_rows(classic)
    assert classic == before == warehouse._fixture_rows(CLASSIC_PROFILE)
    assert warehouse._fixture_rows(ENTERPRISE_PROFILE) == enterprise
    assert [len(table) for table in enterprise] == [24, 576, 576]
    assert enterprise[0] == classic[0]
    assert {(row["tenant_id"], row["news_id"], row["event_time"]) for row in enterprise[1]} == {
        (row["tenant_id"], row["news_id"], row["event_time"]) for row in classic[1]
    }
    for row in enterprise[1]:
        assert 0 <= row["unique_users"] <= row["clicks"] <= row["impressions"] <= 2**63 - 1
        assert 0 <= row["effective_consumptions"] <= row["clicks"]
        assert 0 <= row["interactions"] <= 2**63 - 1
        assert 0 <= row["total_duration_seconds"] <= Decimal("999999999999999999.99")
        assert not {"user_id", "device_id", "ip", "event_id"} & set(row)
    assert [row for row in enterprise[1] if row["event_time"].hour in {22, 23}] == [
        row for row in classic[1] if row["event_time"].hour in {22, 23}
    ]
    assert [row for row in enterprise[2] if row["event_time"].hour in {22, 23}] == [
        row for row in classic[2] if row["event_time"].hour in {22, 23}
    ]


def test_isolation_tenant_keeps_same_ids_with_visible_eightfold_canary():
    for hour in (0, 3, 7, 11, 13, 15, 18, 21):
        main = {row["news_id"]: row for row in hourly_rows(hour)}
        other = {row["news_id"]: row for row in hourly_rows(hour, warehouse.ISOLATION_TENANT_ID)}
        assert set(main) == set(other)
        for news_id in main:
            for metric in ("impressions", "clicks", "unique_users", "total_duration_seconds",
                           "effective_consumptions", "interactions"):
                assert other[news_id][metric] == main[news_id][metric] * 8


def test_operational_situations_have_visible_distinct_signals():
    assert Decimal("1.009") < Decimal(sum_metric(1, "clicks")) / sum_metric(0, "clicks") <= Decimal("1.01")
    assert sum_metric(3, "clicks") > sum_metric(2, "clicks") * 2
    assert sum_metric(5, "clicks") == sum_metric(4, "clicks") // 4
    assert sum_metric(7, "impressions") == sum_metric(6, "impressions") * 2
    assert weighted_ctr(7) < weighted_ctr(6)
    assert (Decimal(sum_metric(7, "effective_consumptions")) / sum_metric(7, "clicks")
            < Decimal(sum_metric(6, "effective_consumptions")) / sum_metric(6, "clicks"))
    assert weighted_ctr(9) > weighted_ctr(8)
    reference = {row["news_id"]: row for row in hourly_rows(8)}
    for row in hourly_rows(9):
        previous = reference[row["news_id"]]
        assert Decimal(row["clicks"]) / row["impressions"] == Decimal(previous["clicks"]) / previous["impressions"]
    quality = analyze(analysis_request("low-volume", operation="quality"))
    assert quality["analysis"]["zero_impression_news_ids"] == ["demo-news-004", "demo-news-008", "demo-news-012"]
    assert any(row["clicks"] == row["impressions"] > 0 for row in hourly_rows(11))


def test_zero_baseline_is_known_zero_and_relative_changes_are_unavailable():
    result = analyze(analysis_request("zero-baseline", operation="baseline"))
    assert result["analysis"]["missing_baseline_news_ids"] == []
    assert result["analysis"]["missing_metric_news_ids"] == []
    for item in result["analysis"]["items"]:
        assert item["status"] == "compared"
        assert item["reference_value"] == "0"
        assert Decimal(item["absolute_change"]) > 0
        assert item["relative_change"] is None


def test_churn_reports_real_candidate_intersection_and_gap_is_explicit():
    churn = analyze(analysis_request("ranking-churn"))["analysis"]
    assert churn["matched_news_count"] == 2
    assert set(churn["added_news_ids"]) == {"demo-news-006", "demo-news-007", "demo-news-008"}
    assert set(churn["removed_news_ids"]) == {"demo-news-003", "demo-news-004", "demo-news-005"}
    assert {item["news_id"] for item in churn["items"]} == {"demo-news-001", "demo-news-002"}
    gap = analyze(analysis_request("recovery-gap"))["analysis"]
    assert gap["gap_seconds"] == 3600
    assert Decimal(gap["aggregate"]["relative_change"]) == 3


def test_large_integer_results_retain_exact_differences_and_no_float():
    assert all(row["impressions"] > 2**53 - 1 for row in hourly_rows(21))
    result = analyze(analysis_request("precision", metric="impressions"))["analysis"]
    for item in result["items"]:
        index = int(item["news_id"].rsplit("-", 1)[1])
        assert item["absolute_change"] == str(55_537 + index * 13)
    assert result["aggregate"]["absolute_change"] == str(sum(55_537 + index * 13 for index in range(1, 13)))


def test_full_fixture_and_catalog_identity_are_deterministic_and_detect_drift():
    rows = warehouse._fixture_rows(ENTERPRISE_PROFILE)
    manifest = enterprise_manifest(rows)
    assert manifest == enterprise_manifest(warehouse._fixture_rows(ENTERPRISE_PROFILE))
    assert manifest["dataset_version"] == ENTERPRISE_DATASET_VERSION
    assert len(manifest["dataset_sha256"]) == 64
    reordered = tuple(list(reversed(table)) for table in deepcopy(rows))
    assert fixture_fingerprint(rows) == fixture_fingerprint(reordered)
    reordered[1][0]["event_time"] = reordered[1][0]["event_time"].astimezone(timezone.utc)
    assert fixture_fingerprint(rows) == fixture_fingerprint(reordered)
    reordered[1][0]["clicks"] += 1
    assert fixture_fingerprint(rows) != fixture_fingerprint(reordered)
    assert enterprise_manifest(rows)["dataset_sha256"] != enterprise_manifest(reordered)["dataset_sha256"]


@pytest.mark.parametrize("value", ["enterprise", "enterprise-v3", "", None, True, 1])
def test_unapproved_profile_is_rejected(value):
    with pytest.raises(ValueError, match="unsupported synthetic dataset profile"):
        validate_profile(value)
    with pytest.raises(ValueError):
        warehouse._fixture_rows(value)


def test_config_only_exposes_enterprise_catalog_when_explicitly_selected(monkeypatch):
    kwargs = dict(agent=SimpleNamespace(), store=SimpleNamespace(), warehouse_factory=lambda _tenant: None,
                  scenarios_path="deploy/text2sql-scenes.local.yml", model_provider="local")
    classic = SqlAssistantService(**kwargs).config()["dataset"]
    assert classic["dataset_profile"] == CLASSIC_PROFILE
    assert "enterprise_scenarios" not in classic
    enterprise = SqlAssistantService(**kwargs, dataset_profile=ENTERPRISE_PROFILE).config()["dataset"]
    assert enterprise["dataset_profile"] == ENTERPRISE_PROFILE
    assert enterprise["enterprise_scenarios"] == enterprise_scenarios()
    assert enterprise["news_count"] == 12 and enterprise["metric_row_count"] == 288
    settings = dict(_env_file=None, environment="e2e", database_url="postgresql+asyncpg://test:test@127.0.0.1/test",
                    redis_url="redis://127.0.0.1/0", temporal_address="127.0.0.1:7233", temporal_namespace="default",
                    artifact_bucket="test", model_runtime_config_path="deploy/model-runtime.local.yml")
    monkeypatch.delenv("SQL_ASSISTANT_DATASET_PROFILE", raising=False)
    assert Settings(**settings).sql_assistant_dataset_profile == CLASSIC_PROFILE
    assert Settings(**settings, sql_assistant_dataset_profile=ENTERPRISE_PROFILE).sql_assistant_dataset_profile == ENTERPRISE_PROFILE
    with pytest.raises(ValidationError):
        Settings(**settings, sql_assistant_dataset_profile="unchecked")


def mappings(rows):
    return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


@pytest.mark.asyncio
async def test_enterprise_refuses_nonempty_unmarked_classic_warehouse_before_writes():
    connection = SimpleNamespace(scalar=AsyncMock(side_effect=[None, "dw.dim_news", True]), execute=AsyncMock())
    with pytest.raises(warehouse.SqlWarehouseContractError, match="new empty isolated warehouse"):
        await warehouse._profile_contract(connection, ENTERPRISE_PROFILE, enterprise_manifest(warehouse._fixture_rows(ENTERPRISE_PROFILE)))
    connection.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["profile", "version", "hash", "extra_row", "classic"])
async def test_stored_profile_identity_cannot_be_switched_or_rewritten(change):
    expected = enterprise_manifest(warehouse._fixture_rows(ENTERPRISE_PROFILE))
    stored = {key: expected[key] for key in ("dataset_profile", "dataset_version", "dataset_sha256")}
    rows = [stored]
    if change == "profile":
        stored["dataset_profile"] = CLASSIC_PROFILE
    elif change == "version":
        stored["dataset_version"] = "old-version"
    elif change == "hash":
        stored["dataset_sha256"] = "a" * 64
    elif change == "extra_row":
        rows.append(deepcopy(stored))
    connection = SimpleNamespace(scalar=AsyncMock(return_value="local_simulation.synthetic_dataset_contract"),
                                 execute=AsyncMock(return_value=mappings(rows)))
    with pytest.raises(warehouse.SqlWarehouseContractError, match="profile/version/hash mismatch"):
        await warehouse._profile_contract(connection, CLASSIC_PROFILE if change == "classic" else ENTERPRISE_PROFILE,
                                          None if change == "classic" else expected)
    assert all(str(call.args[0]).startswith("SELECT ") for call in connection.execute.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["metric", "dimension", "baseline", "missing_row", "extra_row"])
async def test_profile_rows_are_fully_checked_and_drift_is_not_repaired(change):
    expected = warehouse._fixture_rows(ENTERPRISE_PROFILE)
    actual = deepcopy(expected)
    if change == "metric":
        actual[1][10]["clicks"] += 1
    elif change == "dimension":
        actual[0][10]["title"] += " changed"
    elif change == "baseline":
        actual[2][10]["baseline_clicks"] += 1
    elif change == "missing_row":
        actual[1].pop()
    else:
        actual[1].append(deepcopy(actual[1][-1]))
    connection = SimpleNamespace(execute=AsyncMock(side_effect=[mappings(table) for table in actual]))
    with pytest.raises(warehouse.SqlWarehouseContractError, match="missing or changed"):
        await warehouse._verify_profile_rows(connection, expected)
    assert connection.execute.await_count == 3
    assert all(str(call.args[0]).startswith("SELECT ") for call in connection.execute.call_args_list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_enterprise_profile_repeats_without_updates_and_refuses_classic_switch():
    url = os.getenv("SQL_WAREHOUSE_ENTERPRISE_INTEGRATION_URL")
    if not url:
        pytest.skip("set SQL_WAREHOUSE_ENTERPRISE_INTEGRATION_URL to the separate enterprise demo PostgreSQL")
    database = SimpleNamespace(engine=create_async_engine(url))
    try:
        first = await warehouse.initialize_demo_warehouse(database, dataset_profile=ENTERPRISE_PROFILE)
        second = await warehouse.initialize_demo_warehouse(database, dataset_profile=ENTERPRISE_PROFILE)
        assert first == second == warehouse.demo_dataset_info(ENTERPRISE_PROFILE)
        with pytest.raises(warehouse.SqlWarehouseContractError, match="profile/version/hash mismatch"):
            await warehouse.initialize_demo_warehouse(database, dataset_profile=CLASSIC_PROFILE)
        async with database.engine.connect() as connection:
            await warehouse._verify_profile_rows(connection, warehouse._fixture_rows(ENTERPRISE_PROFILE))
        # Exercise the real guarded view; a different tenant must fail before SQL.
        from tests.test_sql_assistant_warehouse import VALID_SQL
        params = {"tenant_id": str(warehouse.DEMO_TENANT_ID), "window_start": warehouse.WINDOW_START + timedelta(hours=21),
                  "window_end": warehouse.WINDOW_START + timedelta(hours=22), "row_limit": 12}
        main = warehouse.LocalPostgresSqlWarehouseClient(database, tenant_id=str(warehouse.DEMO_TENANT_ID))
        other = warehouse.LocalPostgresSqlWarehouseClient(database, tenant_id=str(warehouse.ISOLATION_TENANT_ID))
        query = SqlQuery(VALID_SQL, params, timeout_ms=1000, max_rows=12)
        main_result = await main.execute(query)
        other_query = SqlQuery(VALID_SQL, {**params, "tenant_id": str(warehouse.ISOLATION_TENANT_ID)},
                               timeout_ms=1000, max_rows=12)
        other_result = await other.execute(other_query)
        with pytest.raises(warehouse.SqlWarehouseContractError, match="authenticated tenant"):
            await main.execute(other_query)
        main_rows = {row["news_id"]: row for row in main_result.rows}
        other_rows = {row["news_id"]: row for row in other_result.rows}
        assert len(main_rows) == len(other_rows) == 12
        for news_id in main_rows:
            assert other_rows[news_id]["impressions"] == main_rows[news_id]["impressions"] * 8
            assert other_rows[news_id]["clicks"] == main_rows[news_id]["clicks"] * 8
        # This deliberately changed aggregate is invisible outside our transaction
        # and is always rolled back, including assertion/error paths.
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("""UPDATE dw.news_metric_hourly SET clicks = clicks + 1
                    WHERE tenant_id = :tenant_id AND news_id = :news_id AND event_time = :event_time"""),
                    {"tenant_id": warehouse.DEMO_TENANT_ID, "news_id": "demo-news-001", "event_time": params["window_start"]})
                with pytest.raises(warehouse.SqlWarehouseContractError, match="missing or changed"):
                    await warehouse._verify_profile_rows(connection, warehouse._fixture_rows(ENTERPRISE_PROFILE))
            finally:
                await transaction.rollback()
        async with database.engine.connect() as connection:
            await warehouse._verify_profile_rows(connection, warehouse._fixture_rows(ENTERPRISE_PROFILE))
    finally:
        await database.engine.dispose()
