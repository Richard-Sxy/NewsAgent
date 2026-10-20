"""Real v2 extension: content/features exist, behavior metrics do not."""

from hashlib import sha256

import pytest

from simulation.warehouse_simulation import real_news as warehouse
from simulation.warehouse_simulation import real_news_catalog as catalog
from simulation.warehouse_simulation.content_features import validate_content_features

pytestmark = pytest.mark.skipif(
    not catalog.EXTENSION_PATH.exists(),
    reason="run agent/tools/freeze_real_news_warehouse_extension.py first",
)


def test_extension_manifest_is_frozen_real_content_without_metrics():
    rows = catalog.load_extension()
    assert len(rows) == 7459
    assert len({row["news_id"] for row in rows}) == 7459
    assert [row["news_id"] for row in rows] == sorted(row["news_id"] for row in rows)
    assert all(row["news_content_synthetic"] is False and row["metric_available"] is False for row in rows)
    assert all(row["category"] == "社会" for row in rows)
    # Distinct news_ids are required; a handful of different publications can
    # still share the same body text and must not be silently merged.
    assert len({row["content_sha256"] for row in rows}) >= 7451
    assert sha256(catalog.EXTENSION_PATH.read_bytes()).hexdigest() == catalog.EXTENSION_SHA256


def test_extension_source_bodies_and_features_still_match_the_frozen_manifest():
    articles = catalog.load_extended_catalog()
    by_id = {article["news_id"]: article for article in articles}
    for row in catalog.load_extension()[:20]:
        article = by_id[row["news_id"]]
        assert sha256(article["content"].encode()).hexdigest() == row["content_sha256"]
        validate_content_features(
            {key: row[key] for key in ("title_chars", "content_chars", "paragraph_count",
                                      "sentence_count", "digit_token_count", "keyword_top")},
            article["title"], article["content"],
        )
        assert article["metric_available"] is False


def test_content_only_articles_have_no_behavior_metric_rows():
    config = warehouse.RealNewsSimulationConfig(closed_hours=1)
    extension_ids = {row["news_id"] for row in catalog.load_extension()}
    metric_ids = {row["news_id"] for row in warehouse.iter_rows("news_metric_hourly", config)}
    assert metric_ids
    assert not (metric_ids & extension_ids)


def test_content_features_are_generated_for_both_tenants():
    config = warehouse.RealNewsSimulationConfig(closed_hours=1)
    rows = list(warehouse.iter_rows("news_content_feature", config))
    assert len(rows) == 8120 * 2
    assert {row["tenant_id"] for row in rows} == set(warehouse.TENANTS)
    assert any(row["metric_available"] is False for row in rows)
    assert all(row["content_chars"] > 0 and row["title_chars"] > 0 for row in rows)
