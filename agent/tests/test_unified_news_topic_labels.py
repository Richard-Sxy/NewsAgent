"""Frozen text/topic joins and evidence validation; no database or model calls."""
from copy import deepcopy
from hashlib import sha256

import pytest

from app.modules.behavior_tags.tagging import TopicPolicy, tag_article
from app.modules.news_storage.model import build_package, digest
from app.modules.news_storage.topic_labels import (
    build_tag_package, load_tag_package, save_tag_package, validate_tag_package,
)


@pytest.fixture
def policy():
    return TopicPolicy.model_validate({
        "schema_version": 1, "policy_version": "test-topics-v1",
        "catalog": {"schema_version": 1, "taxonomy_version": "test-taxonomy-v1", "status": "draft",
            "topics": [
                {"topic_id": "tech", "name": "科技", "provenance": "user_confirmed"},
                {"topic_id": "tech.ai", "name": "人工智能", "parent_topic_id": "tech",
                 "provenance": "draft_extension"},
            ]},
        "rules": [{"rule_id": "ai-v1", "topic_id": "tech.ai", "title_terms": ["AI"],
            "body_terms": ["人工智能"], "negative_terms": ["排除词"],
            "synthetic_categories": ["科技"], "title_weight": 1.0, "body_weight": 0.6}],
    })


def observation(news_id="news-1", **changes):
    return {"news_id": news_id, "title": "AI 新闻", "body": "人工智能公开报道。",
        "url": f"https://news.qq.com/rain/a/{news_id}", "publish_time": "2026-10-23T00:00:00+00:00",
        "content_type": "article", "content_state": "body_available", "fragments": [],
        "quality_flags": [], "priority": 10,
        "source": {"package": "test-public", "path": f"fixtures/{news_id}.json",
                   "file_sha256": "a" * 64}, **changes}


def content(*observations, tenant_id="tenant-public"):
    return build_package(list(observations or (observation(),)), tenant_id=tenant_id)


def old_label(policy, source=None, **changes):
    source = source or observation()
    return tag_article({"tenant_id": "tenant-legacy", "news_id": source["news_id"],
        "title": source["title"], "content": source["body"] or "",
        "news_content_synthetic": False}, policy) | changes


def legacy(policy, labels=()):
    return {"policy": policy.model_dump(mode="json"), "labels": list(labels), "binding": {
        "package_version": "behavior-tags-local-v1", "manifest_sha256": "b" * 64,
        "identity_sha256": "c" * 64, "policy_sha256": "d" * 64,
        "content_labels_sha256": "e" * 64, "taxonomy_version": policy.catalog.taxonomy_version,
        "policy_version": policy.policy_version}}


def reseal(package):
    """Model an internally rehashed but semantically invalid artifact."""
    for row in package["labels"]:
        row["label_sha256"] = digest({k: v for k, v in row.items() if k != "label_sha256"})
    manifest = {k: v for k, v in package["manifest"].items()
                if k not in {"identity_sha256", "tag_release_id", "counts"}}
    identity = digest({"manifest": manifest, "policy": package["policy"], "labels": package["labels"]})
    package["manifest"].update(identity_sha256=identity, tag_release_id="tags-" + identity[:24])


def test_labels_cover_all_content_and_preserve_incomplete_states(policy):
    package = content(observation(), observation("title", body=None, content_state="title_only"),
        observation("fragment", title=None, body=None, content_state="fragments_only",
                    fragments=[{"chunk_index": 0, "text": "人工智能片段"}]),
        observation("review", quality_flags=["metadata_review_required"]),
        observation("unknown", title="普通报道", body="没有分类关键词"))
    tags = build_tag_package(package, legacy(policy))
    rows = {row["news_id"]: row for row in tags["labels"]}
    assert set(rows) == {item["news_id"] for item in package["items"]}
    assert rows["news-1"]["topic_ids"] == ["tech", "tech.ai"]
    assert rows["title"]["input_scope"] == "title_only"
    assert {m["field"] for m in rows["title"]["matches"]} == {"title"}
    assert rows["fragment"]["label_status"] == "insufficient_input"
    assert rows["review"]["label_status"] == "metadata_review"
    assert rows["unknown"]["label_status"] == "unclassified"
    assert all(row["labels_are_inferred"] is True for row in rows.values())
    assert tags["manifest"]["counts"]["news_count"] == 5


def test_exact_legacy_text_and_matching_output_is_verified_without_tenant_aliasing(policy):
    prior = old_label(policy)
    tags = build_tag_package(content(), legacy(policy, [prior]))
    row = tags["labels"][0]
    assert row["provenance"] == "legacy_verified"
    assert row["legacy_refs"] == [{"source_tenant_id": "tenant-legacy", "news_id": "news-1",
        "content_sha256": sha256(observation()["body"].encode()).hexdigest(),
        "label_sha256": digest(prior), "provenance": "rule_inferred", "compatible": True,
        "topic_ids": ["tech", "tech.ai"]}]
    assert tags["manifest"]["tenant_id"] == "tenant-public"


@pytest.mark.parametrize("change", [
    {"title": "不同标题"}, {"content_sha256": "f" * 64}, {"news_content_synthetic": True},
    {"news_content_synthetic": None},
])
def test_old_labels_require_actual_same_title_and_body(policy, change):
    row = build_tag_package(content(), legacy(policy, [old_label(policy, **change)]))["labels"][0]
    assert row["legacy_refs"] == []
    assert row["provenance"] == "rule_inferred"


@pytest.mark.parametrize("change", [
    {"topic_scores": {"tech": 0.25, "tech.ai": 0.25}}, {"matches": []},
    {"provenance": "synthetic_category_fallback"},
    {"taxonomy_version": "old-taxonomy"}, {"tagging_policy_version": "old-policy"},
])
def test_legacy_match_is_not_verified_if_inference_or_policy_differs(policy, change):
    row = build_tag_package(content(), legacy(policy, [old_label(policy, **change)]))["labels"][0]
    assert len(row["legacy_refs"]) == 1
    assert row["legacy_refs"][0]["compatible"] is False
    assert row["provenance"] == "rule_inferred"


def test_synthetic_fallback_never_supplies_topics_for_new_content(policy):
    observed = observation(title="普通新闻", body="普通报道内容", category="科技",
                           category_data_synthetic=True)
    old = old_label(policy, observed, provenance="synthetic_category_fallback",
                    topic_ids=["tech", "tech.ai"], topic_scores={"tech": .25, "tech.ai": .25})
    row = build_tag_package(content(observed), legacy(policy, [old]))["labels"][0]
    assert row["label_status"] == "unclassified"
    assert row["topic_ids"] == [] and row["matches"] == []
    assert row["legacy_refs"][0]["compatible"] is False


def test_release_is_deterministic_for_content_and_legacy_input_order(policy):
    a, b = observation(), observation("news-2", title="另一个 AI 报道")
    priors = [old_label(policy, a), old_label(policy, b)]
    expected = build_tag_package(content(a, b), legacy(policy, priors))
    assert build_tag_package(content(b, a), legacy(policy, reversed(priors))) == expected


@pytest.mark.parametrize("key,value", [("tenant_id", "wrong-tenant"),
    ("content_release_id", "news-wrong"), ("content_identity_sha256", "f" * 64)])
def test_rehashed_wrong_content_binding_is_rejected(policy, key, value):
    package = content()
    tags = build_tag_package(package, legacy(policy))
    tags["manifest"][key] = value
    reseal(tags)
    with pytest.raises(ValueError, match="different content release"):
        validate_tag_package(tags, package)


@pytest.mark.parametrize("key,value", [("policy_version", "wrong-policy"),
    ("taxonomy_version", "wrong-taxonomy"), ("manifest_sha256", "bad-sha"),
    ("content_labels_sha256", "bad-sha")])
def test_invalid_source_binding_is_rejected(policy, key, value):
    old = legacy(policy)
    old["binding"][key] = value
    with pytest.raises(ValueError):
        build_tag_package(content(), old)


def test_unknown_legacy_source_fields_cannot_change_binding_contract(policy):
    old = legacy(policy)
    old["binding"]["unverified_extra"] = "another-source"
    with pytest.raises(ValueError):
        build_tag_package(content(), old)


@pytest.mark.parametrize("change", [{"title": "AI " + "长" * 1024}, {"body": "长" * 1_000_001}])
def test_valid_storage_content_beyond_tagger_limits_remains_visible(policy, change):
    package = content(observation(**change), observation("processable"))
    tags = build_tag_package(package, legacy(policy))
    by_id = {row["news_id"]: row for row in tags["labels"]}
    assert by_id["news-1"]["label_status"] == "insufficient_input"
    assert by_id["news-1"]["input_scope"] == "none"
    assert by_id["news-1"]["matches"] == []
    assert by_id["processable"]["label_status"] == "tagged"
    validate_tag_package(tags, package)


def test_rehashed_offset_tamper_fails_against_selected_text(policy):
    package = content()
    tags = build_tag_package(package, legacy(policy))
    tags["labels"][0]["matches"][0].update(start=1, end=3)
    reseal(tags)
    with pytest.raises(ValueError, match="frozen text"):
        validate_tag_package(tags, package)


def test_tagged_news_cannot_be_silently_reclassified_after_rehash(policy):
    package = content()
    tags = build_tag_package(package, legacy(policy))
    tags["labels"][0].update(topic_ids=[], topic_scores={}, matches=[],
                             label_status="unclassified", provenance="unclassified")
    tags["manifest"]["counts"].update(tagged_count=0, unclassified_count=1, topic_memberships_count=0)
    reseal(tags)
    with pytest.raises(ValueError):
        validate_tag_package(tags, package)


def test_present_evidence_cannot_override_negative_terms(policy):
    package = content(observation(title="AI 新闻 排除词"))
    tags = build_tag_package(package, legacy(policy))
    assert tags["labels"][0]["label_status"] == "unclassified"
    tags["labels"][0].update(topic_ids=["tech", "tech.ai"], topic_scores={"tech": 1.0, "tech.ai": 1.0},
        label_status="tagged", provenance="rule_inferred", matches=[{
            "topic_id": "tech.ai", "rule_id": "ai-v1", "field": "title", "term": "AI",
            "start": 0, "end": 2, "score": 1.0, "source": "rule_keyword_match"}])
    tags["manifest"]["counts"].update(tagged_count=1, unclassified_count=0, topic_memberships_count=2)
    reseal(tags)
    with pytest.raises(ValueError):
        validate_tag_package(tags, package)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_coverage_rejects_missing_duplicate_and_unknown_news(policy, mutation):
    package = content(observation(), observation("news-2"))
    tags = build_tag_package(package, legacy(policy))
    if mutation == "missing":
        tags["labels"].pop()
    elif mutation == "duplicate":
        tags["labels"].append(deepcopy(tags["labels"][0]))
    else:
        tags["labels"][0]["news_id"] = "unrelated"
    reseal(tags)
    with pytest.raises(ValueError):
        validate_tag_package(tags, package)


def test_save_load_roundtrip_and_refuses_overwrite(policy, tmp_path):
    tags = build_tag_package(content(), legacy(policy, [old_label(policy)]))
    output = tmp_path / "topics"
    save_tag_package(tags, output)
    assert load_tag_package(output) == tags
    original = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        save_tag_package(tags, output)
    assert (output / "manifest.json").read_bytes() == original


@pytest.mark.parametrize("name", ["labels.jsonl", "policy.json"])
def test_file_tamper_and_symlinks_cannot_replace_frozen_inputs(policy, tmp_path, name):
    tags = build_tag_package(content(), legacy(policy))
    output = tmp_path / "topics"
    save_tag_package(tags, output)
    member = output / name
    raw = member.read_bytes()
    member.write_bytes(raw + b" ")
    with pytest.raises(ValueError):
        load_tag_package(output)
    member.unlink()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(raw)
    member.symlink_to(replacement)
    with pytest.raises(ValueError):
        load_tag_package(output)
