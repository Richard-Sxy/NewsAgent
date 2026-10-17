"""Real-content provenance and independent v8 aggregate semantics."""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json

import pytest

from simulation.warehouse_simulation import real_news as warehouse


@pytest.fixture(scope="module")
def config():
    return warehouse.RealNewsSimulationConfig("2026-09-30T00:00:00+08:00", 50, 20261014)


@pytest.fixture(scope="module")
def catalog(config):
    values = []
    for index in range(12):
        published = (config.start - timedelta(days=1) if index < 9 else
                     config.start + timedelta(hours=12, minutes=18, seconds=15) if index == 9 else
                     config.end - timedelta(minutes=57) if index == 10 else config.end + timedelta(minutes=40))
        values.append({"news_id": f"20260930A{index:07d}", "title": f"已核对公开新闻测试引用{index}",
                       "content": f"用于测试字段保留的公开内容引用{index}。" * 20,
                       "url": f"https://news.qq.com/rain/a/20260930A{index:07d}",
                       "publish_time": published.isoformat(), "publish_time_raw": published.isoformat(),
                       "category": "科技", "content_version": 2, "news_content_synthetic": False})
    return tuple(values)


@pytest.fixture(scope="module")
def tables(config, catalog):
    rows = {name: [] for name in warehouse.TABLE_NAMES}
    for name, row in warehouse._dimension_rows(catalog, config):
        rows[name].append(row)
    for tenant in warehouse.TENANTS:
        for index, article in enumerate(catalog):
            for name, row in warehouse._article_rows(article, index, tenant, config):
                rows[name].append(row)
    return rows


def _hour(value):
    return datetime.fromisoformat(value).replace(minute=0, second=0, microsecond=0).isoformat()


def _hour_key(row):
    return row["tenant_id"], row["news_id"], _hour(row["event_time"]), row["snapshot_revision"]


def test_default_configuration_and_catalog_have_real_distinct_articles():
    config = warehouse.RealNewsSimulationConfig()
    assert config.end.isoformat() == "2026-10-14T20:00:00+08:00"
    articles = warehouse._catalog()
    assert len(articles) == len({item["news_id"] for item in articles}) == 661
    assert all(item["news_content_synthetic"] is False and item["content_type"] == "article" for item in articles)
    assert len({item["category"] for item in articles}) == 7


@pytest.mark.parametrize("args", [("2026-09-30", 50, 1), ("2026-09-30T00:00:00Z", 50, 1),
                                  ("2026-09-30T00:01:00+08:00", 50, 1),
                                  ("2026-09-30T00:00:00+08:00", True, 1),
                                  ("2026-09-30T00:00:00+08:00", 0, 1),
                                  ("2026-09-30T00:00:00+08:00", 50, -1)])
def test_invalid_config_is_rejected(args):
    with pytest.raises(ValueError):
        warehouse.RealNewsSimulationConfig(*args)


def test_source_fields_stay_real_and_main_has_no_fictional_lifecycle(tables, catalog):
    source = {item["news_id"]: item for item in catalog}
    assert len(tables["dim_news"]) == len(catalog) * 2
    for row in tables["dim_news"]:
        original = source[row["news_id"]]
        for field in ("title", "content", "publish_time", "publish_time_raw", "content_version"):
            assert row[field] == original[field]
        assert row["source_url"] == original["url"]
        assert row["news_content_synthetic"] is False and row["metric_data_synthetic"] is True
        if row["tenant_id"] == warehouse.TENANTS[0]:
            assert row["status"] == "published" and row["lifecycle_scenario"] == "none"
            assert row["fixture_content_version"] == original["content_version"]
    events = tables["content_change_event"]
    assert {item["event_type"] for item in events} == {"withdraw", "restrict", "correct"}
    assert all(item["tenant_id"] == warehouse.TENANTS[1] and item["lifecycle_data_synthetic"] for item in events)
    assert all(item["status"] == "pending" and item["attempt_count"] == 0 and item["mapping_required"]
               for item in tables["index_cleanup_task"])


def test_no_metrics_or_baseline_before_publication_or_future_open_boundary(tables, catalog):
    sources = {item["news_id"]: datetime.fromisoformat(item["publish_time"]) for item in catalog}
    future = catalog[-1]["news_id"]
    for name in ("news_metric_5min", "news_metric_hourly", "news_metric_channel_hourly", "news_metric_baseline_hourly"):
        assert all(item["news_id"] != future for item in tables[name])
        for row in tables[name]:
            published = sources[row["news_id"]]
            stamp = datetime.fromisoformat(row["event_time"])
            first = published.replace(minute=(published.minute // 5 * 5 if name == "news_metric_5min" else 0), second=0, microsecond=0)
            assert stamp >= first
            if name == "news_metric_5min":
                assert stamp.minute % 5 == 0 and stamp.second == 0
                assert stamp + timedelta(minutes=5) > published
    article = catalog[9]
    fine = [row for row in tables["news_metric_5min"] if row["news_id"] == article["news_id"]]
    assert min(item["event_time"] for item in fine) == "2026-09-30T12:15:00+08:00"


def test_all_additive_metrics_reconcile_five_minutes_channels_and_hours(tables):
    fine, channels = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    for row in tables["news_metric_5min"]:
        for name in warehouse.ADDITIVE:
            fine[_hour_key(row)][name] += row[name]
    for row in tables["news_metric_channel_hourly"]:
        for name in warehouse.ADDITIVE:
            channels[_hour_key(row)][name] += row[name]
    assert len(fine) == len(channels) == len(tables["news_metric_hourly"])
    for row in tables["news_metric_hourly"]:
        values = {name: row[name] for name in warehouse.ADDITIVE}
        assert values == dict(fine[_hour_key(row)]) == dict(channels[_hour_key(row)])
        assert 0 <= row["unique_users"] <= row["clicks"] <= row["impressions"]
        assert 0 <= row["detail_visitors"] <= row["detail_page_views"]
        assert 0 <= row["direct_page_views"] <= row["detail_page_views"]
        assert row["effective_consumptions"] <= min(row["clicks"], row["detail_page_views"])
        assert row["effective_consumptions"] <= row["detail_page_views"] - row["direct_page_views"]
        assert row["interactions"] == sum(row[name] for name in ("likes", "comments", "shares", "favorites"))
    assert any(row["detail_page_views"] > row["clicks"] and row["direct_page_views"] > 0 for row in tables["news_metric_hourly"])
    assert any(row["unique_users"] != row["detail_visitors"] for row in tables["news_metric_hourly"])


def test_hourly_uv_is_union_not_channel_or_bucket_sum(tables):
    for field in ("unique_users", "detail_visitors"):
        channels, fine = defaultdict(list), defaultdict(list)
        for row in tables["news_metric_channel_hourly"]:
            channels[_hour_key(row)].append(row[field])
        for row in tables["news_metric_5min"]:
            fine[_hour_key(row)].append(row[field])
        overlaps = 0
        for row in tables["news_metric_hourly"]:
            key = _hour_key(row)
            assert max(channels[key]) <= row[field] <= sum(channels[key])
            assert max(fine[key]) <= row[field] <= sum(fine[key])
            overlaps += row[field] < sum(channels[key]) and row[field] < sum(fine[key])
        assert overlaps > 0


def test_ordinary_news_cohort_sizes_remain_plausible_for_aggregate_simulation(config, catalog):
    article = catalog[0]
    rows = [row for name, row in warehouse._article_rows(article, 0, warehouse.TENANTS[0], config)
            if name == "news_metric_hourly" and row["clicks"] > 100]
    assert rows
    for row in rows:
        assert 0.55 <= row["unique_users"] / row["clicks"] <= 0.85
        assert 0.50 <= row["detail_visitors"] / row["detail_page_views"] <= 0.80


def test_latest_revision_quality_and_daily_exclude_open_and_invalidated_hours(config, tables):
    latest = {}
    for row in tables["window_snapshot"]:
        key = row["tenant_id"], row["event_time"]
        if key not in latest or row["snapshot_revision"] > latest[key]["snapshot_revision"]:
            latest[key] = row
    invalid = (config.end - timedelta(hours=2)).isoformat()
    last = (config.end - timedelta(hours=1)).isoformat()
    for tenant in warehouse.TENANTS:
        assert latest[tenant, invalid]["snapshot_revision"] == 2
        assert latest[tenant, invalid]["completeness"] == "incomplete"
        assert latest[tenant, last]["completeness"] == "corrected"
        assert latest[tenant, config.end.isoformat()]["completeness"] == "incomplete"
    days = defaultdict(list)
    for row in tables["news_metric_hourly"]:
        snapshot = latest[row["tenant_id"], row["event_time"]]
        if row["snapshot_revision"] == snapshot["snapshot_revision"] and snapshot["quality_status"] == "passed":
            key = row["tenant_id"], row["news_id"], row["event_time"][:10]
            days[key].append(row)
    for row in tables["news_metric_daily"]:
        hours = days[row["tenant_id"], row["news_id"], row["stat_date"]]
        assert row["observed_hours"] == len(hours)
        assert all(item["event_time"] not in (invalid, config.end.isoformat()) for item in hours)
        for name in warehouse.ADDITIVE:
            assert row[name] == sum(item[name] for item in hours)
        for field in ("unique_users", "detail_visitors"):
            assert max(item[field] for item in hours) <= row[field] <= sum(item[field] for item in hours)
        if row["stat_date"] == config.end.date().isoformat():
            assert row["completeness"] == "partial"
    assert any(row["excluded_hours"] > 0 for row in tables["news_metric_daily"])
    assert any(row["unique_users"] < sum(item["unique_users"] for item in days[row["tenant_id"], row["news_id"], row["stat_date"]])
               for row in tables["news_metric_daily"])


def test_baseline_uses_actual_available_prior_hours_and_null_for_no_samples(config, tables):
    hourly = {(row["tenant_id"], row["news_id"], row["event_time"]): row for row in tables["news_metric_hourly"]
              if row["snapshot_revision"] == 1 and datetime.fromisoformat(row["event_time"]) < config.end - timedelta(hours=2)}
    for row in tables["news_metric_baseline_hourly"]:
        start = datetime.fromisoformat(row["event_time"])
        prior = [hourly[key] for offset in (1, 2, 3)
                 if (key := (row["tenant_id"], row["news_id"], (start - timedelta(days=offset)).isoformat())) in hourly]
        assert row["sample_count"] == len(prior)
        for field in ("impressions", "clicks", "effective_consumptions", "interactions"):
            wanted = str((Decimal(sum(item[field] for item in prior)) / len(prior)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if prior else None
            assert row[f"baseline_{field}"] == wanted
    assert any(row["sample_count"] == 0 for row in tables["news_metric_baseline_hourly"])
    assert any(row["sample_count"] == 2 for row in tables["news_metric_baseline_hourly"])


def test_tenant_and_privacy_and_determinism(config, catalog, tables):
    forbidden = {"user_id", "device_id", "session_id", "click_intervals", "visit_intervals", "audiences"}
    for name, rows in tables.items():
        assert all(not forbidden.intersection(row) for row in rows)
        assert all(row["tenant_id"] in warehouse.TENANTS for row in rows)
        json.dumps(rows, ensure_ascii=False, allow_nan=False)
    assert list(warehouse.iter_rows("dim_news", config, catalog)) == tables["dim_news"]
    assert list(warehouse.iter_rows("window_snapshot", config, catalog)) == tables["window_snapshot"]
    with pytest.raises(ValueError):
        list(warehouse.iter_rows("raw_user_events", config, catalog))
    original = warehouse._hour_packet(catalog[0], 0, warehouse.TENANTS[0], 2, 1, config)
    assert original == warehouse._hour_packet(catalog[0], 0, warehouse.TENANTS[0], 2, 1, config)
    changed = warehouse.RealNewsSimulationConfig(config.start_at, config.closed_hours, config.seed + 1)
    assert original != warehouse._hour_packet(catalog[0], 0, warehouse.TENANTS[0], 2, 1, changed)


def test_copy_encoding_cannot_escape_into_a_sql_command():
    assert warehouse.copy_value(None) == r"\N"
    assert warehouse.copy_value("\\.\nDROP SCHEMA dw;\t\r\\") == "\\\\.\\nDROP SCHEMA dw;\\t\\r\\\\"
    assert warehouse.copy_value(True) == "t"


def test_export_is_atomic_new_only_and_table_hashes_match(tmp_path, catalog):
    config = warehouse.RealNewsSimulationConfig("2026-09-30T00:00:00+08:00", 2, 7)
    output = tmp_path / "new-v8"
    manifest = warehouse.export_dataset(config, output, catalog[:4])
    assert manifest["catalog_count"] == 4 and manifest["news_content_synthetic"] is False
    assert manifest["integration_status"].startswith("standalone_fixture_not_connected")
    for name, meta in manifest["tables"].items():
        raw = (output / f"{name}.jsonl").read_bytes()
        assert meta["sha256"] == sha256(raw).hexdigest()
        assert meta["rows"] == len(raw.splitlines())
    identity = {key: value for key, value in manifest.items() if key != "dataset_sha256"}
    assert sha256(warehouse._json(identity).encode()).hexdigest() == manifest["dataset_sha256"]
    assert sha256((output / "catalog_loader.py").read_bytes()).hexdigest() == manifest["catalog_loader_sha256"]
    sql = (output / "warehouse.sql").read_text()
    assert "CREATE SCHEMA news_sim_v8;" in sql and "CREATE SCHEMA IF NOT EXISTS" not in sql
    assert "COPY news_sim_v8.dim_news" in sql and "\\N" in sql and sql.endswith("COMMIT;\n")
    assert not list(output.glob("*.copy"))
    before = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        warehouse.export_dataset(config, output, catalog[:4])
    assert (output / "manifest.json").read_bytes() == before
