"""Frozen public titles never replace older fixtures or imply real behavior."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.sql_assistant import public_headlines as public
from app.sql_assistant import warehouse
from app.sql_assistant.hot_news_binding import HotNewsSqlBindingError
from app.sql_assistant.scaled_profiles import TABLES, iter_scaled_rows
from app.sql_assistant.service import SqlAssistantError, SqlAssistantService
from examples.data_analysis_demo import _catalog, DemoError
from examples.native_hot_news_sql_support import SqlHotNewsContentRepository
from tests.test_hot_news_sql_binding import scope
from tests.test_sql_assistant_service import MemorySnapshots, RecordingAgent, RecordingWarehouse, TENANT, USER, request


def catalog_data(count=120):
    return {"catalog_version": public.PUBLIC_DATASET_VERSION,
            "collected_at": "2026-10-07T12:00:00+08:00", "articles": [
                {"news_id": f"20261007A{index:05d}", "title": f"公开标题引用测试 {index}",
                 "source": "公开来源", "url": f"https://news.qq.com/rain/a/20261007A{index:05d}",
                 "published_at": "2026-10-07T09:00:00+08:00", "category": "社会",
                 "content_type": "video" if index % 2 else "article"}
                for index in range(1, count + 1)]}


@pytest.fixture
def frozen_catalog(tmp_path, monkeypatch):
    data = catalog_data()
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(public, "CATALOG_PATH", path)
    monkeypatch.setattr(public, "CATALOG_SHA256", sha256(path.read_bytes()).hexdigest())
    schema = tmp_path / "schema-v3.md"
    schema.write_text("# public title reference test contract\n", encoding="utf-8")
    monkeypatch.setattr(warehouse, "SCHEMA_PATH_V3", schema)
    monkeypatch.setattr(warehouse, "SCHEMA_SHA256_V3", sha256(schema.read_bytes()).hexdigest())
    return data, path


def test_public_dimension_preserves_real_title_id_source_and_date(frozen_catalog):
    data, _ = frozen_catalog
    rows = list(public.iter_public_rows("dim_news", 120))
    assert len(rows) == 240
    assert rows[0]["news_id"] == data["articles"][0]["news_id"]
    assert rows[0]["title"] == data["articles"][0]["title"]
    assert rows[0]["source"] == data["articles"][0]["source"]
    assert rows[0]["publish_time"].isoformat() == data["articles"][0]["published_at"]
    assert rows[0]["title"] == rows[120]["title"]
    metrics = public.iter_public_rows("news_metric_hourly", 120)
    first_metric = next(metrics)
    assert first_metric["event_time"] < rows[0]["publish_time"]
    assert first_metric["event_time"] == warehouse.WINDOW_START
    assert warehouse.demo_news_ids(public.PUBLIC_HEADLINES_PROFILE, news_per_tenant=120)[0] == rows[0]["news_id"]
    assert next(iter_scaled_rows("dim_news", 120))["news_id"] == "scale-news-000001"


@pytest.mark.parametrize("bad", [12, 0, 12000, True, "1200"])
def test_public_size_is_explicit_and_never_silently_repeated(bad, frozen_catalog):
    with pytest.raises(ValueError, match="120 or 1200"):
        public.public_news_ids(bad)


def test_missing_catalog_and_too_small_selection_are_rejected(frozen_catalog):
    _, path = frozen_catalog
    with pytest.raises(ValueError, match="smaller"):
        public.public_news_ids(1200)
    path.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        public.public_catalog()


@pytest.mark.parametrize("mutation", ["title", "duplicate_id", "duplicate_title", "url", "date", "source", "extra"])
def test_catalog_contract_validation_does_not_accept_invalid_references(mutation):
    data = catalog_data()
    entry = data["articles"][0]
    if mutation == "title":
        entry["title"] = ""
    elif mutation == "duplicate_id":
        data["articles"][1]["news_id"] = entry["news_id"]
        data["articles"][1]["url"] = entry["url"]
    elif mutation == "duplicate_title":
        data["articles"][1]["title"] = entry["title"]
    elif mutation == "url":
        entry["url"] = "https://example.com/fake"
    elif mutation == "date":
        entry["published_at"] = "2026-10-07T09:00:00"
    elif mutation == "source":
        entry["source"] = "source\ncommand"
    else:
        entry["body"] = "unexpected full text"
    contents = json.dumps(data).encode()
    with pytest.raises(ValueError):
        public._parse_catalog(contents, sha256(contents).hexdigest())


def test_catalog_rejects_ambiguous_duplicate_json_fields():
    data = catalog_data()
    contents = json.dumps(data).replace('"catalog_version":', '"catalog_version": "unapproved", "catalog_version":', 1).encode()
    with pytest.raises(ValueError, match="duplicate object fields"):
        public._parse_catalog(contents, sha256(contents).hexdigest())


def test_manifest_binds_catalog_and_rechecks_file_even_after_cache_hit(frozen_catalog):
    _, path = frozen_catalog
    manifest = public.public_manifest(120)
    assert manifest["tables"]["dim_news"]["rows"] == 240
    assert manifest["tables"]["news_metric_hourly"]["rows"] == 5760
    assert manifest["catalog_sha256"] == public.CATALOG_SHA256
    manifest["tables"]["dim_news"]["rows"] = 1
    assert public.public_manifest(120)["tables"]["dim_news"]["rows"] == 240
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        public.public_manifest(120)


def test_public_scope_binds_dataset_hash_and_rejects_v2_schema(frozen_catalog):
    manifest = public.public_manifest(120)
    identity = {key: manifest[key] for key in ("dataset_profile", "dataset_version", "dataset_sha256", "news_per_tenant")}
    kwargs = {"warehouse_schema_version": "news-warehouse-v3", "schema_sha256": warehouse.SCHEMA_SHA256_V3,
              "dataset_identity": identity}
    digest = scope(**kwargs)
    assert digest != scope(**{**kwargs, "dataset_identity": {**identity, "dataset_sha256": "a" * 64}})
    with pytest.raises(HotNewsSqlBindingError):
        scope(**{**kwargs, "warehouse_schema_version": "news-warehouse-v2"})
    with pytest.raises(HotNewsSqlBindingError):
        scope(**{**kwargs, "dataset_identity": {**identity, "news_per_tenant": 12000}})


def make_service(tmp_path):
    scenes = Path(__file__).resolve().parents[1] / "deploy" / "text2sql-scenes.enterprise-v2.yml"
    source = tmp_path / "scenes-v3.yml"
    source.write_text(scenes.read_text().replace("news-warehouse-v2", "news-warehouse-v3"), encoding="utf-8")
    return SqlAssistantService(agent=RecordingAgent(), store=MemorySnapshots(),
        warehouse_factory=lambda tenant: RecordingWarehouse(), scenarios_path=str(source),
        model_provider="local-deterministic", dataset_profile=public.PUBLIC_HEADLINES_PROFILE, news_per_tenant=120)


@pytest.mark.asyncio
async def test_public_preview_is_versioned_and_catalog_change_rejects_old_snapshot(frozen_catalog, tmp_path, monkeypatch):
    _, path = frozen_catalog
    service = make_service(tmp_path)
    preview = await service.preview(request(), tenant_id=TENANT, user_id=USER)
    assert preview.schema_version == "news-warehouse-v3"
    saved = service.store.snapshots[preview.query_id]
    assert saved["dataset_contract"]["dataset_profile"] == public.PUBLIC_HEADLINES_PROFILE
    old_saved = deepcopy(saved)
    data = json.loads(path.read_text())
    data["articles"][0]["title"] += " changed"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(public, "CATALOG_SHA256", sha256(path.read_bytes()).hexdigest())
    with pytest.raises(SqlAssistantError, match="规模或版本"):
        await service.execute(preview.query_id, tenant_id=TENANT, user_id=USER)
    assert service.store.snapshots[preview.query_id] == old_saved


@pytest.mark.asyncio
async def test_public_content_is_only_title_reference_and_preserves_original_url(frozen_catalog):
    row = next(public.iter_public_rows("dim_news", 120))
    details = SimpleNamespace(tenant_id=str(row["tenant_id"]), dataset_profile=public.PUBLIC_HEADLINES_PROFILE,
                              fetch_metadata=AsyncMock(return_value=(row,)))
    content = await SqlHotNewsContentRepository(details).batch_get_by_news_ids(
        tenant_id=details.tenant_id, news_ids=(row["news_id"],))
    article = public.public_article(row["news_id"])
    assert content[row["news_id"]].title == article["title"]
    assert content[row["news_id"]].source_url == article["url"]
    assert "没有抓取或重建原文" in content[row["news_id"]].body
    assert "合成运营样本" in content[row["news_id"]].body
    assert "日期独立" in content[row["news_id"]].body
    row["title"] += " changed"
    with pytest.raises(Exception, match="frozen catalog"):
        await SqlHotNewsContentRepository(details).batch_get_by_news_ids(tenant_id=details.tenant_id,
                                                                       news_ids=(row["news_id"],))


def test_cli_catalog_binds_public_catalog_identity_and_scale(frozen_catalog, tmp_path):
    config = make_service(tmp_path).config()
    identity, scenarios = _catalog(config)
    assert identity["headline_catalog_count"] == 120
    assert identity["headline_catalog_sha256"] == public.CATALOG_SHA256
    assert identity["headline_date_start"] == identity["headline_date_end"] == "2026-10-07"
    assert len(scenarios) == 10
    config["dataset"]["headline_catalog_count"] = 119
    with pytest.raises(DemoError):
        _catalog(config)


@pytest.mark.asyncio
async def test_public_initializer_streams_bounded_rows_and_never_reseeds_owned_database(frozen_catalog, monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock())
    @asynccontextmanager
    async def begin():
        yield connection
    database = SimpleNamespace(engine=SimpleNamespace(begin=begin))
    ownership = AsyncMock(return_value=False)
    monkeypatch.setattr(warehouse, "_profile_contract", ownership)
    monkeypatch.setattr(warehouse, "_ensure_scaled_schema", AsyncMock())
    verifier = AsyncMock()
    monkeypatch.setattr(warehouse, "_verify_scaled_rows", verifier)
    result = await warehouse._initialize_scaled_warehouse(database, news_per_tenant=120,
                                                         dataset_profile=public.PUBLIC_HEADLINES_PROFILE)
    assert result["total_news_count"] == 240
    inserts = [call for call in connection.execute.call_args_list if str(call.args[0]).startswith("INSERT INTO dw.")]
    assert sum(len(call.args[1]) for call in inserts) == 240 + 5760 * 2
    assert all(len(call.args[1]) <= 500 for call in inserts)
    assert ownership.call_args.args[1] == public.PUBLIC_HEADLINES_PROFILE
    connection.execute.reset_mock()
    ownership.return_value = True
    await warehouse._initialize_scaled_warehouse(database, news_per_tenant=120,
                                                  dataset_profile=public.PUBLIC_HEADLINES_PROFILE)
    assert not any(str(call.args[0]).startswith("INSERT INTO dw.") for call in connection.execute.call_args_list)
    assert verifier.await_count == 2
