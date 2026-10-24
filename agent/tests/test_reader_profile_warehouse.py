"""Frozen aggregate projection preserves causal snapshots, scopes and TopK limits."""

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import sqlite3

import pytest

from app.modules.reader_profiles import build
from app.modules.reader_profiles.contracts import ReaderProfileQuery
from app.modules.reader_profiles.local import LocalNewsReaderProfileClient
from app.modules.reader_profiles.warehouse import ReaderWarehouseSource, snapshot_id
from tests.test_reader_profile_build import OTHER, TENANT, get_row, seal, stamp, write_source


@pytest.fixture
def package(tmp_path):
    source = write_source(tmp_path / "source")
    target = tmp_path / "readers"
    build.build_package(source, target)
    return target


def reseal_database(package):
    """Permit semantic corruption cases to pass the outer artifact checksum."""
    path = package / "reader_profiles.sqlite"
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["artifacts"][path.name] = {"bytes": path.stat().st_size, "sha256": build._hash_file(path)}
    seal(package, manifest)


def target_snapshot(source, tenant=TENANT, hour=50):
    return source.snapshots[tenant, stamp(hour), stamp(hour + 1)]


def target_populations(source, tenant=TENANT, hour=50):
    snap = target_snapshot(source, tenant, hour)
    return {row["lookback_hours"]: row for row in source.population_rows()
            if row["tenant_id"] == tenant and row["news_id"] == "A"
            and row["snapshot_id"] == snap["snapshot_id"]}


def test_snapshot_matches_local_client_and_is_independent_of_requested_news(package):
    source = ReaderWarehouseSource(package)
    client = LocalNewsReaderProfileClient(package, environment="test")
    query = ReaderProfileQuery(tenant_id=TENANT, news_ids=("A",), window_start=stamp(50),
        window_end=stamp(51), source_release_sha256=client.source_release_sha256)
    actual = client._fetch_sync(query)
    other = client._fetch_sync(query.model_copy(update={"news_ids": ("B",)}))
    expected = target_snapshot(source)
    assert source.release == sha256((package / "manifest.json").read_bytes()).hexdigest()
    assert expected["snapshot_id"] == actual.snapshot_id == other.snapshot_id
    assert snapshot_id(source.release, TENANT, datetime.fromisoformat(stamp(50)),
        datetime.fromisoformat(stamp(51)), 1) == actual.snapshot_id
    assert target_snapshot(source, OTHER)["snapshot_id"] != actual.snapshot_id
    assert snapshot_id("f" * 64, TENANT, stamp(50), stamp(51), 1) != actual.snapshot_id
    assert snapshot_id(source.release, TENANT, stamp(50), stamp(51), 2) != actual.snapshot_id


def test_coverage_and_content_bindings_remain_separate_from_nonempty_populations(package):
    source = ReaderWarehouseSource(package)
    assert len(source.snapshots) == 144
    assert len(source.bindings) == 8
    snapshot = target_snapshot(source)
    population = list(source.population_rows())
    # B is a legal bound article with no visitors at hour 50, not an invented row of zeros.
    assert (TENANT, "B") in source.bindings
    assert not any(row["tenant_id"] == TENANT and row["news_id"] == "B"
                   and row["snapshot_id"] == snapshot["snapshot_id"] for row in population)
    assert (TENANT, "unknown") not in source.bindings
    assert (TENANT, stamp(72), stamp(73)) not in source.snapshots
    assert target_snapshot(source, hour=0)["snapshot_id"] not in {row["snapshot_id"] for row in population}
    assert len(population) == source.manifest["counts"]["reader_profiles"] * 2


def test_population_and_topic_projection_preserve_distinct_readers_and_unknowns(package):
    source = ReaderWarehouseSource(package)
    original = get_row(package)
    rows = target_populations(source)
    assert set(rows) == {24, 720}
    for hours, label in ((24, "short_term"), (720, "long_term")):
        row, dimension = rows[hours], original[label]
        assert row["reader_count"] == 3
        assert row["profiled_reader_count"] == 1
        assert row["unknown_reader_count"] == 2
        assert row["cold_start_reader_count"] == 1
        assert row["content_sha256"] == original["content_sha256"]
        assert row["source_has_body_hash"] is True
        assert row["baseline_profiled_count"] == dimension["baseline_profiled_count"]
        assert row["status"] == "complete" and row["trigger_article_excluded"] is True
        assert row["source_topic_count"] == len(dimension["topics"])
    snap = target_snapshot(source)
    projected = [row for row in source.topic_rows() if row["tenant_id"] == TENANT
                 and row["news_id"] == "A" and row["snapshot_id"] == snap["snapshot_id"]]
    assert {(row["lookback_hours"], row["topic_id"]) for row in projected} == {
        (720, "ai"), (720, "finance"), (24, "ai")}
    for hours, label in ((720, "long_term"), (24, "short_term")):
        for rank, item in enumerate(original[label]["topics"], 1):
            row = next(row for row in projected if row["lookback_hours"] == hours and row["topic_id"] == item["topic_id"])
            assert row["source_rank"] == rank
            assert row["topic_reader_count"] == item["reader_count"]
            assert row["baseline_topic_count"] == item["baseline_reader_count"]
    assert target_populations(source, OTHER)[720]["reader_count"] == 1


def test_as_of_records_package_freeze_and_history_truncation_separately(package):
    source = ReaderWarehouseSource(package)
    snapshot = target_snapshot(source)
    assert snapshot["profile_cutoff"] == stamp(50)
    assert snapshot["knowledge_as_of"] == source.manifest["generated_at"] == stamp(73)
    assert snapshot["knowledge_as_of_basis"] == "synthetic_package_frozen_at"
    assert snapshot["history_start"] == stamp(0)
    assert snapshot["watermark_at"] == stamp(72)
    assert snapshot["quality_basis"] == "local_import_contract_validation"
    assert snapshot["source_mode"] == "synthetic" and snapshot["expires_at"] is None
    rows = target_populations(source)
    assert any("history is truncated" in note for note in rows[720]["limitations"])
    assert not any("history is truncated" in note for note in rows[24]["limitations"])
    assert all(any("omitted topics are not zero" in note for note in row["limitations"]) for row in rows.values())


def test_topk_projection_does_not_fill_omitted_topics_with_zeros(tmp_path):
    source_path = write_source(tmp_path / "source")
    package = tmp_path / "top-one"
    build.build_package(source_path, package, top_k=1)
    source = ReaderWarehouseSource(package)
    populations = target_populations(source)
    snap = target_snapshot(source)
    topics = [row for row in source.topic_rows() if row["tenant_id"] == TENANT and row["news_id"] == "A"
              and row["snapshot_id"] == snap["snapshot_id"]]
    assert {row["topic_id"] for row in topics} == {"ai"}
    assert len(topics) == 2
    assert populations[720]["reader_count"] == 3 and populations[720]["unknown_reader_count"] == 2
    for row in populations.values():
        assert row["topics_are_top_k"] is True
        assert row["source_topic_limit"] == row["source_topic_count"] == 1
        assert any("up to 1 topics" in note for note in row["limitations"])
    assert "finance" in {row["topic_id"] for row in source.dictionary_rows()}
    assert not any(row["topic_id"] == "finance" for row in topics)


def test_projection_retains_frozen_dictionary_and_contains_no_personal_rows(package):
    source = ReaderWarehouseSource(package)
    dictionary = source.dictionary_rows()
    assert len(dictionary) == 84
    assert sum(row["topic_level"] == 1 for row in dictionary) == 12
    assert sum(row["topic_level"] == 2 for row in dictionary) == 72
    assert next(row for row in dictionary if row["topic_id"] == "finance")["topic_name"] == "财经"
    all_rows = [*source.binding_rows(), *source.snapshots.values(), *source.population_rows(), *source.topic_rows()]
    encoded = json.dumps(all_rows, ensure_ascii=False)
    assert "synthetic_user_id" not in encoded and "synthetic-u1" not in encoded and "actor_key" not in encoded
    assert source.verify_unchanged() is None


@pytest.mark.parametrize("field,value", [
    ("news_id", "not-A"), ("content_sha256", "f" * 64),
    ("profile_cutoff", stamp(51)), ("trigger_article_excluded", False),
    ("identity_policy_version", "unapproved-identity"),
    ("reader_definition", "click-distinct"), ("taxonomy_version", "other-topics"),
    ("reader_count", True),
])
def test_resealed_invalid_identity_causality_and_counts_are_rejected(package, field, value):
    row = deepcopy(get_row(package))
    row[field] = value
    with sqlite3.connect(package / "reader_profiles.sqlite") as conn:
        conn.execute("UPDATE reader_profiles SET payload_json=? WHERE tenant_id=? AND news_id=? AND window_start=?",
            (build._json(row), TENANT, "A", stamp(50)))
    reseal_database(package)
    source = ReaderWarehouseSource(package)
    with pytest.raises(ValueError):
        list(source.records())


def test_resealed_unknown_interest_topic_is_rejected(package):
    row = get_row(package)
    row["long_term"]["topics"][0]["topic_id"] = "unknown-topic"
    with sqlite3.connect(package / "reader_profiles.sqlite") as conn:
        conn.execute("UPDATE reader_profiles SET payload_json=? WHERE tenant_id=? AND news_id=? AND window_start=?",
            (build._json(row), TENANT, "A", stamp(50)))
    reseal_database(package)
    with pytest.raises(ValueError):
        list(ReaderWarehouseSource(package).records())


@pytest.mark.parametrize("field,value", [
    ("news_id", "not-A"), ("tenant_id", OTHER), ("synthetic_user_id", "synthetic-private-row"),
])
def test_resealed_binding_rejects_extra_identity_and_personal_fields(package, field, value):
    with sqlite3.connect(package / "reader_profiles.sqlite") as conn:
        raw = conn.execute("SELECT payload_json FROM contents WHERE tenant_id=? AND news_id='A'", (TENANT,)).fetchone()[0]
        payload = json.loads(raw)
        payload[field] = value
        conn.execute("UPDATE contents SET payload_json=? WHERE tenant_id=? AND news_id='A'", (build._json(payload), TENANT))
    reseal_database(package)
    with pytest.raises(ValueError):
        ReaderWarehouseSource(package)


def test_incomplete_source_coverage_is_not_published_as_complete(package):
    with sqlite3.connect(package / "reader_profiles.sqlite") as conn:
        raw = conn.execute("SELECT payload_json FROM coverage WHERE tenant_id=? AND window_start=?", (TENANT, stamp(50))).fetchone()[0]
        payload = json.loads(raw)
        payload["complete"] = False
        conn.execute("UPDATE coverage SET payload_json=? WHERE tenant_id=? AND window_start=?", (build._json(payload), TENANT, stamp(50)))
    reseal_database(package)
    with pytest.raises(ValueError, match="coverage"):
        ReaderWarehouseSource(package)


@pytest.mark.parametrize("name", ["manifest.json", "policy.json", "reader_profiles.sqlite"])
def test_import_source_mutation_detected_before_commit(package, name):
    source = ReaderWarehouseSource(package)
    with (package / name).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="changed during import"):
        source.verify_unchanged()
