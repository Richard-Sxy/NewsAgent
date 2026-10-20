"""The real-news-v8 Agent profile keeps content and metric evidence separate."""

from datetime import datetime
from uuid import UUID

import pytest

from app.sql_assistant.real_news_profiles import (
    REAL_NEWS_COUNT, iter_real_news_rows, real_news_manifest, real_news_news_ids,
    validate_real_news_count,
)
from app.sql_assistant.scenarios import load_sql_scenarios
from app.sql_assistant.warehouse import dataset_window, warehouse_contract
from simulation.warehouse_simulation import real_news_catalog as catalog

pytestmark = pytest.mark.skipif(
    not catalog.EXTENSION_PATH.exists(),
    reason="run agent/tools/freeze_real_news_warehouse_extension.py first",
)


def test_profile_roster_and_window_are_the_frozen_extension():
    assert validate_real_news_count(REAL_NEWS_COUNT) == 8120
    ids = real_news_news_ids()
    assert len(ids) == len(set(ids)) == 8120
    assert dataset_window("real-news-v8") == (
        datetime.fromisoformat("2026-09-30T00:00:00+08:00"),
        datetime.fromisoformat("2026-10-14T20:00:00+08:00"),
    )
    assert warehouse_contract("real-news-v8").version == "news-warehouse-v7"


def test_profile_manifest_reports_content_only_and_metric_counts():
    manifest = real_news_manifest()
    assert manifest["catalog_count"] == 661
    assert manifest["extension_count"] == 7459
    assert manifest["metric_available_article_count"] == 661
    assert manifest["content_only_article_count"] == 7459
    assert manifest["tables"]["dim_news"]["rows"] == 16240
    assert manifest["tables"]["news_metric_hourly"]["rows"] > 0
    assert manifest["news_content_synthetic"] is False
    assert manifest["metric_data_synthetic"] is True


def test_dimension_rows_include_content_only_articles_but_metrics_do_not():
    extension_id = catalog.load_extension()[0]["news_id"]
    dimension_ids = {row["news_id"] for row in iter_real_news_rows("dim_news")}
    assert extension_id in dimension_ids
    metric_ids = {row["news_id"] for row in iter_real_news_rows("news_metric_hourly")}
    assert metric_ids
    assert extension_id not in metric_ids
    assert len(dimension_ids) == 8120


def test_profile_rows_are_tenant_scoped_and_typed():
    rows = list(iter_real_news_rows("dim_news"))
    assert {row["tenant_id"] for row in rows} == {
        UUID("11111111-1111-4111-8111-111111111111"),
        UUID("33333333-3333-4333-8333-333333333333"),
    }
    assert all(row["content_type"] == "article" for row in rows)
    assert {row["category"] for row in rows} == {
        "科技", "财经", "体育", "社会", "汽车", "教育", "娱乐",
    }


def test_real_news_scenario_declares_all_seven_categories():
    config = load_sql_scenarios("deploy/text2sql-scenes.real-news-v8.yml")
    assert config.warehouse_schema_version == "news-warehouse-v7"
    assert config.resolve("news-ranking").allowed_categories == (
        "科技", "财经", "体育", "社会", "汽车", "教育", "娱乐",
    )


def test_invalid_profile_count_is_rejected():
    with pytest.raises(ValueError):
        validate_real_news_count(661)
