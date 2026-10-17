"""Contract tests for the independent, entirely synthetic warehouse package."""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest

from simulation.warehouse_simulation import tencent_news as warehouse


ADDITIVE_METRICS = (
    "impressions", "clicks", "total_duration_seconds", "effective_consumptions", "interactions",
    "likes", "comments", "shares", "favorites", "follows", "negative_feedback",
    "video_starts", "video_completions",
)
EXPECTED_TABLES = {
    "dim_publisher", "dim_channel", "dim_news", "window_snapshot", "news_metric_channel_hourly",
    "news_metric_hourly", "news_metric_5min", "news_metric_baseline_hourly", "content_change_event",
    "content_tombstone", "index_cleanup_task", "board_snapshot", "board_item",
}


@pytest.fixture(scope="module")
def config():
    return warehouse.SimulationConfig(news_per_tenant=12, days=4, start_date="2026-10-07", seed=20261014)


@pytest.fixture(scope="module")
def tables(config):
    return {name: list(warehouse.iter_rows(name, config)) for name in EXPECTED_TABLES}


def _hour(value):
    return datetime.fromisoformat(value).replace(minute=0, second=0, microsecond=0).isoformat()


def _bucket_key(row):
    return (row["tenant_id"], row["news_id"], _hour(row["event_time"]), row["snapshot_revision"])


def _table_names():
    return set(warehouse.TABLE_NAMES)


def _digest(rows):
    return sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def test_explicit_table_contract_and_news_dimension_keys(tables):
    assert _table_names() == EXPECTED_TABLES
    dimensions = tables["dim_news"]
    assert len(dimensions) == 24
    assert len({(row["tenant_id"], row["news_id"]) for row in dimensions}) == 24
    required = {"tenant_id", "news_id", "category", "primary_topic", "content_type", "content_format",
                "publisher_id", "content_version", "status", "title", "publish_time"}
    assert all(required <= set(row) for row in dimensions)
    assert {row["content_type"] for row in dimensions} == {"article", "video"}
    assert {row["status"] for row in dimensions} == {"published", "withdrawn", "restricted"}
    assert any(row["status"] == "published" and row["content_version"] == 2 for row in dimensions)
    publishers = {(row["tenant_id"], row["publisher_id"]) for row in tables["dim_publisher"]}
    assert all((row["tenant_id"], row["publisher_id"]) in publishers for row in dimensions)
    assert all(row["synthetic"] is True for row in dimensions)


def test_generated_rows_are_json_friendly_and_keep_no_user_records(tables):
    for rows in tables.values():
        json.dumps(rows, ensure_ascii=False, allow_nan=False)
        for row in rows:
            assert "user_id" not in row and "device_id" not in row and "session_id" not in row
            if "tenant_id" in row:
                assert str(UUID(row["tenant_id"])) == row["tenant_id"]
            for field in ("event_time", "publish_time"):
                if field in row and row[field] is not None:
                    value = datetime.fromisoformat(row[field])
                    assert value.utcoffset() == timedelta(hours=8)


def test_table_counts_cover_two_tenants_all_revisions_and_bounded_five_minute_retention(config, tables):
    news = config.news_per_tenant
    hours = config.days * 24
    expected = {
        "dim_publisher": 12, "dim_channel": 12, "dim_news": news * 2,
        "window_snapshot": (hours + 2) * 2,
        "news_metric_channel_hourly": news * 2 * 3 * (hours + 2),
        "news_metric_hourly": news * 2 * (hours + 2),
        "news_metric_5min": news * 2 * 3 * (12 + 12 + 7),
        "news_metric_baseline_hourly": news * 2 * (hours + 1),
        "content_change_event": news // 2, "content_tombstone": news // 3,
        "index_cleanup_task": news * 2, "board_snapshot": 2, "board_item": 24,
    }
    assert {name: len(rows) for name, rows in tables.items()} == expected


def test_all_news_metrics_reference_a_news_in_the_same_tenant(tables):
    identities = {(row["tenant_id"], row["news_id"]) for row in tables["dim_news"]}
    tenants = {tenant for tenant, _news in identities}
    assert len(tenants) == 2
    for name in ("news_metric_hourly", "news_metric_channel_hourly", "news_metric_5min",
                 "news_metric_baseline_hourly", "content_change_event", "content_tombstone",
                 "index_cleanup_task", "board_item"):
        assert tables[name]
        assert {row["tenant_id"] for row in tables[name]} == tenants
        assert all((row["tenant_id"], row["news_id"]) in identities for row in tables[name])


def test_five_minute_counts_reconcile_with_channel_and_total_hourly(tables):
    fine = defaultdict(lambda: defaultdict(int))
    channel_totals = defaultdict(lambda: defaultdict(int))
    for row in tables["news_metric_5min"]:
        key = (*_bucket_key(row), row["channel_id"])
        assert datetime.fromisoformat(row["event_time"]).minute % 5 == 0
        for metric in ADDITIVE_METRICS:
            fine[key][metric] += row[metric]
    channel_keys = set()
    for row in tables["news_metric_channel_hourly"]:
        key = (*_bucket_key(row), row["channel_id"])
        assert key not in channel_keys
        channel_keys.add(key)
        # Fine-grained traces are deliberately retained only for the late
        # correction and open-window cases, rather than the whole history.
        if key in fine:
            assert dict(fine[key]) == {metric: row[metric] for metric in ADDITIVE_METRICS}
        for metric in ADDITIVE_METRICS:
            channel_totals[_bucket_key(row)][metric] += row[metric]
    assert set(fine) <= channel_keys
    assert len(fine) == 24 * 3 * 3
    hourly_keys = set()
    for row in tables["news_metric_hourly"]:
        key = _bucket_key(row)
        assert key not in hourly_keys
        hourly_keys.add(key)
        assert dict(channel_totals[key]) == {metric: row[metric] for metric in ADDITIVE_METRICS}
        assert 0 <= row["unique_users"] <= row["clicks"] <= row["impressions"]
        assert 0 <= row["effective_consumptions"] <= row["clicks"]
        assert 0 <= row["video_completions"] <= row["video_starts"]
    assert hourly_keys == set(channel_totals)


def test_unique_users_use_union_instead_of_summing_channels_or_time_slices(tables):
    channels = defaultdict(list)
    slices = defaultdict(list)
    for row in tables["news_metric_channel_hourly"]:
        channels[_bucket_key(row)].append(row["unique_users"])
    for row in tables["news_metric_5min"]:
        slices[(*_bucket_key(row), row["channel_id"])].append(row["unique_users"])
    channel_overlap = slice_overlap = False
    for row in tables["news_metric_hourly"]:
        values = channels[_bucket_key(row)]
        assert max(values) <= row["unique_users"] <= sum(values)
        channel_overlap |= row["unique_users"] < sum(values)
    for row in tables["news_metric_channel_hourly"]:
        key = (*_bucket_key(row), row["channel_id"])
        if key not in slices:
            continue
        values = slices[key]
        assert max(values) <= row["unique_users"] <= sum(values)
        slice_overlap |= row["unique_users"] < sum(values)
    assert channel_overlap and slice_overlap


def test_repeated_generation_has_identical_complete_row_hashes(config, tables):
    for name in EXPECTED_TABLES:
        assert _digest(list(warehouse.iter_rows(name, config))) == _digest(tables[name])


def test_unknown_table_is_rejected(config):
    with pytest.raises(ValueError):
        list(warehouse.iter_rows("enterprise_private_user_events", config))


def test_metric_revisions_bind_to_complete_watermark_and_open_window_contract(config, tables):
    snapshots = {(row["tenant_id"], row["event_time"], row["snapshot_revision"]): row
                 for row in tables["window_snapshot"]}
    assert len(snapshots) == 2 * (config.days * 24 + 2)
    last = (config.end - timedelta(hours=1)).isoformat()
    for tenant in warehouse.TENANTS:
        before, after = snapshots[(tenant, last, 1)], snapshots[(tenant, last, 2)]
        assert before["completeness"] == "incomplete" and before["quality_status"] == "pending"
        assert after["completeness"] == "corrected" and after["quality_status"] == "passed"
        assert after["snapshot_at"] > before["snapshot_at"]
        open_row = snapshots[(tenant, config.end.isoformat(), 1)]
        assert open_row["completeness"] == "incomplete" and open_row["quality_status"] == "pending"
        assert (tenant, config.end.isoformat(), 2) not in snapshots
    for row in snapshots.values():
        assert datetime.fromisoformat(row["window_end"]) - datetime.fromisoformat(row["event_time"]) == timedelta(hours=1)
        watermark = datetime.fromisoformat(row["watermark_at"])
        assert datetime.fromisoformat(row["snapshot_at"]) >= watermark
        end = datetime.fromisoformat(row["window_end"])
        if row["completeness"] == "incomplete":
            assert watermark < end
        else:
            assert watermark >= end
    for name in ("news_metric_hourly", "news_metric_channel_hourly", "news_metric_5min"):
        for row in tables[name]:
            tenant, _news, event_time, revision = _bucket_key(row)
            assert (tenant, event_time, revision) in snapshots


def test_late_correction_matches_declared_added_clicks_and_bin_count(config, tables):
    last = (config.end - timedelta(hours=1)).isoformat()
    hourly = {(row["tenant_id"], row["news_id"], row["snapshot_revision"]): row
              for row in tables["news_metric_hourly"] if row["event_time"] == last}
    bins = {(row["tenant_id"], row["news_id"], row["channel_id"], row["event_time"], row["snapshot_revision"]): row
            for row in tables["news_metric_5min"] if _hour(row["event_time"]) == last}
    for snapshot in tables["window_snapshot"]:
        if snapshot["event_time"] != last or snapshot["snapshot_revision"] != 2:
            continue
        tenant = snapshot["tenant_id"]
        deltas = [row["clicks"] - hourly[(tenant, news, 1)]["clicks"]
                  for (row_tenant, news, revision), row in hourly.items() if row_tenant == tenant and revision == 2]
        assert deltas == [63] * config.news_per_tenant
        assert sum(deltas) == snapshot["late_added_clicks"]
        changed = 0
        for (row_tenant, news, channel, stamp, revision), row in bins.items():
            if row_tenant == tenant and revision == 2:
                delta = row["clicks"] - bins[(tenant, news, channel, stamp, 1)]["clicks"]
                assert delta in (0, 7)
                changed += int(delta > 0)
        assert changed == snapshot["late_corrected_bins"]


def test_baselines_recompute_prior_three_days_same_hour_without_future_or_open_data(config, tables):
    hourly = {(row["tenant_id"], row["news_id"], row["event_time"], row["snapshot_revision"]): row
              for row in tables["news_metric_hourly"]}
    snapshots = {(row["tenant_id"], row["event_time"], row["snapshot_revision"]): row
                 for row in tables["window_snapshot"]}
    metrics = ("impressions", "clicks", "effective_consumptions", "interactions")
    first_day = first_open = known_zero = False
    for baseline in tables["news_metric_baseline_hourly"]:
        current = datetime.fromisoformat(baseline["event_time"])
        prior = []
        for offset in range(1, 4):
            historical = current - timedelta(days=offset)
            if historical < config.start:
                continue
            key = (baseline["tenant_id"], baseline["news_id"], historical.isoformat(), 1)
            assert snapshots[(key[0], key[2], key[3])]["completeness"] == "complete"
            prior.append(hourly[key])
        assert baseline["sample_count"] == len(prior)
        assert baseline["baseline_version"] == "sim-prior-3-days-same-hour-v1"
        for metric in metrics:
            actual = baseline[f"baseline_{metric}"]
            if not prior:
                assert actual is None
                first_day = True
            else:
                expected = (Decimal(sum(row[metric] for row in prior)) / len(prior)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                assert Decimal(actual) == expected
                known_zero |= actual == "0.00"
        first_open |= current == config.end and baseline["sample_count"] == 3
    assert first_day and first_open and known_zero


def test_lifecycle_tombstones_and_cleanup_tasks_are_scoped_and_only_pending(tables):
    dimensions = {(row["tenant_id"], row["news_id"]): row for row in tables["dim_news"]}
    events = {(row["tenant_id"], row["event_id"]): row for row in tables["content_change_event"]}
    assert {row["event_type"] for row in events.values()} == {"withdraw", "restrict", "correct"}
    expected_tombstones = {(row["tenant_id"], row["news_id"], row["event_id"])
                           for row in events.values() if row["event_type"] != "correct"}
    assert {(row["tenant_id"], row["news_id"], row["event_id"])
            for row in tables["content_tombstone"]} == expected_tombstones
    for tombstone in tables["content_tombstone"]:
        event = events[(tombstone["tenant_id"], tombstone["event_id"])]
        assert tombstone["status"] in ("withdrawn", "restricted")
        assert tombstone["blocked_through_version"] == event["to_version"]
        assert tombstone["lifecycle_revision"] == event["lifecycle_revision"]
    actions = {"postgres_knowledge": "deactivate_chunks", "milvus": "delete_versioned_chunks",
               "bge_snapshot": "denylist_then_rebuild", "retrieval_cache": "invalidate"}
    assert len(tables["index_cleanup_task"]) == len(events) * len(actions)
    assert len({row["task_id"] for row in tables["index_cleanup_task"]}) == len(tables["index_cleanup_task"])
    tasks_by_event = defaultdict(set)
    for task in tables["index_cleanup_task"]:
        event = events[(task["tenant_id"], task["event_id"])]
        news = dimensions[(task["tenant_id"], task["news_id"])]
        assert task["news_id"] == event["news_id"]
        assert task["status"] == "pending" and task["attempt_count"] == 0 and task["mapping_required"] is True
        assert task["content_source"] == "tencent-news-simulation-v7"
        assert task["action"] == actions[task["target"]]
        assert task["lifecycle_revision"] == event["lifecycle_revision"]
        expected_version = event["from_version"] if event["event_type"] == "correct" else event["to_version"]
        assert task["through_version"] == expected_version <= news["content_version"]
        if event["event_type"] == "correct":
            assert task["through_version"] < news["content_version"]
        tasks_by_event[(task["tenant_id"], task["event_id"])].add(task["target"])
    assert all(targets == set(actions) for targets in tasks_by_event.values())


def test_original_board_retains_ties_positions_and_rank_gaps_after_eligibility_filter(tables):
    snapshots = {(row["tenant_id"], row["board_id"], row["snapshot_id"]): row for row in tables["board_snapshot"]}
    grouped = defaultdict(list)
    status = {(row["tenant_id"], row["news_id"]): row["status"] for row in tables["dim_news"]}
    for row in tables["board_item"]:
        key = (row["tenant_id"], row["board_id"], row["snapshot_id"])
        assert key in snapshots
        assert snapshots[key]["source_kind"] == "synthetic_original_board"
        grouped[key].append(row)
    for items in grouped.values():
        assert [row["source_position"] for row in items] == list(range(1, 13))
        assert [row["source_rank"] for row in items][:4] == [1, 2, 2, 4]
        assert all(Decimal(left["source_score"]) > Decimal(right["source_score"])
                   for left, right in zip(items, items[1:]))
        eligible = [row for row in items if status[(row["tenant_id"], row["news_id"])] == "published"]
        assert len(eligible) < len(items)
        assert max(row["source_rank"] for row in eligible) > len(eligible)


@pytest.mark.parametrize("intervals,expected", [([], 0), ([(0, 10), (5, 15)], 15),
                                               ([(5, 10), (5, 10), (10, 12)], 7),
                                               ([(20, 25), (0, 5)], 10), ([(0, 0)], 0)])
def test_audience_union_handles_overlap_duplicates_and_disjoint_ranges(intervals, expected):
    assert warehouse.union_size(intervals) == expected


@pytest.mark.parametrize("updates", [{"news_per_tenant": True}, {"news_per_tenant": 13},
                                      {"news_per_tenant": 0}, {"days": 0}, {"days": 31},
                                      {"seed": -1}, {"seed": False}, {"start_date": "2026-2-01"},
                                      {"start_date": "2026-02-30"}])
def test_invalid_config_is_rejected(updates):
    with pytest.raises(ValueError):
        warehouse.SimulationConfig(**updates)


@pytest.mark.parametrize("value,expected", [(None, r"\N"), (True, "t"), (False, "f"),
                                           ("\\N", r"\\N"), ("\\.", r"\\."),
                                           ("a\tb\nc\rd", r"a\tb\nc\rd"),
                                           ("x'; DROP TABLE dw.dim_news; --", "x'; DROP TABLE dw.dim_news; --")])
def test_sql_copy_encoding_keeps_nulls_and_untrusted_text_as_data(value, expected):
    encoded = warehouse.copy_value(value)
    assert encoded == expected
    assert "\t" not in encoded and "\n" not in encoded and "\r" not in encoded
    if value is not None:
        assert encoded != r"\N" and encoded != r"\."


def test_export_is_deterministic_hash_bound_and_never_overwrites_existing_output(tmp_path):
    config = warehouse.SimulationConfig(news_per_tenant=12, days=1)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first = warehouse.export_dataset(config, first_dir)
    second = warehouse.export_dataset(config, second_dir)
    assert first == second
    assert first["schema"] == "news_sim_v7" and first["synthetic"] is True
    assert first["integration_status"] == "standalone_fixture_not_connected_to_agent_or_vector_store"
    assert set(first["tables"]) == EXPECTED_TABLES
    saved = json.loads((first_dir / "manifest.json").read_text())
    assert saved == first
    assert sha256((first_dir / "generator.py").read_bytes()).hexdigest() == first["generator_sha256"]
    assert sha256((first_dir / "schema.sql").read_bytes()).hexdigest() == first["schema_sha256"]
    identity = {name: value for name, value in first.items() if name != "dataset_sha256"}
    assert _digest(identity) == first["dataset_sha256"]
    for name in EXPECTED_TABLES:
        encoded = (first_dir / f"{name}.jsonl").read_bytes()
        assert sha256(encoded).hexdigest() == first["tables"][name]["sha256"]
        assert len(encoded.splitlines()) == first["tables"][name]["rows"]
    sql = (first_dir / "warehouse.sql").read_bytes()
    assert sha256(sql).hexdigest() == first["warehouse_sql_sha256"]
    assert sql.startswith(b"\\set ON_ERROR_STOP on\nBEGIN;") and sql.endswith(b"COMMIT;\n")
    assert b"COPY news_sim_v7.news_metric_hourly" in sql
    assert b"COPY dw." not in sql and b"DROP SCHEMA dw" not in sql
    before = {path.name: sha256(path.read_bytes()).hexdigest() for path in first_dir.iterdir()}
    with pytest.raises(FileExistsError):
        warehouse.export_dataset(config, first_dir)
    assert before == {path.name: sha256(path.read_bytes()).hexdigest() for path in first_dir.iterdir()}


def test_seed_changes_metrics_but_keeps_news_ids_and_tenant_scopes():
    first = warehouse.SimulationConfig(news_per_tenant=12, days=1, seed=1)
    second = warehouse.SimulationConfig(news_per_tenant=12, days=1, seed=2)
    assert list(warehouse.iter_rows("dim_news", first)) == list(warehouse.iter_rows("dim_news", second))
    assert _digest(list(warehouse.iter_rows("news_metric_hourly", first))) != _digest(
        list(warehouse.iter_rows("news_metric_hourly", second)))


def test_export_escapes_a_title_that_looks_like_copy_termination_and_sql(tmp_path, monkeypatch):
    original = warehouse.iter_rows
    payload = "\\.\nDROP SCHEMA dw CASCADE;\tSELECT secret;\r\\N"

    def rows(table, config):
        for row in original(table, config):
            if table == "dim_news":
                row = {**row, "title": payload}
            yield row

    monkeypatch.setattr(warehouse, "iter_rows", rows)
    output = tmp_path / "escaped"
    warehouse.export_dataset(warehouse.SimulationConfig(news_per_tenant=12, days=1), output)
    title = json.loads((output / "dim_news.jsonl").read_text().splitlines()[0])["title"]
    assert title == payload
    sql = (output / "warehouse.sql").read_text()
    assert warehouse.copy_value(payload) in sql
    assert "\nDROP SCHEMA dw CASCADE;" not in sql


def test_export_failure_leaves_no_partial_dataset_or_temporary_directory(tmp_path, monkeypatch):
    original = warehouse.iter_rows

    def broken_rows(table, config):
        for row in original(table, config):
            yield row
            if table == "dim_channel":
                raise OSError("simulated generation interruption")

    monkeypatch.setattr(warehouse, "iter_rows", broken_rows)
    output = tmp_path / "failed"
    with pytest.raises(OSError, match="generation interruption"):
        warehouse.export_dataset(warehouse.SimulationConfig(news_per_tenant=12, days=1), output)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_export_does_not_replace_a_dangling_symlink_or_existing_file(tmp_path):
    config = warehouse.SimulationConfig(news_per_tenant=12, days=1)
    link = tmp_path / "existing-link"
    destination = tmp_path / "not-created"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError):
        warehouse.export_dataset(config, link)
    assert link.is_symlink() and link.readlink() == destination
    assert not destination.exists()
    existing = tmp_path / "existing-file"
    existing.write_text("preserve this artifact")
    with pytest.raises(FileExistsError):
        warehouse.export_dataset(config, existing)
    assert existing.read_text() == "preserve this artifact"


def test_export_does_not_overwrite_another_exporter_claiming_the_output_during_generation(tmp_path, monkeypatch):
    output = tmp_path / "racing-export"
    original = Path.mkdir

    def claim_first(path, *args, **kwargs):
        if path == output:
            original(path)
            (path / "other-exporter.txt").write_text("preserve the winning artifact")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", claim_first)
    with pytest.raises(FileExistsError):
        warehouse.export_dataset(warehouse.SimulationConfig(news_per_tenant=12, days=1), output)
    assert (output / "other-exporter.txt").read_text() == "preserve the winning artifact"
    assert sorted(path.name for path in output.iterdir()) == ["other-exporter.txt"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["racing-export"]


def test_export_rename_failure_cleans_its_exclusive_output_reservation(tmp_path, monkeypatch):
    output = tmp_path / "rename-failure"
    original = Path.rename

    def fail_commit(path, target):
        if target == output:
            raise OSError("simulated atomic commit failure")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", fail_commit)
    with pytest.raises(OSError, match="atomic commit failure"):
        warehouse.export_dataset(warehouse.SimulationConfig(news_per_tenant=12, days=1), output)
    assert not output.exists() and list(tmp_path.iterdir()) == []
