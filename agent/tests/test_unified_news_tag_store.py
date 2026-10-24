"""Transactional tag releases, explicit scoping and filtered pagination."""

from copy import deepcopy

import pytest
from sqlalchemy import create_engine, delete, func, select, text, update

from app.modules.news_storage.model import build_package
from app.modules.news_storage.store import FrozenNewsStore, items, releases, versions
from app.modules.news_storage.tag_store import FrozenNewsTagStore, tag_releases, tags, topic_memberships
from app.modules.news_storage.topic_labels import build_tag_package


def candidate(news_id="n1", **overrides):
    record = dict(news_id=news_id, title="航天最新进展", body="航天工程正在稳步推进。" * 20,
                  url=f"https://news.qq.com/rain/a/{news_id}", publish_time="2026-10-01T08:00:00+08:00",
                  content_type="article", content_state="body_available", fragments=[], quality_flags=[],
                  priority=10, source={"package": "fixture", "path": f"fixtures/{news_id}.json",
                                       "file_sha256": "a" * 64, "manifest_path": None,
                                       "manifest_sha256": None, "source_content_version": "1",
                                       "fetched_at": None})
    record.update(overrides)
    return record


def legacy(policy_version="topics-policy-test-v1"):
    return {
        "labels": [],
        "policy": {"schema_version": 1, "policy_version": policy_version,
                   "catalog": {"taxonomy_version": "topics-test-v1", "status": "draft", "topics": [
                       {"topic_id": "tech", "name": "科技", "provenance": "user_confirmed"},
                       {"topic_id": "tech.space", "name": "航天", "parent_topic_id": "tech",
                        "provenance": "draft_extension"},
                       {"topic_id": "finance", "name": "财经", "provenance": "user_confirmed"}]},
                   "rules": [{"rule_id": "space.v1", "topic_id": "tech.space", "title_terms": ["航天"],
                              "body_terms": ["航天"]},
                             {"rule_id": "finance.v1", "topic_id": "finance", "title_terms": ["市场"],
                              "body_terms": ["市场"]}]},
        "binding": {"package_version": "behavior-tags-local-v1", "policy_version": policy_version,
                    "taxonomy_version": "topics-test-v1", "manifest_sha256": "a" * 64,
                    "identity_sha256": "b" * 64, "policy_sha256": "c" * 64,
                    "content_labels_sha256": "d" * 64},
    }


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tags.sqlite'}")
    content = FrozenNewsStore(engine)
    content.initialize()
    result = FrozenNewsTagStore(content)
    result.initialize()
    yield result
    engine.dispose()


def publish(store, candidates=None, *, tenant="tenant-a", policy_version="topics-policy-test-v1"):
    content = build_package(candidates or [candidate()], tenant_id=tenant)
    store.content_store.import_release(content)
    package = build_tag_package(content, legacy(policy_version))
    summary = store.import_release(package)
    scope = (tenant, content["manifest"]["release_id"], summary["tag_release_id"])
    return content, package, summary, scope


def count(store, table):
    with store.engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_roundtrip_is_idempotent_and_original_content_unchanged(store):
    content, package, result, scope = publish(store)
    assert result["import_status"] == "created"
    assert result["legacy_source"] == package["manifest"]["legacy_source"]
    assert store.import_release(package)["import_status"] == "already_present"
    assert [count(store, table) for table in (tag_releases, tags, topic_memberships)] == [1, 1, 2]
    store.initialize()
    row = store.get_record(*scope, "n1")
    assert row["body"] == content["versions"][0]["body"]
    assert row["tags"]["topic_ids"] == ["tech", "tech.space"]
    assert row["tags"]["topic_names"] == ["科技", "航天"]
    assert all(row["tags"][key] == value for key, value in package["labels"][0].items())
    assert row["tags"]["labels_are_inferred"] is True
    assert "tags" not in store.content_store.get_record(*scope[:2], "n1")
    with store.engine.connect() as connection:
        assert store.content_store._read_package(connection, *scope[:2]) == content
        sql_row = connection.execute(text(
            "SELECT news_id,topic_ids,tag_status,input_scope FROM news_content_tagged_selected "
            "WHERE tenant_id=:tenant AND content_release_id=:content AND tag_release_id=:tags"
        ), {"tenant": scope[0], "content": scope[1], "tags": scope[2]}).mappings().one()
    assert sql_row["news_id"] == "n1"
    assert sql_row["tag_status"] == "tagged"
    assert sql_row["input_scope"] == "title_body"
    topics = store.get_topics(*scope)
    assert topics["topic_counts"] == {"tech": 1, "tech.space": 1, "finance": 0}
    assert topics["catalog"]["taxonomy_version"] == "topics-test-v1"


def test_topic_filter_happens_before_limit_and_is_any_match_without_duplicates(store):
    _, _, _, scope = publish(store, [
        candidate("a", title="市场走势", body="市场行情" * 20),
        candidate("b", title="市场走势", body="市场行情" * 20),
        candidate("c"), candidate("d"),
        candidate("e", title="航天视频", body=None, content_state="title_only", content_type="video"),
    ])
    rows = store.list_records(*scope, topic_ids=("tech.space",), limit=1)
    assert [row["news_id"] for row in rows] == ["c"]
    assert [row["news_id"] for row in store.list_records(
        *scope, topic_ids=("tech", "tech.space"), after_news_id="c", limit=1)] == ["d"]
    assert [row["news_id"] for row in store.list_records(
        *scope, topic_ids=("tech", "tech.space"), body_only=True)] == ["c", "d"]
    assert [row["news_id"] for row in store.list_records(
        *scope, topic_ids=("finance", "tech.space"))] == ["a", "b", "c", "d", "e"]
    assert store.list_records(*scope, topic_ids=("nonexistent",)) == []


def test_partial_and_metadata_review_news_remain_visible(store):
    _, package, result, scope = publish(store, [
        candidate("body"),
        candidate("title", title="航天报道", body=None, content_state="title_only"),
        candidate("fragment", title=None, body=None, content_state="fragments_only",
                  fragments=[{"chunk_index": 0, "text": "历史航天片段"}]),
        candidate("review", quality_flags=["metadata_review_required"]),
        candidate("other", title="今日生活", body="生活随笔" * 20),
    ])
    rows = {row["news_id"]: row for row in store.list_records(*scope)}
    assert len(rows) == len(package["labels"]) == result["counts"]["news_count"] == 5
    assert rows["title"]["tags"]["input_scope"] == "title_only"
    assert rows["fragment"]["tags"]["label_status"] == "insufficient_input"
    assert rows["review"]["tags"]["label_status"] == "metadata_review"
    assert rows["other"]["tags"]["label_status"] == "unclassified"
    assert rows["fragment"]["body"] is None


def test_tenant_content_and_tag_release_scopes_are_required(store):
    _, _, _, scope = publish(store)
    for bad in (("tenant-b", *scope[1:]), (scope[0], "missing", scope[2]), (*scope[:2], "missing")):
        for operation in (store.get_record, store.list_records, store.summary, store.get_topics):
            with pytest.raises(ValueError):
                operation(*bad, "n1") if operation == store.get_record else operation(*bad)
    assert store.get_record(*scope, "missing") is None
    _, _, _, other = publish(store, tenant="tenant-b")
    assert store.get_record(*other, "n1")["tenant_id"] == "tenant-b"


def test_new_policy_or_content_release_does_not_change_old_labels(store):
    original, _, _, scope = publish(store)
    old_record = store.get_record(*scope, "n1")
    second = build_tag_package(original, legacy("topics-policy-test-v2"))
    second_summary = store.import_release(second)
    assert second_summary["tag_release_id"] != scope[2]
    _, _, _, later = publish(store, [candidate(body="正文已更新：航天" * 30)])
    assert later[1] != scope[1]
    assert store.get_record(*scope, "n1") == old_record
    with pytest.raises(ValueError):
        store.get_record(scope[0], later[1], scope[2], "n1")


@pytest.mark.parametrize("kind", ["row", "manifest", "topic_memberships"])
def test_modified_package_is_rejected_before_write(store, kind):
    content = build_package([candidate()], tenant_id="tenant-a")
    store.content_store.import_release(content)
    package = build_tag_package(content, legacy())
    if kind == "row":
        package["labels"][0]["topic_ids"] = ["finance"]
    elif kind == "manifest":
        package["manifest"]["content_identity_sha256"] = "f" * 64
    else:
        package["manifest"]["counts"]["topic_memberships_count"] += 1
    with pytest.raises(ValueError):
        store.import_release(package)
    assert [count(store, table) for table in (tag_releases, tags, topic_memberships)] == [0, 0, 0]


def test_import_requires_exact_content_release_in_database(store):
    content = build_package([candidate()], tenant_id="tenant-a")
    package = build_tag_package(content, legacy())
    with pytest.raises(ValueError, match="unknown frozen release"):
        store.import_release(package)
    assert count(store, tag_releases) == 0


def test_failed_post_write_validation_rolls_back_all_tag_tables(store, monkeypatch):
    content = build_package([candidate()], tenant_id="tenant-a")
    store.content_store.import_release(content)
    package = build_tag_package(content, legacy())
    original = store._read_package

    def damaged(*args):
        result = original(*args)
        result["labels"][0]["label_status"] = "unclassified"
        return result

    monkeypatch.setattr(store, "_read_package", damaged)
    with pytest.raises(ValueError):
        store.import_release(package)
    assert [count(store, table) for table in (tag_releases, tags, topic_memberships)] == [0, 0, 0]
    assert [count(store, table) for table in (releases, versions, items)] == [1, 1, 1]


@pytest.mark.parametrize("kind", ["label", "column", "membership", "delete_label", "release"])
def test_persisted_corruption_is_detected_on_read_and_reimport(store, kind):
    _, package, _, scope = publish(store)
    with store.engine.begin() as connection:
        if kind == "label":
            changed = deepcopy(package["labels"][0])
            changed["matches"][0]["term"] = "篡改"
            connection.execute(update(tags).values(label=changed))
        elif kind == "column":
            connection.execute(update(tags).values(label_status="unclassified"))
        elif kind == "membership":
            connection.execute(delete(topic_memberships).where(topic_memberships.c.topic_id == "tech.space"))
        elif kind == "delete_label":
            connection.execute(delete(topic_memberships))
            connection.execute(delete(tags))
        else:
            connection.execute(update(tag_releases).values(content_identity_sha256="f" * 64))
    with pytest.raises(ValueError):
        store.get_record(*scope, "n1")
    with pytest.raises(ValueError):
        store.import_release(package)


def test_read_does_not_rerun_tagger(store, monkeypatch):
    _, _, _, scope = publish(store)

    def forbidden(*args, **kwargs):
        raise AssertionError("a frozen read must not run inference")

    monkeypatch.setattr("app.modules.news_storage.topic_labels.tag_article", forbidden)
    assert store.get_record(*scope, "n1")["tags"]["label_status"] == "tagged"
    assert len(store.list_records(*scope, topic_ids=("tech",))) == 1


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": True}, {"limit": 1001},
                                    {"after_news_id": None}, {"body_only": 1},
                                    {"topic_ids": "tech"}, {"topic_ids": [""]}])
def test_invalid_pagination_arguments_fail_closed(store, options):
    _, _, _, scope = publish(store)
    with pytest.raises(ValueError):
        store.list_records(*scope, **options)
