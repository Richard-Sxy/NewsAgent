"""Fixtures prove distinct readers and pre-window, article-free interests."""
from copy import deepcopy
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from app.behavior_tags.build import GENERATOR_VERSION
from app.behavior_tags.tagging import load_topic_policy
from app.reader_profiles import build

TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "33333333-3333-4333-8333-333333333333"
START = datetime(2026, 10, 10, tzinfo=UTC)
POLICY = Path(__file__).resolve().parents[1] / "deploy/behavior-topic-policy.local.yml"


def stamp(hour=0):
    return (START + timedelta(hours=hour)).isoformat()


def event(user, hour, kind, news=None, *, tenant=TENANT, sequence=0):
    topics = {"A": ["tech"], "B": ["finance"], "C": ["ai"], "D": ["military"]}
    return dict(tenant_id=tenant, synthetic_user_id="synthetic-" + user,
        event_id="synthetic-event-" + sha256(f"{tenant}|{user}|{hour}|{kind}|{news}|{sequence}".encode()).hexdigest(),
        occurred_at=stamp(hour), event_type=kind, news_id=news, topic_ids=topics.get(news, []),
        content_sha256=sha256((news or "").encode()).hexdigest() if news else None,
        duration_seconds=60 if kind == "detail_view" else 0, effective_read=kind == "detail_view",
        source_mode="synthetic", generator_version=GENERATOR_VERSION, not_reconstructed_from_aggregate_uv=True)


def fixture_events():
    return [event("u1", 0, "profile_initialized"), event("u2", 0, "profile_initialized"),
        event("u4", 0, "profile_initialized"), event("u5", 0, "profile_initialized"),
        event("u1", 1, "click", "B"), event("u1", 49, "click", "C"),
        event("u1", 49.5, "click", "A"), event("u2", 49.3, "click", "A"),
        event("u3", 49, "click", "B"), event("u4", 49, "click", "B"), event("u5", 49, "click", "B"),
        event("u1", 50, "click", "D"),
        event("u1", 50.1, "detail_view", "A"), event("u1", 50.2, "detail_view", "A"),
        event("u2", 50.3, "detail_view", "A"), event("u3", 50.4, "detail_view", "A"),
        event("u4", 51, "detail_view", "A"), event("other", 0, "profile_initialized", tenant=OTHER),
        event("other", 49, "click", "B", tenant=OTHER), event("other", 50.2, "detail_view", "A", tenant=OTHER)]


def write_source(directory, events=None, *, end_hours=72):
    directory.mkdir(exist_ok=True)
    policy = load_topic_policy(POLICY)
    contents = [dict(tenant_id=tenant, news_id=news, content_sha256=sha256(news.encode()).hexdigest(), topic_ids=topics)
        for tenant in (TENANT, OTHER) for news, topics in (("A", ["tech"]), ("B", ["finance"]), ("C", ["ai"]), ("D", ["military"]))]
    events = fixture_events() if events is None else events
    artifacts = {}
    for name, rows in (("content_labels.jsonl", contents), ("synthetic_events.jsonl", events)):
        payload = b"".join((build._json(row) + "\n").encode() for row in rows)
        (directory / name).write_bytes(payload)
        artifacts[name] = dict(bytes=len(payload), rows=len(rows), sha256=sha256(payload).hexdigest())
    payload = (build._json(policy.model_dump(mode="json")) + "\n").encode()
    (directory / "policy.json").write_bytes(payload)
    artifacts["policy.json"] = dict(bytes=len(payload), sha256=sha256(payload).hexdigest())
    manifest = dict(schema_version=1, package_version="behavior-tags-local-v1", generator_version=GENERATOR_VERSION,
        tenants=[TENANT, OTHER], simulation_window=dict(window_start=stamp(), window_end=stamp(end_hours)),
        build_as_of=stamp(end_hours + 1), taxonomy_version=policy.catalog.taxonomy_version, policy_version=policy.policy_version,
        artifacts=artifacts)
    seal(directory, manifest)
    return directory


def seal(directory, manifest):
    manifest.pop("identity_sha256", None)
    manifest["identity_sha256"] = sha256(build._json(manifest).encode()).hexdigest()
    (directory / "manifest.json").write_text(build._json(manifest) + "\n", encoding="utf-8")


def get_row(package, hour=50, *, tenant=TENANT):
    with sqlite3.connect(package / "reader_profiles.sqlite") as connection:
        row = connection.execute("SELECT payload_json FROM reader_profiles WHERE tenant_id=? AND news_id=? AND window_start=?",
            (tenant, "A", stamp(hour))).fetchone()
    return json.loads(row[0])


def test_distinct_readers_feature_cutoff_trigger_exclusion_and_denominators(tmp_path):
    source = write_source(tmp_path / "source")
    before = {path.name: build._hash_file(path) for path in source.iterdir()}
    package = tmp_path / "reader"
    manifest = build.build_package(source, package)
    row = get_row(package)
    assert row["reader_count"] == 3
    assert row["content_topic_ids"] == ["tech"] and build._time(row["profile_cutoff"]) == build._time(stamp(50))
    long, short = row["long_term"], row["short_term"]
    assert long["profiled_reader_count"] == short["profiled_reader_count"] == 1
    assert long["unknown_reader_count"] == short["unknown_reader_count"] == 2
    assert long["cold_start_reader_count"] == short["cold_start_reader_count"] == 1
    assert {item["topic_id"] for item in long["topics"]} == {"finance", "ai"}
    assert {item["topic_id"] for item in short["topics"]} == {"ai"}
    assert "tech" not in {item["topic_id"] for item in long["topics"]}
    assert "military" not in {item["topic_id"] for item in long["topics"]}
    finance = next(item for item in long["topics"] if item["topic_id"] == "finance")
    assert finance["share_all"] == "0.33333333" and finance["share_profiled"] == "1.00000000"
    assert finance["baseline_reader_count"] == finance["baseline_profiled_count"] == 3
    assert finance["baseline_share"] == "1.00000000" and finance["lift"] is None and finance["low_sample"]
    assert get_row(package, tenant=OTHER)["reader_count"] == 1
    assert manifest["counts"]["covered_windows"] == 144
    assert {path.name: build._hash_file(path) for path in source.iterdir()} == before
    assert {path.name for path in package.iterdir()} == {"manifest.json", "policy.json", "reader_profiles.sqlite"}
    with sqlite3.connect(package / "reader_profiles.sqlite") as connection:
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "metadata", "contents", "coverage", "reader_profiles"}
        assert not any("synthetic-u1" in row[0] for row in connection.execute("SELECT payload_json FROM reader_profiles"))


def test_twenty_four_hour_counts_are_recomputed_not_summed(tmp_path):
    source = write_source(tmp_path / "source")
    package = tmp_path / "daily"
    build.build_package(source, package, window_hours=24)
    row = get_row(package, 48)
    assert row["reader_count"] == 4
    assert row["long_term"]["profiled_reader_count"] == 1
    assert row["short_term"]["profiled_reader_count"] == 0


def test_negative_trigger_contribution_exclusion_can_restore_other_interest():
    events = [(START, "u", "B", "click", ("finance",), 1.0),
        (START, "u", "A", "negative_feedback", ("finance",), -3.0)]
    state = build._RollingState(events, 24, START)
    state.advance(START + timedelta(hours=1))
    assert state.profile("u", True)[1] is False
    assert state.profile("u", True, "A")[:2] == (frozenset({"finance"}), True)


def test_supported_lift_uses_profiled_denominator_and_rounded_baseline(tmp_path):
    rows = []
    for index in range(10):
        user = f"sample-{index}"
        rows.extend((event(user, 0, "profile_initialized"),
            event(user, 49, "click", "B" if index < 5 else "C")))
        if index < 5:
            rows.append(event(user, 50.2, "detail_view", "A"))
    source = write_source(tmp_path / "source", rows)
    target = tmp_path / "reader"
    build.build_package(source, target)
    topic = get_row(target)["long_term"]["topics"][0]
    assert topic["reader_count"] == 5 and topic["baseline_profiled_count"] == 10
    assert topic["share_profiled"] == "1.00000000" and topic["baseline_share"] == "0.50000000"
    assert topic["lift"] == "2.00000000" and topic["low_sample"] is False


def test_sliding_history_lower_bound_and_expiration():
    rows = [(START, "u", "B", "click", ("finance",), 1.0)]
    state = build._RollingState(rows, 24, START)
    state.advance(START + timedelta(hours=24))
    assert state.profile("u", True)[1]
    state.advance(START + timedelta(hours=25))
    assert state.profile("u", True) == (frozenset(), False, True)


@pytest.mark.parametrize("name", ["policy.json", "content_labels.jsonl", "synthetic_events.jsonl"])
def test_required_input_hashes_are_verified_without_reading_other_big_artifacts(tmp_path, name):
    source = write_source(tmp_path / "source")
    path = source / name
    original = path.read_bytes()
    path.write_bytes(original[:-1] + (b" " if original[-1:] != b" " else b"\n"))
    with pytest.raises(ValueError):
        build.build_package(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("change", [
    lambda rows: rows[1].update(source_mode="enterprise"),
    lambda rows: rows[1].update(synthetic_user_id="real-user"),
    lambda rows: rows[1].update(not_reconstructed_from_aggregate_uv=False),
    lambda rows: rows.append(deepcopy(rows[0])),
    lambda rows: rows[-1].update(content_sha256="f" * 64),
    lambda rows: rows[-1].update(occurred_at=stamp(72)),
    lambda rows: rows[-1].update(duration_seconds=-1),
    lambda rows: rows[-1].update(topic_ids=["finance"]),
])
def test_resealed_invalid_events_are_rejected(tmp_path, change):
    rows = fixture_events()
    change(rows)
    source = write_source(tmp_path / "source", rows)
    with pytest.raises(ValueError):
        build.build_package(source, tmp_path / "out")


def test_top_k_is_applied_after_complete_local_statistics(tmp_path):
    source = write_source(tmp_path / "source")
    package = tmp_path / "limited"
    build.build_package(source, package, top_k=1)
    row = get_row(package)
    assert row["long_term"]["profiled_reader_count"] == 1
    assert len(row["long_term"]["topics"]) == 1
    assert row["long_term"]["topics"][0]["reader_count"] == 1


@pytest.mark.parametrize("options", [dict(window_hours=2), dict(top_k=0), dict(top_k=21), dict(window_hours=True)])
def test_invalid_build_options(tmp_path, options):
    with pytest.raises(ValueError):
        build.build_package(tmp_path / "missing", tmp_path / "out", **options)


def test_existing_output_is_not_overwritten(tmp_path):
    package = tmp_path / "existing"
    package.mkdir()
    (package / "keep").write_text("user file")
    with pytest.raises(ValueError, match="already exists"):
        build.build_package(tmp_path / "missing", package)
    assert (package / "keep").read_text() == "user file"
