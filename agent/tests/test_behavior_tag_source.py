"""Frozen source fixtures exercise gates independently of the large v8 corpus."""

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json

import pytest

from app.modules.behavior_tags import source
from app.modules.behavior_tags.source import WarehouseSourceError, load_warehouse_source


TENANTS = ("11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333")
START = datetime.fromisoformat("2026-10-14T00:00:00+08:00")
NEWS = "20261013ABCD00"
POLICY = "sim-click-attributed-reading-v8.1"
UV = "temporary_interval_union_v8.1"


def stamp(hours=0, minutes=0):
    return (START + timedelta(hours=hours, minutes=minutes)).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def article(tenant):
    content = "人工智能研究与应用的公开新闻正文。"
    return dict(tenant_id=tenant, news_id=NEWS, title="人工智能新闻", content=content,
        source_url="https://example.com/news", content_type="article", category="科技",
        category_data_synthetic=True, source="公开测试来源", publish_time=stamp(-24),
        publish_time_raw=stamp(-24), source_content_version=1, content_version=1,
        fixture_content_version=1, content_sha256=sha256(content.encode()).hexdigest(),
        source_file="test.json", source_sha256="a" * 64, source_package="fixture",
        source_manifest_sha256=None, source_html_sha256=None, news_content_synthetic=False,
        metric_data_synthetic=True, lifecycle_data_synthetic=True, lifecycle_scenario="none",
        status="published", lifecycle_revision=1, status_effective_at=stamp(-24))


def snapshot(tenant, hour, revision, complete=True):
    return dict(tenant_id=tenant, event_time=stamp(hour), snapshot_revision=revision,
        window_end=stamp(hour + 1), watermark_at=stamp(hour + 1, 10) if complete else stamp(hour, 45),
        snapshot_at=stamp(hour + 1, 15) if complete else stamp(hour, 50),
        completeness=("corrected" if revision > 1 else "complete") if complete else "incomplete",
        quality_status="passed" if complete else "pending", policy_version=POLICY,
        metric_data_synthetic=True)


def metric(tenant, hour, revision):
    return dict(tenant_id=tenant, news_id=NEWS, event_time=stamp(hour), snapshot_revision=revision,
        impressions=100, clicks=20 + (revision > 1), unique_users=10,
        detail_page_views=30, direct_page_views=15, detail_visitors=20,
        total_duration_seconds=600, effective_consumptions=12, interactions=6,
        likes=3, comments=1, shares=1, favorites=1, negative_feedback=1,
        policy_version=POLICY, uv_method=UV)


def baseline(tenant, hour):
    return dict(tenant_id=tenant, news_id=NEWS, event_time=stamp(hour), sample_count=0,
        baseline_version="sim-prior-3-available-days-same-hour-v8.1",
        **dict.fromkeys(source._BASES))


def fixture_rows():
    data = {name: [] for name in source._COLUMNS}
    for tenant in TENANTS:
        data["dim_news"].append(article(tenant))
        for hour, revision, complete in ((0, 1, True), (1, 1, True), (1, 2, False),
                                          (2, 1, False), (2, 2, True), (3, 1, False)):
            window = snapshot(tenant, hour, revision, complete)
            if hour == 1 and revision == 2:
                window["snapshot_at"] = stamp(3, 25)
            data["window_snapshot"].append(window)
            data["news_metric_hourly"].append(metric(tenant, hour, revision))
        # No baseline for hour 2: absence is explicitly unknown, not zero.
        data["news_metric_baseline_hourly"].append(baseline(tenant, 0))
        data["news_metric_baseline_hourly"].append(baseline(tenant, 1))
    return data


def write_fixture(path, data=None, manifest_change=None):
    data = fixture_rows() if data is None else data
    tables = {}
    for name, rows in data.items():
        payload = b"".join(canonical(row) + b"\n" for row in rows)
        (path / f"{name}.jsonl").write_bytes(payload)
        tables[name] = dict(rows=len(rows), sha256=sha256(payload).hexdigest(), columns=sorted(source._COLUMNS[name]))
    manifest = dict(dataset_version="real-news-business-warehouse-v8-fixture", schema="news_sim_v8",
        tenants=list(TENANTS), catalog_sha256="c" * 64, closed_window_start=stamp(),
        closed_window_end=stamp(3), asof=stamp(3, 50), news_content_synthetic=False,
        metric_data_synthetic=True, tables=tables,
        metric_provenance=dict(policy_version=POLICY, uv_method=UV,
            event_storage="aggregate only; temporary audience intervals never saved"))
    if manifest_change:
        manifest_change(manifest)
    manifest["dataset_sha256"] = sha256(canonical(manifest)).hexdigest()
    (path / "manifest.json").write_bytes(canonical(manifest))
    return data


def test_latest_revision_before_gate_and_missing_baseline_are_preserved(tmp_path):
    write_fixture(tmp_path)
    loaded = load_warehouse_source(tmp_path)
    assert len(loaded.articles) == 2
    assert len(loaded.observations) == 4
    assert loaded.stats["qualified_windows"] == 4
    assert loaded.stats["excluded_windows"] == 2
    assert {row["event_time"] for row in loaded.observations} == {stamp(0), stamp(2)}
    assert all(row["unique_users"] == 10 for row in loaded.observations)
    early, late = loaded.observations[:2]
    assert early["sample_count"] == 0 and early["baseline_clicks"] is None
    assert late["sample_count"] is None and late["baseline_version"] is None
    assert late["snapshot_revision"] == 2 and late["clicks"] == 21
    assert late["source_content_version"] == 1
    assert late["content_sha256"] == loaded.articles[0]["content_sha256"]
    assert loaded.source_identity["tenant_ids"] == list(TENANTS)
    assert len(loaded.source_identity["input_file_sha256"]) == 5


def test_visibility_cutoff_selects_latest_visible_then_applies_gate(tmp_path):
    write_fixture(tmp_path)
    loaded = load_warehouse_source(tmp_path, tenant_ids=(TENANTS[0],),
        as_of=datetime.fromisoformat(stamp(2, 30)))
    assert loaded.window_end.isoformat() == stamp(2)
    assert len(loaded.observations) == 2
    assert loaded.observations[1]["event_time"] == stamp(1)
    assert loaded.observations[1]["snapshot_revision"] == 1


def test_tombstones_fail_closed_even_with_published_dimension(tmp_path):
    data = fixture_rows()
    data["content_tombstone"].append(dict(tenant_id=TENANTS[1], news_id=NEWS, event_id="b" * 64,
        blocked_through_version=1, lifecycle_revision=2, status="restricted", effective_at=stamp(2),
        lifecycle_data_synthetic=True))
    write_fixture(tmp_path, data)
    loaded = load_warehouse_source(tmp_path)
    assert len(loaded.articles) == 1 and len(loaded.observations) == 2
    assert loaded.stats["excluded_lifecycle_observations"] == 2
    assert loaded.stats["not_historical_lifecycle_reconstruction"] is True
    assert loaded.source_identity["source_schema"] == "news_sim_v8"
    assert loaded.source_identity["blocked_news_identities"] == [dict(
        tenant_id=TENANTS[1], news_id=NEWS, status="restricted", lifecycle_revision=2,
        reason="content_tombstone")]


def test_current_lifecycle_blocks_are_exported_only_for_selected_tenant(tmp_path):
    data = fixture_rows()
    data["dim_news"][1].update(status="withdrawn", lifecycle_revision=2, status_effective_at=stamp(2))
    write_fixture(tmp_path, data)
    loaded = load_warehouse_source(tmp_path)
    assert loaded.source_identity["blocked_news_identities"] == [dict(
        tenant_id=TENANTS[1], news_id=NEWS, status="withdrawn", lifecycle_revision=2,
        reason="current_lifecycle_status")]
    selected = load_warehouse_source(tmp_path, tenant_ids=(TENANTS[0],))
    assert selected.source_identity["blocked_news_identities"] == []


@pytest.mark.parametrize("name", list(source._COLUMNS))
def test_each_input_hash_is_verified(tmp_path, name):
    write_fixture(tmp_path)
    path = tmp_path / f"{name}.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(WarehouseSourceError):
        load_warehouse_source(tmp_path)


@pytest.mark.parametrize("name", ["dim_news", "window_snapshot", "news_metric_hourly", "news_metric_baseline_hourly"])
def test_duplicate_business_keys_rejected_even_when_hash_is_resealed(tmp_path, name):
    data = fixture_rows()
    data[name].append(deepcopy(data[name][0]))
    write_fixture(tmp_path, data)
    with pytest.raises(WarehouseSourceError, match="duplicate"):
        load_warehouse_source(tmp_path)


@pytest.mark.parametrize("table,field,value", [
    ("news_metric_hourly", "news_id", "20261013UNKNOWN"),
    ("news_metric_hourly", "tenant_id", "22222222-2222-4222-8222-222222222222"),
    ("news_metric_hourly", "event_time", "2026-10-14T00:01:00+08:00"),
    ("news_metric_hourly", "event_time", "2026-10-14T00:00:00"),
    ("news_metric_hourly", "snapshot_revision", 99),
    ("news_metric_hourly", "policy_version", "other-policy"),
    ("news_metric_hourly", "uv_method", "sum-hourly-uv"),
    ("news_metric_hourly", "unique_users", 99),
    ("news_metric_hourly", "clicks", -1),
    ("news_metric_hourly", "clicks", True),
    ("news_metric_hourly", "interactions", 99),
    ("window_snapshot", "watermark_at", "2027-01-01T00:00:00+08:00"),
    ("window_snapshot", "policy_version", "other-policy"),
    ("dim_news", "content_sha256", "0" * 64),
    ("dim_news", "source_content_version", 2),
    ("news_metric_baseline_hourly", "baseline_clicks", "0.00"),
    ("news_metric_baseline_hourly", "sample_count", -1),
    ("news_metric_baseline_hourly", "event_time", "2026-09-01T00:00:00+08:00"),
])
def test_resealed_but_invalid_semantics_are_rejected(tmp_path, table, field, value):
    data = fixture_rows()
    data[table][0][field] = value
    write_fixture(tmp_path, data)
    with pytest.raises(WarehouseSourceError):
        load_warehouse_source(tmp_path)


def test_manifest_hash_and_duplicate_keys_are_rejected(tmp_path):
    write_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    raw = path.read_bytes().replace(b'"news_sim_v8"', b'"news_sim_v9"')
    path.write_bytes(raw)
    with pytest.raises(WarehouseSourceError, match="SHA-256"):
        load_warehouse_source(tmp_path)
    write_fixture(tmp_path)
    path.write_bytes(path.read_bytes().replace(b'{', b'{"schema":"news_sim_v8",', 1))
    with pytest.raises(WarehouseSourceError):
        load_warehouse_source(tmp_path)


@pytest.mark.parametrize("tenants", [(), (TENANTS[0], TENANTS[0]), ("not-a-uuid",), [TENANTS[0]]])
def test_tenant_selection_is_strict(tmp_path, tenants):
    write_fixture(tmp_path)
    with pytest.raises(WarehouseSourceError):
        load_warehouse_source(tmp_path, tenant_ids=tenants)


def test_limits_and_symlinks_are_rejected(tmp_path, monkeypatch):
    write_fixture(tmp_path)
    monkeypatch.setattr(source, "MAX_TABLE_ROWS", 1)
    with pytest.raises(WarehouseSourceError, match="row count"):
        load_warehouse_source(tmp_path)
    monkeypatch.setattr(source, "MAX_TABLE_ROWS", 500_000)
    target = tmp_path / "dim_news.jsonl"
    actual = tmp_path / "actual.jsonl"
    target.rename(actual)
    target.symlink_to(actual)
    with pytest.raises(WarehouseSourceError, match="regular"):
        load_warehouse_source(tmp_path)


def test_explicit_asof_cannot_claim_future_visibility(tmp_path):
    write_fixture(tmp_path)
    with pytest.raises(WarehouseSourceError, match="visibility"):
        load_warehouse_source(tmp_path, as_of=datetime.fromisoformat(stamp(4)))
