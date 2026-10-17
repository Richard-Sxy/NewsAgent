"""Offline package acceptance: provenance, determinism, event-only profiles and atomic writes."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from app.modules.audience.taxonomy import Topic, TopicCatalog
from app.modules.behavior_tags import build
from app.modules.behavior_tags.source import WarehouseSource
from app.modules.behavior_tags.tagging import TopicPolicy, TopicRule


START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=7)
TENANTS = ("11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333")


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def fixture_source():
    articles, observations = [], []
    for tenant in TENANTS:
        for index, (title, content) in enumerate((("航天科学研究", "航天实验取得新进展。"),
                                                 ("资本市场新闻", "市场政策与经济变化。"))):
            article = dict(tenant_id=tenant, news_id=f"news-{index}", title=title,
                content=content, content_sha256=sha256(content.encode()).hexdigest(),
                content_version=1, source_content_version=1,
                news_content_synthetic=False, category_data_synthetic=True,
                publish_time=(START - timedelta(days=1)).isoformat(), status="published")
            articles.append(article)
            for hour in (0, 1):
                observations.append(dict(tenant_id=tenant, news_id=f"news-{index}",
                    window_start=(START + timedelta(hours=hour)).isoformat(),
                    window_end=(START + timedelta(hours=hour + 1)).isoformat(),
                    event_time=(START + timedelta(hours=hour)).isoformat(),
                    content_sha256=article["content_sha256"], source_content_version=1,
                    impressions=200, clicks=40, unique_users=17, detail_page_views=50,
                    direct_page_views=10, detail_visitors=30, total_duration_seconds=3600,
                    effective_consumptions=30, interactions=8, likes=2, comments=2, shares=2,
                    favorites=2, negative_feedback=1, sample_count=None,
                    baseline_impressions=None, baseline_clicks=None,
                    baseline_effective_consumptions=None, baseline_interactions=None))
    return WarehouseSource(tuple(articles), tuple(observations),
        {"dataset_sha256": "a" * 64, "metric_data_synthetic": True}, {}, START, END, END + timedelta(minutes=35))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source_dir = tmp_path / "frozen-source"
    source_dir.mkdir()
    marker = source_dir / "immutable.json"
    marker.write_text('{"source":"untouched"}')
    policy = TopicPolicy(policy_version="topic-rules-test-v1", catalog=TopicCatalog(
        taxonomy_version="topics-test-v1", status="draft", topics=(
            Topic(topic_id="tech", name="科技", provenance="user_confirmed"),
            Topic(topic_id="tech.space", name="航天", parent_topic_id="tech", provenance="draft_extension"),
            Topic(topic_id="finance", name="财经", provenance="user_confirmed"),
            Topic(topic_id="finance.markets", name="市场", parent_topic_id="finance", provenance="draft_extension"),
        )), rules=(
            TopicRule(rule_id="space.v1", topic_id="tech.space", title_terms=("航天",), body_terms=("航天",)),
            TopicRule(rule_id="markets.v1", topic_id="finance.markets", title_terms=("市场",), body_terms=("市场",)),
        ))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    source = fixture_source()
    monkeypatch.setattr(build, "_read_source", lambda path: source)
    return source_dir, policy_path, source


def construct(inputs, output, **options):
    source_dir, policy_path, _ = inputs
    return build.build_package(source_dir, output, policy_path=policy_path, users_per_tenant=20, **options)


def test_package_is_repeatable_versioned_and_keeps_source_unchanged(inputs, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    marker = (inputs[0] / "immutable.json").read_bytes()
    one = construct(inputs, first)
    two = construct(inputs, second)
    assert one == two
    assert {path.name for path in first.iterdir()} == {
        "manifest.json", "policy.json", "content_labels.jsonl", "news_behavior_labels.jsonl",
        "synthetic_events.jsonl", "synthetic_profiles.jsonl", "label_index.sqlite"}
    assert all(path.read_bytes() == (second / path.name).read_bytes() for path in first.iterdir())
    assert (inputs[0] / "immutable.json").read_bytes() == marker
    unsigned = {key: value for key, value in one.items() if key != "identity_sha256"}
    assert one["identity_sha256"] == sha256(build._json(unsigned).encode()).hexdigest()
    for name, metadata in one["artifacts"].items():
        assert sha256((first / name).read_bytes()).hexdigest() == metadata["sha256"]
    assert one["source_identity"] == inputs[2].source_identity
    assert all(datetime.fromisoformat(stamp) <= inputs[2].as_of for stamp in one["profile_snapshot_times"])
    assert one["provenance"]["synthetic_events"].startswith("independent synthetic world")


def test_content_and_aggregate_labels_have_distinct_provenance_and_uv_is_not_added(inputs, tmp_path):
    output = tmp_path / "package"
    construct(inputs, output)
    content = rows(output / "content_labels.jsonl")
    behavior = rows(output / "news_behavior_labels.jsonl")
    assert len(content) == 4 and all(item["provenance"] == "rule_inferred" for item in content)
    assert content[0]["topic_ids"] == ["tech", "tech.space"]
    assert len(behavior) == 8 and all(item["unique_users"] == 17 for item in behavior)
    assert all(item["sample_count"] is None and "baseline.missing" in item["label_ids"] for item in behavior)
    assert all(item["behavior_label_scope"] == "news_window_aggregate" for item in behavior)
    assert {item["content_sha256"] for item in content} == {item["content_sha256"] for item in behavior}


def test_profiles_follow_only_visible_synthetic_events_and_preserve_cold_start(inputs, tmp_path):
    output = tmp_path / "package"
    manifest = construct(inputs, output)
    events = rows(output / "synthetic_events.jsonl")
    profiles = rows(output / "synthetic_profiles.jsonl")
    assert all(event["source_mode"] == "synthetic" and event["synthetic_user_id"].startswith("synthetic-") for event in events)
    assert all(event["not_reconstructed_from_aggregate_uv"] for event in events)
    assert not any({"user_id", "anonymous_user_key", "device_id", "session_id"} & event.keys() for event in events)
    assert all(START <= datetime.fromisoformat(event["occurred_at"]) < END for event in events)
    for event in events:
        if event["sampling_reference"]:
            assert datetime.fromisoformat(event["sampling_reference"]["window_end"]) <= datetime.fromisoformat(event["occurred_at"])
            assert event["source_dataset_version"] == event["sampling_reference"]["source_dataset_version"]
        else:
            assert event["sampling_mode"] == "unconditioned_synthetic_prior"
        if event["news_id"]:
            assert event["channel"] in build.CHANNELS and event["device"] in build.DEVICES
    assert {profile["cohort_id"] for profile in profiles} == set(build.COHORT_IDS)
    first = [profile for profile in profiles if profile["as_of"] == manifest["profile_snapshot_times"][0]]
    assert all(profile["counts"]["clicks"] == 0 and profile["topic_weights"] == {} for profile in first)
    last = [profile for profile in profiles if profile["as_of"] == manifest["profile_snapshot_times"][-1]]
    for profile in last:
        user_events = [event for event in events if (event["tenant_id"], event["synthetic_user_id"]) ==
                       (profile["tenant_id"], profile["synthetic_user_id"])]
        assert profile["counts"]["clicks"] == sum(event["event_type"] == "click" for event in user_events)
        assert profile["counts"]["total_duration_seconds"] == sum(event["duration_seconds"] for event in user_events)
        assert datetime.fromisoformat(profile["events_through"]) <= datetime.fromisoformat(profile["as_of"])
        assert sum(profile["channel_counts"].values()) == profile["counts"]["impressions"]
        assert sum(profile["hour_counts"].values()) == profile["counts"]["impressions"]
        assert sum(profile["device_counts"].values()) == profile["counts"]["impressions"]
        assert "北京时间" in profile["active_hour_band"] or profile["active_hour_band"] == "无行为（冷启动）"
        assert all(0 < weight <= 1 for weight in profile["topic_weights"].values())
        if profile["cohort_id"] == "cold_start":
            assert profile["topic_weights"] == {} and profile["counts"]["impressions"] == 0
        if profile["cohort_id"] == "notification_disabled":
            assert profile["notification_allowed"] is False
        if profile["cohort_id"] == "frequency_limited":
            assert profile["frequency_capped"] is True
    assert any("profile.deep_reading" in item["behavior_label_ids"] for item in last)
    assert any("profile.shallow_reading" in item["behavior_label_ids"] for item in last)
    # Adding future events cannot contaminate a historical profile.
    selected = last[0]
    user_events = [event for event in events if (event["tenant_id"], event["synthetic_user_id"]) ==
                   (selected["tenant_id"], selected["synthetic_user_id"])]
    future = {**next(event for event in user_events if event["event_type"] == "click"),
              "occurred_at": (END + timedelta(days=1)).isoformat()}
    assert build.profile_from_events(user_events + [future], as_of=END, root_ids={"tech", "finance"}) == selected


def test_sqlite_matches_jsonl_and_cannot_cross_tenant_by_identity(inputs, tmp_path):
    output = tmp_path / "package"
    manifest = construct(inputs, output)
    with sqlite3.connect((output / "label_index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table, artifact in (("content_labels", "content_labels.jsonl"),
                                ("behavior_labels", "news_behavior_labels.jsonl"),
                                ("profiles", "synthetic_profiles.jsonl")):
            actual = [json.loads(row[0]) for row in connection.execute(f"SELECT payload_json FROM {table}")]
            assert actual == rows(output / artifact)
        stamps = [row[0] for row in connection.execute("SELECT DISTINCT as_of FROM profiles ORDER BY as_of")]
        assert stamps == manifest["profile_snapshot_times"]
        assert connection.execute("SELECT count(*) FROM profiles WHERE tenant_id=?", ("unknown",)).fetchone()[0] == 0
        assert dict(connection.execute("SELECT key,value FROM metadata"))["taxonomy_version"] == "topics-test-v1"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM profiles")


def test_changed_seed_changes_only_synthetic_derivatives_and_identity(inputs, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    first = construct(inputs, one, seed=10)
    second = construct(inputs, two, seed=11)
    assert first["identity_sha256"] != second["identity_sha256"]
    assert (one / "content_labels.jsonl").read_bytes() == (two / "content_labels.jsonl").read_bytes()
    assert (one / "news_behavior_labels.jsonl").read_bytes() == (two / "news_behavior_labels.jsonl").read_bytes()
    assert (one / "synthetic_events.jsonl").read_bytes() != (two / "synthetic_events.jsonl").read_bytes()


def test_output_collision_or_failure_never_overwrites_or_leaves_partial_package(inputs, tmp_path, monkeypatch):
    output = tmp_path / "package"
    construct(inputs, output)
    digest = sha256((output / "manifest.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="new"):
        construct(inputs, output)
    assert sha256((output / "manifest.json").read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match="outside"):
        construct(inputs, inputs[0] / "derived")
    failed = tmp_path / "failed"
    monkeypatch.setattr(build, "_simulate_tenant", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("injected failure")))
    with pytest.raises(ValueError, match="injected"):
        construct(inputs, failed)
    assert not failed.exists() and not list(tmp_path.glob(".failed.building-*"))


@pytest.mark.parametrize("field,value", [("users_per_tenant", True), ("users_per_tenant", 0),
    ("users_per_tenant", 2001), ("days", 0), ("days", 32), ("seed", -1)])
def test_input_budgets_are_checked_before_loading_source(inputs, tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(build, "_read_source", lambda _: pytest.fail("source should not be read"))
    source_dir, policy_path, _ = inputs
    with pytest.raises(ValueError):
        build.build_package(source_dir, tmp_path / "invalid", policy_path=policy_path, **{field: value})


def test_event_budget_failure_is_atomic(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(build, "MAX_EVENTS", 10)
    with pytest.raises(ValueError, match="event limit"):
        construct(inputs, tmp_path / "too-many")
    assert not (tmp_path / "too-many").exists()


def test_behavior_binding_mismatch_is_rejected(inputs, tmp_path, monkeypatch):
    source = inputs[2]
    observations = ({**source.observations[0], "content_sha256": "b" * 64}, *source.observations[1:])
    changed = WarehouseSource(source.articles, observations, source.source_identity, source.stats,
                              source.window_start, source.window_end, source.as_of)
    monkeypatch.setattr(build, "_read_source", lambda _: changed)
    with pytest.raises(ValueError, match="bound"):
        construct(inputs, tmp_path / "mismatch")
    assert not (tmp_path / "mismatch").exists()


def test_repeated_sources_preserve_overlapping_scenario_and_metric_worlds(inputs, tmp_path, monkeypatch):
    source = inputs[2]
    extra = tuple({**source.observations[0], "source_dataset_version": "same-dataset",
        "scenario_id": scenario, "metric_definition_id": definition, "clicks": clicks}
        for scenario, definition, clicks in (("first", "policy-v1", 45), ("second", "policy-v2", 50)))
    merged = WarehouseSource(source.articles, source.observations + extra,
        {**source.source_identity, "sources": ["first", "second"]}, source.stats,
        source.window_start, source.window_end, source.as_of)
    received = []
    def load(paths):
        received.append(paths)
        return merged
    monkeypatch.setattr(build, "_read_merged_sources", load)
    output = tmp_path / "merged"
    manifest = build.build_package([inputs[0], "profile:classic-v1"], output,
        policy_path=inputs[1], users_per_tenant=10)
    assert received and received[0][1] == "profile:classic-v1"
    assert manifest["source_identity"]["sources"] == ["first", "second"]
    with sqlite3.connect(output / "label_index.sqlite") as connection:
        records = connection.execute("SELECT scenario_id,metric_definition_id,payload_json FROM behavior_labels "
            "WHERE source_dataset_version='same-dataset' ORDER BY scenario_id").fetchall()
        assert [(scenario, definition) for scenario, definition, _ in records] == [("first", "policy-v1"), ("second", "policy-v2")]
        assert [json.loads(payload)["clicks"] for _, _, payload in records] == [45, 50]


def test_profile_input_uses_merged_loader_even_when_only_one_source_is_named(inputs, tmp_path, monkeypatch):
    received = []
    monkeypatch.setattr(build, "_read_merged_sources", lambda paths: received.append(paths) or inputs[2])
    build.build_package("profile:classic-v1", tmp_path / "fixture-profile",
                        policy_path=inputs[1], users_per_tenant=1)
    assert received == [["profile:classic-v1"]]
