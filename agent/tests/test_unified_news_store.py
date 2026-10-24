"""Exercise immutable releases against real isolated SQL transactions."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, func, select, text, update

from app.modules.news_storage.model import build_package
from app.modules.news_storage.store import (
    FrozenNewsStore, ReleaseNewsContentRepository, items, provenance, releases, versions,
)


def candidate(news_id="n1", *, body="公开新闻正文。" * 30, source="first", **overrides):
    row = {
        "news_id": news_id, "title": f"新闻 {news_id}", "body": body,
        "url": f"https://news.qq.com/rain/a/{news_id}",
        "publish_time": "2026-10-01T08:00:00+08:00",
        "content_type": "article", "content_state": "body_available",
        "fragments": [], "quality_flags": [], "priority": 10,
        "source": {"package": source, "path": f"fixtures/{source}/{news_id}.json",
                   "file_sha256": "a" * 64, "manifest_path": None,
                   "manifest_sha256": None, "source_content_version": "1",
                   "fetched_at": None},
    }
    row.update(overrides)
    return row


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'content.sqlite'}")
    result = FrozenNewsStore(engine)
    result.initialize()
    yield result
    engine.dispose()


def publish(store, candidates, tenant="tenant-a"):
    package = build_package(candidates, tenant_id=tenant)
    result = store.import_release(package)
    return package, result


def table_count(store, table):
    with store.engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_roundtrip_and_duplicate_import_are_immutable(store):
    package, result = publish(store, [candidate()])
    release_id = result["release_id"]
    assert result["import_status"] == "created"
    assert store.import_release(package)["import_status"] == "already_present"
    assert table_count(store, versions) == 1
    assert table_count(store, releases) == 1
    assert table_count(store, provenance) == 1
    assert table_count(store, items) == 1
    store.initialize()  # A repeated initialization never truncates existing data.
    record = store.get_record("tenant-a", release_id, "n1")
    assert all(record[key] == value for key, value in package["versions"][0].items())
    assert record["publish_time"] == "2026-10-01T00:00:00+00:00"
    assert record["provenance"] == [package["provenance"][0]["source"]]
    assert store.get_record("tenant-a", release_id, "missing") is None
    with store.engine.connect() as connection:
        extras = connection.execute(select(versions.c.record)).scalar_one()
    assert set(extras) == {"fragments", "quality_flags"}
    assert "body" not in extras
    with store.engine.connect() as connection:
        selected = connection.execute(text(
            "SELECT news_id,body,release_id FROM news_content_selected "
            "WHERE tenant_id=:tenant AND release_id=:release"
        ), {"tenant": "tenant-a", "release": release_id}).mappings().one()
    assert selected["body"] == candidate()["body"]
    assert selected["release_id"] == release_id


def test_variants_and_multiple_sources_survive_without_changing_old_release(store):
    original = candidate()
    initial, result = publish(store, [original])
    old_release = result["release_id"]
    second_source = candidate(source="second")
    corrected = candidate(body="另一版本正文。" * 30, source="third", priority=1)
    expanded, later = publish(store, [original, second_source, corrected])
    assert table_count(store, versions) == 2
    assert later["counts"]["conflicting_body_news_count"] == 1
    assert later["counts"]["provenance_count"] == 3
    assert store.get_record("tenant-a", old_release, "n1")["body"] == original["body"]
    assert len(store.get_record("tenant-a", old_release, "n1")["provenance"]) == 1
    selected = store.get_record("tenant-a", later["release_id"], "n1")
    assert selected["body"] == corrected["body"]
    assert "conflicting_body_variants" in selected["selection_quality_flags"]
    assert store.import_release(initial)["import_status"] == "already_present"
    assert store.import_release(expanded)["import_status"] == "already_present"


def test_tenant_and_release_scope_are_mandatory_even_for_empty_requests(store):
    _, result = publish(store, [candidate()])
    release = result["release_id"]
    for operation in (
        lambda: store.get_record("tenant-b", release, "n1"),
        lambda: store.list_records("tenant-b", release),
        lambda: store.summary("tenant-b", release),
        lambda: store.get_record("tenant-a", "unknown", "n1"),
        lambda: store.list_records("", release),
    ):
        with pytest.raises(ValueError):
            operation()
    repo = ReleaseNewsContentRepository(store, "tenant-a", release)
    with pytest.raises(ValueError, match="different tenant"):
        asyncio.run(repo.batch_get_by_news_ids(tenant_id="tenant-b", news_ids=()))
    missing = ReleaseNewsContentRepository(store, "tenant-a", "unknown")
    with pytest.raises(ValueError, match="unknown frozen release"):
        asyncio.run(missing.batch_get_by_news_ids(tenant_id="tenant-a", news_ids=()))
    _, other = publish(store, [candidate(body="租户 B 正文" * 30)], tenant="tenant-b")
    assert store.get_record("tenant-b", other["release_id"], "n1")["body"] != store.get_record("tenant-a", release, "n1")["body"]


def test_stable_pagination_and_body_only_filter(store):
    _, result = publish(store, [
        candidate("c"), candidate("a"),
        candidate("b", body=None, content_state="title_only", publish_time=None),
    ])
    release = result["release_id"]
    assert [r["news_id"] for r in store.list_records("tenant-a", release, limit=2)] == ["a", "b"]
    assert [r["news_id"] for r in store.list_records("tenant-a", release, after_news_id="b", limit=2)] == ["c"]
    assert [r["news_id"] for r in store.list_records("tenant-a", release, body_only=True)] == ["a", "c"]
    for limit in (0, 1001, True):
        with pytest.raises(ValueError, match="limit"):
            store.list_records("tenant-a", release, limit=limit)


def test_repository_excludes_partial_missing_and_unreviewed_metadata(store):
    _, result = publish(store, [
        candidate("full"), candidate("video", content_type="video"),
        candidate("fragments", body=None, content_state="fragments_only",
                  fragments=[{"chunk_index": 0, "text": "只有切片，不代表完整正文"}], publish_time=None),
        candidate("title", body=None, content_state="title_only"),
        candidate("review", quality_flags=["metadata_review_required"]),
        candidate("missing_url", url=None),
    ])
    repo = ReleaseNewsContentRepository(store, "tenant-a", result["release_id"])
    rows = asyncio.run(repo.batch_get_by_news_ids(
        tenant_id="tenant-a", news_ids=("full", "full", "video", "fragments", "title", "review", "missing_url", "unknown"),
    ))
    assert set(rows) == {"full", "video"}
    assert str(rows["video"].content_type) == "video"
    assert rows["full"].collection_id is None
    evidence = asyncio.run(repo.get_record(tenant_id="tenant-a", news_id="fragments"))
    assert evidence["body"] is None
    assert evidence["fragments"][0]["text"] == "只有切片，不代表完整正文"
    with pytest.raises(ValueError, match="different tenant"):
        asyncio.run(repo.get_record(tenant_id="tenant-b", news_id="full"))


@pytest.mark.parametrize("tamper", ["body", "manifest", "selection", "source"])
def test_tampering_is_rejected_before_any_write(store, tamper):
    package = build_package([candidate()], tenant_id="tenant-a")
    altered = deepcopy(package)
    if tamper == "body":
        altered["versions"][0]["body"] = "篡改正文"
    elif tamper == "manifest":
        altered["manifest"]["counts"]["news_count"] += 1
    elif tamper == "selection":
        altered["items"][0]["record_sha256"] = "0" * 64
    else:
        altered["provenance"][0]["source"]["file_sha256"] = "b" * 64
    with pytest.raises(ValueError):
        store.import_release(altered)
    assert table_count(store, releases) == 0
    assert table_count(store, versions) == 0


def test_verification_failure_rolls_back_entire_release(store, monkeypatch):
    package = build_package([candidate()], tenant_id="tenant-a")
    original = store._read_package

    def corrupted_read(*args):
        result = original(*args)
        result["versions"][0]["body"] = "Simulated persistence corruption"
        return result

    monkeypatch.setattr(store, "_read_package", corrupted_read)
    with pytest.raises(ValueError, match="record/hash"):
        store.import_release(package)
    assert all(table_count(store, table) == 0 for table in (releases, versions, provenance, items))


def test_existing_release_manifest_cannot_be_replaced(store):
    package, result = publish(store, [candidate()])
    different = build_package([candidate(body="新正文。" * 30)], tenant_id="tenant-a")
    # Even caller attempts to reuse an old release ID cannot pass identity checks.
    different["manifest"]["release_id"] = result["release_id"]
    with pytest.raises(ValueError, match="identity"):
        store.import_release(different)
    assert table_count(store, releases) == 1
    assert store.import_release(package)["import_status"] == "already_present"


def test_persisted_body_tampering_is_detected_by_reader(store):
    _, result = publish(store, [candidate()])
    with store.engine.begin() as connection:
        connection.execute(update(versions).values(body="Changed outside the importer"))
    with pytest.raises(ValueError, match="record/hash"):
        store.get_record("tenant-a", result["release_id"], "n1")
    with pytest.raises(ValueError, match="record/hash"):
        store.list_records("tenant-a", result["release_id"])
    with pytest.raises(ValueError, match="record/hash"):
        store.get_records("tenant-a", result["release_id"], ("n1",))
