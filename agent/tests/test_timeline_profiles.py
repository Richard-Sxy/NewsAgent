from datetime import datetime, timedelta
from itertools import islice
from copy import deepcopy

import pytest

from app.sql_assistant.timeline_profiles import START, END, HOURS, iter_timeline_rows, timeline_manifest
from app.sql_assistant.warehouse import demo_dataset_info, dataset_window, verify_schema_contract
from app.sql_assistant.scaled_profiles import scaled_manifest
from examples.native_hot_news_sql_support import _validate_window
from examples.data_analysis_demo import _catalog, DemoError


def test_timeline_boundaries_and_counters():
    rows = list(islice(iter_timeline_rows("news_metric_hourly", 120), HOURS * 2))
    assert rows[0]["event_time"] == START
    assert rows[HOURS - 1]["event_time"] == END - timedelta(hours=1)
    assert len({r["event_time"] for r in rows[:HOURS]}) == HOURS
    for row in rows:
        assert 0 <= row["unique_users"] <= row["clicks"] <= row["impressions"]
        assert 0 <= row["effective_consumptions"] <= row["clicks"]
    assert rows[19]["clicks"] != rows[19 + 24]["clicks"]
    assert rows[19]["clicks"] / rows[19 + HOURS]["clicks"] != rows[19 + 24]["clicks"] / rows[19 + 24 + HOURS]["clicks"]


def test_manifest_and_catalog_integrity():
    manifest = timeline_manifest(120)
    assert manifest["tables"]["dim_news"]["rows"] == 240
    assert manifest["tables"]["news_metric_hourly"]["rows"] == 172800
    assert manifest["tables"]["news_metric_baseline_hourly"]["rows"] == 172800
    config = {"schema_version": "news-warehouse-v4", "schema_sha256": "d" * 64,
              "dataset": {**manifest, "schema_version": "news-warehouse-v4", "schema_sha256": "d" * 64,
                "news_count": 120, "metric_row_count": 86400, "baseline_row_count": 86400,
                "total_news_count": 240, "total_metric_row_count": 172800, "total_baseline_row_count": 172800}}
    identity, scenarios = _catalog(config)
    assert len(scenarios) == 13
    assert identity["hours_per_news"] == 720
    for case in scenarios:
        _validate_window(datetime.fromisoformat(case.current_start),
                         datetime.fromisoformat(case.current_end), "timeline-v4")
    bad = deepcopy(config)
    bad["dataset"]["hours_per_news"] = 24
    with pytest.raises(DemoError):
        _catalog(bad)
    manifest["tables"]["dim_news"]["rows"] = 0
    assert timeline_manifest(120)["tables"]["dim_news"]["rows"] == 240
    assert scaled_manifest(120)["hours_per_news"] == 24


def test_news_coverage_and_frozen_contract():
    from app.sql_assistant.scenarios import load_sql_scenarios
    from pathlib import Path
    assert load_sql_scenarios(Path(__file__).resolve().parents[1] / "deploy/text2sql-scenes.timeline-v4.yml").warehouse_schema_version == "news-warehouse-v4"
    rows = list(iter_timeline_rows("dim_news", 120))
    assert len({row["title"] for row in rows}) == 120
    assert {row["category"] for row in rows} == {"科技", "财经", "体育", "社会"}
    assert {row["content_type"] for row in rows} == {"article", "video"}
    assert all(row["publish_time"] < START for row in rows)
    assert "720" in verify_schema_contract("timeline-v4")
    assert dataset_window("classic-v1")[1] - dataset_window("classic-v1")[0] == timedelta(days=1)
    _validate_window(END - timedelta(hours=1), END, "timeline-v4")
    with pytest.raises(Exception, match="outside"):
        _validate_window(END, END + timedelta(hours=1), "timeline-v4")


def test_timeline_binding_and_schema_identity():
    from app.sql_assistant.hot_news_binding import build_hot_news_sql_scope, HotNewsSqlBindingError
    from app.sql_assistant.scenarios import load_sql_scenarios
    from pathlib import Path
    from app.sql_assistant.warehouse import SCHEMA_SHA256_V4
    config = load_sql_scenarios(Path(__file__).resolve().parents[1] / "deploy/text2sql-scenes.timeline-v4.yml")
    values = dict(question="查询点击量最高的前100条新闻", scenario_id="news-ranking",
        window_start=START, window_end=START + timedelta(hours=1), schema_sha256=SCHEMA_SHA256_V4,
        scenario=config.resolve("news-ranking").model_dump(mode="json"),
        production_bundle_version="local-test-v1", model_scene="text2sql_assistant",
        warehouse_schema_version="news-warehouse-v4", dataset_identity=dict(dataset_profile="timeline-v4",
            dataset_version="timeline-test-v1", dataset_sha256="a" * 64, news_per_tenant=120))
    first = build_hot_news_sql_scope(**values)
    second = build_hot_news_sql_scope(**{**values, "window_start": START + timedelta(days=1),
                                        "window_end": START + timedelta(days=1, hours=1)})
    assert first != second
    with pytest.raises(HotNewsSqlBindingError):
        build_hot_news_sql_scope(**{**values, "warehouse_schema_version": "news-warehouse-v3"})
