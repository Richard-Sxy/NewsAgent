"""Content release integrity without a database, Settings, or model calls."""

from copy import deepcopy
from itertools import permutations

import pytest

from app.modules.news_storage.model import build_package, validate_package


def observed_article(*, news_id="20261023A0000100", body="仅供单元测试的新闻正文。" * 12,
                     package="public-fixture-a", priority=10, **changes):
    """An explicit test observation, not a fetched or enterprise news record."""
    row = {
        "news_id": news_id,
        "title": "测试新闻标题",
        "body": body,
        "url": f"https://news.qq.com/rain/a/{news_id}",
        "publish_time": "2026-10-23T10:00:00+08:00",
        "content_type": "article",
        "content_state": "body_available",
        "fragments": [],
        "quality_flags": ["cached_body_completeness_unreviewed"],
        "source": {
            "package": package,
            "path": f"fixtures/{package}/{news_id}.json",
            "file_sha256": "a" * 64,
            "manifest_path": f"fixtures/{package}/manifest.json",
            "manifest_sha256": "b" * 64,
        },
        "priority": priority,
    }
    row.update(changes)
    return row


def release(*rows, tenant_id="tenant-test"):
    return build_package(list(rows), tenant_id=tenant_id)


def selected_record(package, news_id="20261023A0000100"):
    item = next(row for row in package["items"] if row["news_id"] == news_id)
    return next(row for row in package["versions"]
                if row["news_id"] == news_id
                and row["record_sha256"] == item["record_sha256"])


def test_release_is_deterministic_across_input_order_and_exact_duplicates():
    rows = [observed_article(), observed_article(package="public-fixture-b", priority=20),
            observed_article(news_id="20261023A0000200")]
    expected = release(*rows)
    for ordered in permutations(rows):
        assert release(*ordered) == expected
    assert release(*rows, deepcopy(rows[0])) == expected
    assert expected["manifest"]["counts"]["news_count"] == 2
    assert expected["manifest"]["counts"]["version_count"] == 2
    assert expected["manifest"]["counts"]["provenance_count"] == 3


def test_same_news_with_different_bodies_retains_versions_and_conflict_flag():
    preferred = observed_article(body="优先来源正文。" * 20)
    other = observed_article(body="另一来源保留的不同正文。" * 20,
                             package="public-fixture-b", priority=30)
    package = release(preferred, other)
    assert {row["body"] for row in package["versions"]} == {preferred["body"], other["body"]}
    assert len({row["body_sha256"] for row in package["versions"]}) == 2
    assert selected_record(package)["body"] == preferred["body"]
    assert "conflicting_body_variants" in package["items"][0]["quality_flags"]
    assert package["manifest"]["counts"]["conflicting_body_news_count"] == 1


def test_same_content_from_multiple_sources_deduplicates_without_losing_evidence():
    package = release(observed_article(), observed_article(package="public-fixture-b", priority=20))
    assert len(package["versions"]) == 1
    assert len(package["items"]) == 1
    assert {row["source"]["package"] for row in package["provenance"]} == {
        "public-fixture-a", "public-fixture-b"}
    version_hash = package["versions"][0]["record_sha256"]
    assert all(row["record_sha256"] == version_hash for row in package["provenance"])
    assert "conflicting_body_variants" not in package["items"][0]["quality_flags"]


def test_selection_prefers_captured_body_over_a_higher_priority_title():
    body = observed_article(priority=100)
    title = observed_article(package="public-fixture-headlines", priority=0,
                             content_state="title_only", body=None)
    package = release(body, title)
    assert selected_record(package)["content_state"] == "body_available"
    assert package["manifest"]["counts"]["body_available_count"] == 1
    assert package["manifest"]["counts"]["title_only_count"] == 0
    assert len(package["versions"]) == 2


def test_missing_metadata_stays_missing_and_fragments_are_not_reconstructed():
    fragments = [{"chunk_index": 0, "text": "只有历史切片，不能证明全文完整。", "chunk_id": "test:1"},
                 {"chunk_index": 0, "text": "另一个相同编号的历史切片。", "chunk_id": "test:2"}]
    row = observed_article(content_state="fragments_only", body=None, title=None,
                           url=None, publish_time=None, fragments=fragments,
                           quality_flags=["duplicate_chunk_index"])
    package = release(row)
    record = selected_record(package)
    assert record["body"] is None
    assert record["body_sha256"] is None
    assert record["title"] is None and record["url"] is None and record["publish_time"] is None
    assert record["fragments"] == fragments
    assert {"missing_title", "missing_url", "missing_publish_time", "duplicate_chunk_index"} <= set(record["quality_flags"])
    assert package["manifest"]["counts"]["fragments_only_count"] == 1


@pytest.mark.parametrize("changes", [
    {"body": None},
    {"content_state": "fragments_only", "body": None, "fragments": []},
    {"content_state": "fragments_only", "fragments": [{"chunk_index": 0, "text": "切片"}]},
    {"content_state": "title_only"},
    {"content_state": "title_only", "body": None, "title": None},
])
def test_inconsistent_body_and_partial_evidence_states_are_rejected(changes):
    with pytest.raises(ValueError):
        release(observed_article(**changes))


def test_title_only_preserves_video_type_without_inventing_body():
    package = release(observed_article(content_state="title_only", content_type="video", body=None))
    record = selected_record(package)
    assert record["content_type"] == "video"
    assert record["body"] is None
    assert record["fragments"] == []
    assert package["manifest"]["counts"]["title_only_count"] == 1


def test_equivalent_timezone_offsets_have_the_same_record_and_release_identity():
    shanghai = release(observed_article())
    utc = release(observed_article(publish_time="2026-10-23T02:00:00Z"))
    assert shanghai == utc
    assert selected_record(shanghai)["publish_time"] == "2026-10-23T02:00:00+00:00"


@pytest.mark.parametrize("value", ["2026-10-23T10:00:00", "2026-10-23", "not-a-date"])
def test_unzoned_or_invalid_publication_time_is_not_silently_assumed(value):
    with pytest.raises(ValueError):
        release(observed_article(publish_time=value))


def test_tenant_and_provenance_are_part_of_release_identity():
    original = release(observed_article())
    other_tenant = release(observed_article(), tenant_id="tenant-other")
    other_source = release(observed_article(package="public-fixture-b"))
    assert original["versions"] == other_tenant["versions"] == other_source["versions"]
    assert len({row["manifest"]["release_id"] for row in (original, other_tenant, other_source)}) == 3


@pytest.mark.parametrize("field,value", [
    ("body", "正文已被篡改"),
    ("body_sha256", "0" * 64),
    ("record_sha256", "0" * 64),
])
def test_changed_content_or_record_hash_is_rejected(field, value):
    package = release(observed_article())
    package["versions"][0][field] = value
    with pytest.raises(ValueError, match="record/hash mismatch"):
        validate_package(package)


def test_switching_selected_version_is_rejected_even_when_the_version_exists():
    package = release(observed_article(), observed_article(body="另一个正文版本。" * 20,
                                                         package="public-fixture-b", priority=30))
    preferred_hash = package["items"][0]["record_sha256"]
    other = next(row for row in package["versions"] if row["record_sha256"] != preferred_hash)
    package["items"][0]["record_sha256"] = other["record_sha256"]
    with pytest.raises(ValueError, match="selection differs"):
        validate_package(package)


def test_removing_selected_news_is_rejected():
    package = release(observed_article(), observed_article(news_id="20261023A0000200"))
    package["items"].pop()
    with pytest.raises(ValueError, match="omits"):
        validate_package(package)


def test_unbound_content_version_is_rejected():
    package = release(observed_article())
    package["provenance"] = []
    with pytest.raises(ValueError, match="every version needs provenance"):
        validate_package(package)


@pytest.mark.parametrize("field", ["news_count", "version_count", "provenance_count", "body_available_count"])
def test_manifest_counts_must_match_bound_content(field):
    package = release(observed_article())
    package["manifest"]["counts"][field] += 1
    with pytest.raises(ValueError, match="counts mismatch"):
        validate_package(package)


@pytest.mark.parametrize("field,value", [("identity_sha256", "0" * 64), ("release_id", "news-incorrect")])
def test_release_identity_tampering_is_rejected(field, value):
    package = release(observed_article())
    package["manifest"][field] = value
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_package(package)


def test_source_evidence_cannot_be_changed_without_changing_release_identity():
    package = release(observed_article())
    package["provenance"][0]["source"]["file_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_package(package)


@pytest.mark.parametrize("path", ["/private/source.json", "fixtures/../source.json"])
def test_source_paths_must_stay_repository_relative(path):
    row = observed_article()
    row["source"]["path"] = path
    with pytest.raises(ValueError, match="repository-relative"):
        release(row)


def test_input_observations_are_not_modified_and_nested_fragments_are_copied():
    row = observed_article(content_state="fragments_only", body=None,
                           fragments=[{"chunk_index": 0, "text": "测试切片"}])
    before = deepcopy(row)
    package = release(row)
    assert row == before
    row["fragments"][0]["text"] = "输入对象后续变化"
    row["source"]["file_sha256"] = "c" * 64
    validate_package(package)
    assert selected_record(package)["fragments"][0]["text"] == "测试切片"
