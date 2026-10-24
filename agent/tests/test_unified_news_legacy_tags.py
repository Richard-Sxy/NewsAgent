"""Frozen legacy-topic reuse and explicitly scoped aggregate access boundaries."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from app.modules.behavior_tags.tagging import TopicPolicy, tag_article
from app.modules.news_storage.legacy_tags import load_legacy_tags, query_legacy_behavior
from app.modules.news_storage.model import canonical_json


def _json_bytes(value):
    return (canonical_json(value) + "\n").encode()


def _freeze(directory, manifest):
    for name in ("policy.json", "content_labels.jsonl", "label_index.sqlite"):
        raw = (directory / name).read_bytes()
        manifest["artifacts"][name] = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
        if name.endswith(".jsonl"):
            manifest["artifacts"][name]["rows"] = len(raw.splitlines())
    manifest.pop("identity_sha256", None)
    manifest["identity_sha256"] = sha256(canonical_json(manifest).encode()).hexdigest()
    (directory / "manifest.json").write_bytes(_json_bytes(manifest))


@pytest.fixture
def legacy(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir()
    policy = TopicPolicy.model_validate({"schema_version": 1, "policy_version": "policy-v1",
        "catalog": {"schema_version": 1, "taxonomy_version": "topics-v1", "status": "draft",
                    "topics": [{"topic_id": "tech", "name": "科技", "provenance": "user_confirmed"}]},
        "rules": [{"rule_id": "tech-v1", "topic_id": "tech", "title_terms": ["AI"],
                   "body_terms": ["科技"]}]})
    content = tag_article({"tenant_id": "source-a", "news_id": "news-1", "title": "AI 新闻",
                           "content": "科技报道", "news_content_synthetic": False}, policy)
    (directory / "policy.json").write_bytes(_json_bytes(policy.model_dump(mode="json")))
    (directory / "content_labels.jsonl").write_bytes(_json_bytes(content))
    rows = []
    for hour, dataset, scenario, definition, uv in [(1, "dataset-a", "scenario-a", "metric-a", 3),
            (1, "dataset-b", "scenario-b", "metric-b", 4),
            (2, "dataset-a", "scenario-a", "metric-a", 5)]:
        rows.append({"tenant_id": "source-a", "news_id": "news-1", "content_sha256": content["content_sha256"],
            "window_start": f"2026-10-13T0{hour}:00:00+00:00",
            "window_end": f"2026-10-13T0{hour + 1}:00:00+00:00", "source_dataset_version": dataset,
            "scenario_id": scenario, "metric_definition_id": definition, "unique_users": uv,
            "clicks": uv + 3, "label_ids": ["reading.deep"], "reasons": [],
            "behavior_rule_version": "news-window-behavior-labels-v1",
            "behavior_label_scope": "news_window_aggregate", "behavior_labels_are_inferred": True,
            "synthetic_user_id": "must-never-be-returned"})
    with sqlite3.connect(directory / "label_index.sqlite") as conn:
        conn.executescript("""CREATE TABLE content_labels(tenant_id TEXT,news_id TEXT,payload_json TEXT);
            CREATE TABLE behavior_labels(tenant_id TEXT,news_id TEXT,window_start TEXT,window_end TEXT,
              source_dataset_version TEXT,scenario_id TEXT,metric_definition_id TEXT,payload_json TEXT);""")
        conn.execute("INSERT INTO content_labels VALUES(?,?,?)", ("source-a", "news-1", canonical_json(content)))
        for row in reversed(rows):
            conn.execute("INSERT INTO behavior_labels VALUES(?,?,?,?,?,?,?,?)", (
                row["tenant_id"], row["news_id"], row["window_start"], row["window_end"],
                row["source_dataset_version"], row["scenario_id"], row["metric_definition_id"], canonical_json(row)))
    manifest = {"schema_version": 1, "package_version": "behavior-tags-local-v1",
        "generator_version": "warehouse-guided-synthetic-events-v1",
        "behavior_rule_version": "news-window-behavior-labels-v1", "taxonomy_version": "topics-v1",
        "policy_version": "policy-v1", "tenants": ["source-a", "source-b"],
        "source_window": {"window_start": "2026-10-13T00:00:00+00:00", "window_end": "2026-10-14T00:00:00+00:00"},
        "counts": {"content_labels": 1}, "artifacts": {}}
    # These deliberately do not exist. Loading topics must not open personal data.
    manifest["artifacts"]["synthetic_events.jsonl"] = {"bytes": 1000, "sha256": "0" * 64}
    manifest["artifacts"]["synthetic_profiles.jsonl"] = {"bytes": 1000, "sha256": "0" * 64}
    _freeze(directory, manifest)
    return directory, manifest, content, rows


def _query(legacy, **overrides):
    directory, _, content, _ = legacy
    args = {"expected_manifest_sha256": sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        "source_tenant_id": "source-a", "news_id": "news-1", "content_sha256": content["content_sha256"],
        "window_start": "2026-10-13T00:00:00+00:00", "window_end": "2026-10-14T00:00:00+00:00"}
    return query_legacy_behavior(directory, **(args | overrides))


def test_topic_load_verifies_exact_binding_without_personal_artifacts(legacy):
    directory, manifest, content, _ = legacy
    result = load_legacy_tags(directory)
    assert result["labels"] == [content]
    assert result["policy"]["policy_version"] == "policy-v1"
    assert set(result["binding"]) == {"package_version", "manifest_sha256", "identity_sha256", "policy_sha256",
        "content_labels_sha256", "taxonomy_version", "policy_version"}
    assert result["binding"]["identity_sha256"] == manifest["identity_sha256"]


@pytest.mark.parametrize("name", ["policy.json", "content_labels.jsonl"])
def test_modified_used_artifact_is_rejected(legacy, name):
    directory, _, _, _ = legacy
    with (directory / name).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="size mismatch"):
        load_legacy_tags(directory)


def test_modified_manifest_is_rejected(legacy):
    directory, manifest, _, _ = legacy
    manifest["seed"] = 42
    (directory / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(ValueError, match="identity mismatch"):
        load_legacy_tags(directory)


@pytest.mark.parametrize("target", ["manifest", "policy", "content"])
def test_duplicate_json_keys_rejected(legacy, target):
    directory, manifest, _, _ = legacy
    name = {"manifest": "manifest.json", "policy": "policy.json", "content": "content_labels.jsonl"}[target]
    raw = (directory / name).read_bytes().replace(b"{", b'{"duplicate":1,"duplicate":2,', 1)
    (directory / name).write_bytes(raw)
    if target != "manifest":
        _freeze(directory, manifest)
    with pytest.raises(ValueError, match="duplicate JSON"):
        load_legacy_tags(directory)


@pytest.mark.parametrize("version_key", ["schema_version", "package_version", "generator_version", "behavior_rule_version"])
def test_non_local_or_unsupported_package_rejected(legacy, version_key):
    directory, manifest, _, _ = legacy
    manifest[version_key] = "enterprise-production"
    _freeze(directory, manifest)
    with pytest.raises(ValueError, match="unsupported non-local"):
        load_legacy_tags(directory)


def test_policy_manifest_version_disagreement_rejected(legacy):
    directory, manifest, _, _ = legacy
    manifest["policy_version"] = "changed-v2"
    _freeze(directory, manifest)
    with pytest.raises(ValueError, match="version mismatch"):
        load_legacy_tags(directory)


def test_duplicate_content_identity_and_bad_count_rejected(legacy):
    directory, manifest, content, _ = legacy
    (directory / "content_labels.jsonl").write_bytes(_json_bytes(content) * 2)
    _freeze(directory, manifest)
    with pytest.raises(ValueError, match="duplicate mismatch"):
        load_legacy_tags(directory)
    (directory / "content_labels.jsonl").write_bytes(_json_bytes(content))
    manifest["counts"]["content_labels"] = 2
    _freeze(directory, manifest)
    with pytest.raises(ValueError, match="count mismatch"):
        load_legacy_tags(directory)


def test_symlink_artifact_and_directory_rejected(legacy, tmp_path):
    directory, _, _, _ = legacy
    link = tmp_path / "linked-directory"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        load_legacy_tags(link)
    policy = directory / "policy.json"
    moved = directory / "moved-policy.json"
    policy.rename(moved)
    policy.symlink_to(moved)
    with pytest.raises(ValueError, match="regular file"):
        load_legacy_tags(directory)


def test_query_preserves_sources_hours_and_uv_without_personal_data(legacy):
    result = _query(legacy)
    assert result["status"] == "available"
    assert result["source_mode"] == "synthetic"
    assert [r["unique_users"] for r in result["rows"]] == [3, 4, 5]
    assert [r["metric_definition_id"] for r in result["rows"]] == ["metric-a", "metric-b", "metric-a"]
    assert all("synthetic_user_id" not in row for row in result["rows"])
    assert all(row["source_tenant_id"] == "source-a" for row in result["rows"])
    limited = _query(legacy, limit=1)
    assert limited["truncated"] is True
    assert [r["unique_users"] for r in limited["rows"]] == [3]


@pytest.mark.parametrize("overrides,reason", [
    ({"source_tenant_id": "not-a-tenant"}, "source_tenant_not_in_package"),
    ({"source_tenant_id": "source-b"}, "news_not_in_legacy_package"),
    ({"news_id": "other-news"}, "news_not_in_legacy_package"),
    ({"content_sha256": "0" * 64}, "content_hash_mismatch"),
    ({"window_start": "2026-10-13T10:00:00+00:00"}, "no_aggregate_for_requested_window"),
])
def test_unavailable_is_explicit_and_never_cross_tenant_fallback(legacy, overrides, reason):
    result = _query(legacy, **overrides)
    assert result["status"] == "unavailable" and result["reason"] == reason
    assert result["rows"] == []


@pytest.mark.parametrize("overrides", [
    {"window_start": "2026-10-13T00:00:00"},
    {"window_start": "2026-10-12T00:00:00+00:00"},
    {"window_end": "2026-10-15T00:00:00+00:00"},
    {"window_start": "2026-10-14T00:00:00+00:00"},
    {"limit": 0}, {"limit": 101}, {"limit": True},
    {"expected_manifest_sha256": "0" * 64},
])
def test_window_limit_and_manifest_binding_are_required(legacy, overrides):
    with pytest.raises(ValueError):
        _query(legacy, **overrides)


def test_timezone_offsets_select_same_instants(legacy):
    result = _query(legacy, window_start=datetime(2026, 10, 13, 1, tzinfo=timezone.utc),
                    window_end="2026-10-13T10:00:00+08:00")
    assert [row["unique_users"] for row in result["rows"]] == [3, 4]


def test_sqlite_hash_checked_only_on_aggregate_query(legacy):
    directory, _, _, _ = legacy
    with (directory / "label_index.sqlite").open("ab") as stream:
        stream.write(b"damage")
    assert load_legacy_tags(directory)["labels"]
    with pytest.raises(ValueError, match="size mismatch"):
        _query(legacy)


def test_indexed_content_must_match_verified_jsonl(legacy):
    directory, manifest, content, _ = legacy
    with sqlite3.connect(directory / "label_index.sqlite") as conn:
        conn.execute("UPDATE content_labels SET payload_json=?", (canonical_json({**content, "title": "Changed"}),))
    _freeze(directory, manifest)
    with pytest.raises(ValueError, match="differs from verified content"):
        _query(legacy)


@pytest.mark.parametrize("changed", [{"tenant_id": "source-b"}, {"news_id": "other"},
    {"content_sha256": "0" * 64}, {"scenario_id": "different"},
    {"window_start": "2026-10-13T00:00:00+00:00"}, {"behavior_rule_version": "v2"}])
def test_behavior_payload_scope_must_match_index_and_bound_content(legacy, changed):
    directory, manifest, _, rows = legacy
    with sqlite3.connect(directory / "label_index.sqlite") as conn:
        conn.execute("UPDATE behavior_labels SET payload_json=? WHERE rowid=1", (canonical_json(rows[-1] | changed),))
    _freeze(directory, manifest)
    with pytest.raises(ValueError, match="identity/window mismatch"):
        _query(legacy)
