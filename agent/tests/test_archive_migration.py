"""Regression contracts for the archive merge and Windows data-root migration."""

from contextlib import contextmanager
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from app.observability import decorators
from app.observability.decorator import trace
from app.sql_assistant import real_news_profiles as profiles
from app.sql_assistant import warehouse
from simulation.warehouse_simulation import real_news_catalog as catalog


@pytest.mark.asyncio
async def test_archive_trace_handles_optional_identity_and_failed_domain_results(monkeypatch):
    attributes, errors, outcomes = [], [], []

    @contextmanager
    def operation(name, **kwargs):
        attributes.append(kwargs["attributes"])
        yield object()

    monkeypatch.setattr(decorators, "operation", operation)
    monkeypatch.setattr(decorators, "record_error", lambda span, error, name: errors.append(error))
    monkeypatch.setattr(decorators, "set_safe_attributes", lambda span, values: outcomes.append(values))

    @trace("migration.optional")
    async def without_identity(value):
        return value

    @trace("migration.result", identity={"request.tenant_id": "tenant_id"}, result_status=True)
    async def with_identity(request):
        return SimpleNamespace(status="failed")

    assert await without_identity("private input") == "private input"
    result = await with_identity(SimpleNamespace(tenant_id="tenant-a", text="private input"))
    assert result.status == "failed"
    assert attributes == [{}, {"tenant_id": "tenant-a"}]
    assert outcomes == [{"newsagent.outcome": "failed"}]
    assert len(errors) == 1 and isinstance(errors[0], decorators.DomainOutcomeError)
    assert without_identity.__name__ == "without_identity"


def test_frozen_source_names_resolve_new_data_root_without_overriding_legacy(tmp_path):
    relative = "news/data/incremental/example/articles/news.json"
    relocated = tmp_path / "var" / relative
    relocated.parent.mkdir(parents=True)
    relocated.write_text("{}", encoding="utf-8")
    assert catalog._resolve_source(tmp_path, relative) == relocated
    legacy = tmp_path / relative
    legacy.parent.mkdir(parents=True)
    legacy.write_text("corrupt legacy file must still fail validation", encoding="utf-8")
    assert catalog._resolve_source(tmp_path, relative) == legacy
    with pytest.raises(ValueError, match="escapes"):
        catalog._resolve_source(tmp_path, "../../outside.json")


def test_extension_serialization_is_canonical_and_versioned():
    v3_path = catalog.EXTENSION_PATH.with_name("catalog-extension-v3.json")
    raw = v3_path.read_bytes()
    payload = json.loads(raw)
    canonical = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    assert raw == canonical
    assert payload["extension_version"] == "real-news-warehouse-extension-v3-20261023"
    assert len(catalog.load_extension(v3_path)) == 7459


def test_real_profile_excludes_restricted_tombstoned_and_content_only_metrics(tmp_path, monkeypatch):
    tenant = str(profiles.TENANTS[0])
    article_ids = ["eligible", "restricted", "tombstoned", "content-only"]
    monkeypatch.setattr(profiles, "_articles", lambda: tuple(
        {"news_id": key, "metric_available": key != "content-only"} for key in article_ids
    ))
    monkeypatch.setattr(profiles, "verified_real_news_dataset_path", lambda: tmp_path)
    dimensions = [{"tenant_id": tenant, "news_id": key,
                   "status": "restricted" if key == "restricted" else "published",
                   "metric_available": key != "content-only"} for key in article_ids]
    hour = "2026-10-03T10:00:00+08:00"
    metrics = [{"tenant_id": tenant, "news_id": key, "event_time": hour,
                "snapshot_revision": 1, **{name: 2 for name in profiles._MEASURE_FIELDS}}
               for key in article_ids]
    tables = {"dim_news": dimensions, "news_metric_hourly": metrics,
              "content_tombstone": [{"tenant_id": tenant, "news_id": "tombstoned"}]}
    for table, rows in tables.items():
        (tmp_path / f"{table}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    rows = list(profiles._iter_metrics("news_metric_hourly", {(tenant, hour): 1}))
    assert [row["news_id"] for row in rows] == ["eligible"]
    assert rows[0]["tenant_id"] == profiles.TENANTS[0]
    assert rows[0]["event_time"] == datetime.fromisoformat(hour)


def test_real_profile_counts_keep_primary_tenant_separate_from_totals(monkeypatch):
    manifest = {
        "dataset_version": "test", "dataset_sha256": "a" * 64, "enterprise_scenarios": [],
        "metric_available_article_count": 661, "content_only_article_count": 7459,
        "extension_sha256": "b" * 64,
        "tables": {"news_metric_hourly": {"rows": 6}, "news_metric_baseline_hourly": {"rows": 3}},
        "tenant_table_rows": {str(warehouse.DEMO_TENANT_ID): {"news_metric_hourly": 2, "news_metric_baseline_hourly": 1}},
    }
    monkeypatch.setattr(warehouse, "streamed_manifest", lambda *args: manifest)
    result = warehouse.demo_dataset_info("real-news-v8", news_per_tenant=8120)
    assert (result["metric_row_count"], result["total_metric_row_count"]) == (2, 6)
    assert (result["baseline_row_count"], result["total_baseline_row_count"]) == (1, 3)
