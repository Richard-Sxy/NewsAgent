from datetime import datetime, timedelta
from itertools import islice
from copy import deepcopy
from pathlib import Path

import pytest

from app.sql_assistant.timeline_rich_profiles import (
    END, HOURS, START, TOPICS_V5, SOURCES, STAGES,
    iter_timeline_v5_rows, timeline_v5_manifest, timeline_v5_scenarios,
)
from app.sql_assistant.warehouse import (
    SCHEMA_SHA256_V5, demo_dataset_info, dataset_window, verify_schema_contract,
)
from simulation.native_hot_news_sql_support import _validate_window


def test_timeline_v5_boundaries_and_counters():
    rows = list(islice(iter_timeline_v5_rows("news_metric_hourly", 120), HOURS * 2))
    assert rows[0]["event_time"] == START
    assert rows[HOURS - 1]["event_time"] == END - timedelta(hours=1)
    for row in rows:
        assert 0 <= row["unique_users"] <= row["clicks"] <= row["impressions"]
        assert 0 <= row["effective_consumptions"] <= row["clicks"]
    # Topic-level daily waves must produce day-over-day variation beyond v4 factors.
    assert rows[19]["clicks"] != rows[19 + 24]["clicks"]


def test_timeline_v5_dim_news_topic_source_and_stage_coverage():
    rows = list(iter_timeline_v5_rows("dim_news", 1200))
    assert len(rows) == 2400
    titles = {row["title"] for row in rows}
    assert len(titles) == 1200
    assert {row["category"] for row in rows} == {"科技", "财经", "体育", "社会"}
    assert {row["content_type"] for row in rows} == {"article", "video"}
    assert all(row["publish_time"] < START for row in rows)
    assert {row["source"] for row in rows} == {f"合成{name}" for name in SOURCES}
    for category, topics in TOPICS_V5.items():
        assert len(topics) == 24 and len(set(topics)) == 24
        for topic in topics:
            assert any(f"{category}·{topic}（" in row["title"] for row in rows)
    joined = "".join(titles)
    assert all(stage in joined for stage in STAGES)


def test_timeline_v5_manifest_counts_and_determinism():
    manifest = timeline_v5_manifest(120)
    assert manifest["dataset_profile"] == "timeline-v5"
    assert manifest["dataset_version"] == "news-timeline-20260910-20261009-v2"
    assert manifest["tables"]["dim_news"]["rows"] == 240
    assert manifest["tables"]["news_metric_hourly"]["rows"] == 172800
    assert manifest["tables"]["news_metric_baseline_hourly"]["rows"] == 172800
    mutated = deepcopy(manifest)
    mutated["tables"]["dim_news"]["rows"] = 0
    assert timeline_v5_manifest(120)["tables"]["dim_news"]["rows"] == 240


def test_timeline_v5_scenarios_within_window():
    scenarios = timeline_v5_scenarios()
    assert len(scenarios) == 17
    ids = [case["id"] for case in scenarios]
    assert len(ids) == len(set(ids))
    assert {"weekday-weekend", "intra-day-contrast", "video-morning-surge", "midday-zero-baseline"} <= set(ids)
    for case in scenarios:
        _validate_window(datetime.fromisoformat(case["current_start"]),
                         datetime.fromisoformat(case["current_end"]), "timeline-v5")
        assert case["reference_start"] < case["current_start"] < case["current_end"]


def test_timeline_v5_frozen_contract_and_dataset_info():
    from app.sql_assistant.scenarios import load_sql_scenarios
    config = load_sql_scenarios(Path(__file__).resolve().parents[1] / "deploy/text2sql-scenes.timeline-v5.yml")
    assert config.warehouse_schema_version == "news-warehouse-v5"
    assert verify_schema_contract("timeline-v5").startswith("# 本地热点新闻数仓契约 v5")
    assert dataset_window("timeline-v5") == (START, END)
    assert dataset_window("timeline-v5")[1] - dataset_window("timeline-v5")[0] == timedelta(days=30)
    info = demo_dataset_info("timeline-v5", news_per_tenant=120)
    assert info["schema_version"] == "news-warehouse-v5"
    assert info["schema_sha256"] == SCHEMA_SHA256_V5
    assert info["hours_per_news"] == 720 and info["news_per_tenant"] == 120
    assert info["total_metric_row_count"] == 172800


def test_timeline_v5_binding_scope():
    from app.sql_assistant.hot_news_binding import HotNewsSqlBindingError, build_hot_news_sql_scope
    from app.sql_assistant.scenarios import load_sql_scenarios
    config = load_sql_scenarios(Path(__file__).resolve().parents[1] / "deploy/text2sql-scenes.timeline-v5.yml")
    values = dict(question="查询点击量最高的前100条新闻", scenario_id="news-ranking",
        window_start=START, window_end=START + timedelta(hours=1), schema_sha256=SCHEMA_SHA256_V5,
        scenario=config.resolve("news-ranking").model_dump(mode="json"),
        production_bundle_version="local-test-v1", model_scene="text2sql_assistant",
        warehouse_schema_version="news-warehouse-v5", dataset_identity=dict(dataset_profile="timeline-v5",
            dataset_version="news-timeline-20260910-20261009-v2", dataset_sha256="b" * 64, news_per_tenant=120))
    first = build_hot_news_sql_scope(**values)
    second = build_hot_news_sql_scope(**{**values, "window_start": START + timedelta(days=1),
                                         "window_end": START + timedelta(days=1, hours=1)})
    assert first != second
    with pytest.raises(HotNewsSqlBindingError):
        build_hot_news_sql_scope(**{**values, "warehouse_schema_version": "news-warehouse-v4"})
    with pytest.raises(HotNewsSqlBindingError):
        build_hot_news_sql_scope(**{**values, "dataset_identity": {**values["dataset_identity"],
                                                                    "news_per_tenant": 12}})
