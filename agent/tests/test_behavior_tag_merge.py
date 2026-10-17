"""Small, resealed contracts exercise cross-version isolation and provenance."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json

import pytest

from app.modules.behavior_tags import merge
from app.modules.behavior_tags.source import WarehouseSource, WarehouseSourceError

TENANT = "11111111-1111-4111-8111-111111111111"
START = datetime.fromisoformat("2026-10-07T00:00:00+08:00")
EMPTY = sha256(b"").hexdigest()


def stamp(hour=0, minute=0):
    return (START + timedelta(hours=hour, minutes=minute)).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def v7_rows():
    article = dict(tenant_id=TENANT, news_id="v7-news-1", title="人工智能模型新闻", category="科技",
        primary_topic="人工智能", content_format="article", content_source="synthetic", content_type="article",
        content_version=1, event_id="e" * 64, lifecycle_revision=1, publish_time=stamp(-24),
        publisher_id="publisher-1", source="测试合成来源", status="published", status_effective_at=stamp(-24), synthetic=True)
    windows, metrics = [], []
    for hour, revision, good in ((0, 1, True), (0, 2, False), (1, 1, True)):
        windows.append(dict(tenant_id=TENANT, event_time=stamp(hour), window_end=stamp(hour + 1),
            snapshot_revision=revision, watermark_at=stamp(hour + 1, 5) if good else stamp(hour, 45),
            snapshot_at=stamp(2, 20) if revision == 2 else stamp(hour + 1, 10),
            completeness="complete" if good else "incomplete", quality_status="passed" if good else "pending",
            metric_policy_version="sim-consumption-policy-v1", dataset_version="v7-fixture", synthetic=True,
            late_added_clicks=0, late_corrected_bins=0))
        metrics.append(dict(tenant_id=TENANT, news_id="v7-news-1", event_time=stamp(hour), snapshot_revision=revision,
            uv_method="synthetic_interval_union_v1", impressions=1000, clicks=100, unique_users=80,
            total_duration_seconds=3000, effective_consumptions=50, likes=5, comments=1, shares=1,
            favorites=1, follows=4, interactions=12, negative_feedback=1, video_starts=0, video_completions=0))
    baseline = dict(tenant_id=TENANT, news_id="v7-news-1", event_time=stamp(1), sample_count=1,
        baseline_version="baseline-v7", **{field: "10" for field in merge.BASE_FIELDS})
    return dict(dim_news=[article], window_snapshot=windows, news_metric_hourly=metrics,
        news_metric_baseline_hourly=[baseline], content_tombstone=[])


def write_v7(directory, rows=None):
    rows = v7_rows() if rows is None else rows
    tables = {}
    for table, values in rows.items():
        payload = b"".join(canonical(row) + b"\n" for row in values)
        (directory / (table + ".jsonl")).write_bytes(payload)
        tables[table] = dict(rows=len(values), sha256=sha256(payload).hexdigest())
    manifest = dict(dataset_version="v7-fixture", schema="news_sim_v7", synthetic=True, tenants=[TENANT],
        historical_window_start=stamp(), historical_window_end=stamp(2), tables=tables)
    manifest["dataset_sha256"] = sha256(canonical(manifest)).hexdigest()
    (directory / "manifest.json").write_bytes(canonical(manifest))


def source(version, *, body="人工智能公开报道。", synthetic=False, scenario=None, hour=0,
           schema=None, blocked=(), policy="policy-1", news_id="same-news"):
    article = dict(tenant_id=TENANT, news_id=news_id, title="人工智能新闻", content=body,
        content_sha256=sha256(body.encode()).hexdigest(), news_content_synthetic=synthetic,
        content_scope="synthetic_title_only" if synthetic else "verified_public_body")
    metric = dict(tenant_id=TENANT, news_id=news_id, content_sha256=article["content_sha256"],
        event_time=stamp(hour), window_start=stamp(hour), window_end=stamp(hour + 1), unique_users=10)
    identity = dict(dataset_version=version, dataset_sha256=sha256(version.encode()).hexdigest(),
        tenant_ids=[TENANT], policy_version=policy, uv_method="uv-1", news_content_synthetic=synthetic,
        blocked_news_identities=list(blocked))
    if scenario:
        identity["scenario_id"] = scenario
    if schema:
        identity["source_schema"] = schema
    return WarehouseSource((article,), (metric,), identity, {}, START + timedelta(hours=hour),
        START + timedelta(hours=hour + 1), START + timedelta(hours=hour + 1))


def patch_sources(monkeypatch, values):
    monkeypatch.setattr(merge, "_load_source", lambda value, **kwargs: deepcopy(values[value]))


def test_v7_latest_quality_gate_and_missing_metric_fields(tmp_path):
    write_v7(tmp_path)
    loaded = merge.load_merged_source([tmp_path])
    assert len(loaded.articles) == 1 and len(loaded.observations) == 1
    metric = loaded.observations[0]
    assert metric["window_start"] == stamp(1)
    assert metric["detail_page_views"] is None and metric["follows"] == 4
    assert metric["unique_users"] == 80 and metric["sample_count"] == 1
    assert metric["source_dataset_version"] == "v7-fixture"
    assert loaded.articles[0]["content"] == ""
    assert loaded.articles[0]["news_id"].startswith("synthetic:tencent-news-v7:")


@pytest.mark.parametrize("table", list(v7_rows()))
def test_v7_all_relevant_hashes_are_verified(tmp_path, table):
    write_v7(tmp_path)
    path = tmp_path / (table + ".jsonl")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(WarehouseSourceError):
        merge.load_merged_source([tmp_path])


@pytest.mark.parametrize("change", [
    lambda rows: rows["news_metric_hourly"].append(deepcopy(rows["news_metric_hourly"][0])),
    lambda rows: rows["news_metric_hourly"][0].update(news_id="unknown"),
    lambda rows: rows["news_metric_hourly"][0].update(uv_method="summed_uv"),
    lambda rows: rows["window_snapshot"][0].update(window_end=stamp(2)),
    lambda rows: rows["news_metric_baseline_hourly"][0].update(baseline_clicks="NaN"),
])
def test_v7_resealed_business_contract_errors_fail(tmp_path, change):
    rows = v7_rows()
    change(rows)
    write_v7(tmp_path, rows)
    with pytest.raises(WarehouseSourceError):
        merge.load_merged_source([tmp_path])


def test_different_worlds_share_verified_content_but_never_sum_uv(monkeypatch):
    values = {"a": source("real-news-business-warehouse-v8", schema="news_sim_v8"),
              "b": source("corpus-v6", scenario="corpus-v6", policy="legacy-v6")}
    values["b"].source_identity["generator_profile"] = "corpus-v6"
    patch_sources(monkeypatch, values)
    merged = merge.load_merged_source(["b", "a"])
    assert len(merged.articles) == 1 and len(merged.articles[0]["source_refs"]) == 2
    assert merged.articles[0]["source_dataset_version"] == "real-news-business-warehouse-v8"
    assert len(merged.observations) == 2 and all(row["unique_users"] == 10 for row in merged.observations)
    assert len({row["metric_definition_id"] for row in merged.observations}) == 2
    assert merged.stats["uv_summed"] is False


def test_synthetic_news_ids_are_namespaced_per_scenario(monkeypatch):
    patch_sources(monkeypatch, {"a": source("v4", synthetic=True, scenario="timeline-v4", body=""),
        "b": source("v5", synthetic=True, scenario="timeline-v5", body="")})
    merged = merge.load_merged_source(["a", "b"])
    assert len(merged.articles) == 2
    assert {row["news_id"] for row in merged.articles} == {
        "synthetic:timeline-v4:same-news", "synthetic:timeline-v5:same-news"}
    assert all(row["source_news_id"] == "same-news" for row in merged.observations)


def test_v8_block_is_global_for_old_real_sources_but_does_not_cross_synthetic_worlds(monkeypatch):
    v8 = source("real-news-business-warehouse-v8", schema="news_sim_v8",
        blocked=[dict(tenant_id=TENANT, news_id="same-news")], news_id="eligible")
    patch_sources(monkeypatch, {"old": source("v6", scenario="corpus-v6"), "new": v8,
        "synthetic": source("v5", scenario="timeline-v5", synthetic=True, body="")})
    merged = merge.load_merged_source(["old", "new", "synthetic"])
    assert "same-news" not in {row["news_id"] for row in merged.articles}
    assert "same-news" not in {row["news_id"] for row in merged.observations}
    assert len(merged.articles) == 2
    old_coverage = next(row for row in merged.source_identity["coverage_by_scenario"] if row["source_dataset_version"] == "v6")
    assert old_coverage["hours"] == []


def test_conflicting_real_body_is_rejected(monkeypatch):
    patch_sources(monkeypatch, {"a": source("v8"), "b": source("v6", body="另一版本正文。")})
    with pytest.raises(WarehouseSourceError, match="conflicting verified content"):
        merge.load_merged_source(["a", "b"])


def test_headline_enrichment_retains_missing_source_body_evidence(monkeypatch):
    patch_sources(monkeypatch, {"headline": source("v3", body="", scenario="public-headlines-v3"),
        "body": source("real-news-business-warehouse-v8", schema="news_sim_v8")})
    merged = merge.load_merged_source(["headline", "body"])
    observation = next(row for row in merged.observations if row["source_dataset_version"] == "v3")
    assert observation["source_content_sha256"] is None
    assert observation["content_sha256"] == merged.articles[0]["content_sha256"]
    assert "no body" in observation["content_binding"]


def test_duplicate_dataset_is_idempotent_but_conflicting_hash_is_rejected(monkeypatch):
    patch_sources(monkeypatch, {"a": source("v8"), "b": source("v8")})
    assert len(merge.load_merged_source(["a", "a"]).observations) == 1
    other = source("v8")
    other.source_identity["dataset_sha256"] = "f" * 64
    patch_sources(monkeypatch, {"a": source("v8"), "b": other})
    with pytest.raises(WarehouseSourceError, match="conflicting source hashes"):
        merge.load_merged_source(["a", "b"])


def test_coverage_preserves_gaps_instead_of_asserting_envelope_is_complete(monkeypatch):
    patch_sources(monkeypatch, {"a": source("v8", hour=0), "b": source("v6", hour=10)})
    merged = merge.load_merged_source(["a", "b"])
    assert merged.window_start == START and merged.window_end == START + timedelta(hours=11)
    assert [row["hours"] for row in merged.source_identity["coverage_by_scenario"]] == [[stamp(0)], [stamp(10)]]


def test_observation_budget_fails_closed(monkeypatch):
    patch_sources(monkeypatch, {"a": source("v8"), "b": source("v6")})
    monkeypatch.setattr(merge, "MAX_MERGED_OBSERVATIONS", 1)
    with pytest.raises(WarehouseSourceError, match="budget"):
        merge.load_merged_source(["a", "b"])


def test_generator_source_is_reproducible_bounded_and_records_actual_modules():
    result = merge.load_merged_source(["profile:classic-v1"])
    identity = result.source_identity["sources"][0]
    assert len(result.articles) == 24 and len(result.observations) == 576
    assert "app.sql_assistant.warehouse" in identity["generator_module_sha256"]
    assert "app.sql_assistant.synthetic_profiles" in identity["generator_module_sha256"]
    assert all(len(value) == 64 for value in identity["generator_module_sha256"].values())
    assert all(row["detail_visitors"] is None for row in result.observations)
    assert all(row["sample_count"] is None for row in result.observations)


@pytest.mark.parametrize("values", [[], "profile:classic-v1", ["unknown"] * 11])
def test_source_sequence_is_bounded(values):
    with pytest.raises(WarehouseSourceError):
        merge.load_merged_source(values)
