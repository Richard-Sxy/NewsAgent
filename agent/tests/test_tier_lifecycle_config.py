"""Lifecycle policy contracts: explicit configuration and unambiguous time rules."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from app.modules.knowledge.tier_lifecycle_config import (
    KnowledgeLifecycleConfig,
    load_knowledge_lifecycle_config,
)
from app.retrieval.tiered_vector import VectorTier


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
LOCAL_CONFIG = DEPLOY / "knowledge-lifecycle.local.example.yml"
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def config() -> KnowledgeLifecycleConfig:
    return load_knowledge_lifecycle_config(LOCAL_CONFIG)


def changed(config, **overrides):
    return KnowledgeLifecycleConfig.model_validate(config.model_dump() | overrides)


def test_local_example_loads_all_three_tiers_without_connections(config):
    assert config.embedding_version == "native-local-embedding-v1"
    assert config.expected_embedding_dimensions == 32
    assert set(config.query_weights) == set(VectorTier)
    assert config.task_queue == "knowledge-lifecycle-local"


def test_enterprise_template_cannot_silently_use_local_defaults():
    with pytest.raises(ValueError):
        load_knowledge_lifecycle_config(DEPLOY / "knowledge-lifecycle.enterprise.example.yml")


@pytest.mark.parametrize("overrides, match", [
    ({"unexpected_key": 1}, "extra_forbidden"),
    ({"schema_version": 2}, "schema_version"),
    ({"schema_version": True}, "schema_version"),
    ({"policy_version": " "}, "policy_version"),
    ({"embedding_version": " model-v1 "}, "embedding_version"),
    ({"chunking_version": ""}, "chunking_version"),
    ({"task_queue": " "}, "task_queue"),
    ({"hot_max_age_seconds": 480}, "hot_max_age_seconds"),
    ({"hot_max_age_seconds": 481}, "hot_max_age_seconds"),
    ({"cold_min_age_seconds": 0}, "cold_min_age_seconds"),
    ({"chunk_overlap": 800}, "chunk_overlap"),
    ({"chunk_overlap": -1}, "chunk_overlap"),
    ({"chunk_chars": "800"}, "chunk_chars"),
    ({"max_chunks_per_document": 0}, "max_chunks_per_document"),
    ({"max_vector_coordinates_per_document": 31}, "max_vector_coordinates_per_document"),
    ({"expected_embedding_dimensions": True}, "expected_embedding_dimensions"),
    ({"embedding_batch_size": 0}, "embedding_batch_size"),
    ({"vector_batch_size": 4097}, "vector_batch_size"),
    ({"migration_interval_seconds": 0}, "migration_interval_seconds"),
    ({"dispatch_interval_seconds": float("nan")}, "dispatch_interval_seconds"),
    ({"max_attempts": 0}, "max_attempts"),
    ({"retry_initial_interval_seconds": 20}, "retry_max_interval_seconds"),
    ({"workflow_timeout_seconds": 30}, "workflow_timeout_seconds"),
    ({"query_max_candidates": 10}, "query_max_candidates"),
    ({"query_refill_factor": 1}, "query_refill_factor"),
    ({"query_request_timeout_seconds": float("inf")}, "query_request_timeout_seconds"),
    ({"query_max_concurrency": 0}, "query_max_concurrency"),
    ({"max_search_rows": 0}, "max_search_rows"),
    ({"query_weights": {"hot": 1, "warm": 1}}, "query_weights"),
    ({"query_weights": {"hot": 1, "warm": 1, "cold": -1}}, "query_weights"),
    ({"query_weights": {"hot": 0, "warm": 0, "cold": 0}}, "query weight"),
    ({"query_weights": {"hot": 1, "warm": 1, "cold": float("nan")}}, "query_weights"),
])
def test_invalid_policy_is_rejected(config, overrides, match):
    with pytest.raises(ValueError, match=match):
        changed(config, **overrides)


def test_all_settings_are_explicit(config):
    for key in KnowledgeLifecycleConfig.model_fields:
        values = config.model_dump()
        del values[key]
        with pytest.raises(ValueError, match=key):
            KnowledgeLifecycleConfig.model_validate(values)


@pytest.mark.parametrize("age, expected", [
    (0, VectorTier.HOT),
    (119.999, VectorTier.HOT),
    (120, VectorTier.WARM),
    (479.999, VectorTier.WARM),
    (480, VectorTier.COLD),
    (10000, VectorTier.COLD),
])
def test_age_boundaries(config, age, expected):
    assert config.classify_tier(NOW - timedelta(seconds=age), NOW) == expected


def test_reheat_expires_at_exact_boundary(config):
    old = NOW - timedelta(days=10)
    assert config.classify_tier(old, NOW, reheat_until=NOW + timedelta(seconds=1)) == VectorTier.HOT
    assert config.classify_tier(old, NOW, reheat_until=NOW) == VectorTier.COLD
    assert config.classify_tier(old, NOW, reheat_until=NOW - timedelta(seconds=1)) == VectorTier.COLD


@pytest.mark.parametrize("publish, now, hold, match", [
    (None, NOW, None, "publish_time"),
    (NOW.replace(tzinfo=None), NOW, None, "publish_time"),
    (NOW, NOW.replace(tzinfo=None), None, "now"),
    (NOW, NOW, NOW.replace(tzinfo=None), "reheat_until"),
    (NOW + timedelta(microseconds=1), NOW, None, "later than now"),
    (NOW + timedelta(seconds=1), NOW, NOW + timedelta(days=1), "later than now"),
])
def test_missing_ambiguous_or_future_time_is_rejected(config, publish, now, hold, match):
    with pytest.raises(ValueError, match=match):
        config.classify_tier(publish, now, reheat_until=hold)


def test_timezone_offsets_preserve_instant(config):
    shanghai = NOW.astimezone(timezone(timedelta(hours=8)))
    assert config.classify_tier(shanghai - timedelta(seconds=120), NOW) == VectorTier.WARM


def test_dst_transition_uses_elapsed_time_not_wall_clock(config):
    new_york = ZoneInfo("America/New_York")
    # The repeated 01:30 is one elapsed hour later, despite equal wall clock values.
    before = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0)
    after = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)
    assert config.classify_tier(before, after) == VectorTier.COLD


def test_loader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "duplicate.yml"
    path.write_text(LOCAL_CONFIG.read_text() + "\npolicy_version: silently-changed\n")
    with pytest.raises(ValueError, match="duplicate key"):
        load_knowledge_lifecycle_config(path)


def test_loader_rejects_duplicate_tier_weights(tmp_path):
    path = tmp_path / "duplicate.yml"
    path.write_text(LOCAL_CONFIG.read_text().replace("cold: 1.0}", "cold: 1.0, cold: 0.1}"))
    with pytest.raises(ValueError, match="duplicate key"):
        load_knowledge_lifecycle_config(path)


@pytest.mark.parametrize("content", ["[invalid", "!!python/object:object {}", "", "- list-not-object"])
def test_loader_rejects_invalid_yaml(tmp_path, content):
    path = tmp_path / "invalid.yml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_knowledge_lifecycle_config(path)


def test_loader_rejects_missing_and_oversized_file(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        load_knowledge_lifecycle_config(tmp_path / "missing.yml")
    oversized = tmp_path / "large.yml"
    oversized.write_bytes(b" " * 1_048_577)
    with pytest.raises(ValueError, match="exceeds"):
        load_knowledge_lifecycle_config(oversized)


def test_integer_seconds_and_float_weights_roundtrip(config, tmp_path):
    path = tmp_path / "roundtrip.yml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    assert load_knowledge_lifecycle_config(path) == config
