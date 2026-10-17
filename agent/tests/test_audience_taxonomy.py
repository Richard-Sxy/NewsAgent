"""Topic hierarchy and draft provenance are independent of user-level data."""

from pathlib import Path

from pydantic import ValidationError
import pytest
import yaml

from app.audience.taxonomy import MAX_CATALOG_BYTES, MAX_TOPICS, Topic, TopicCatalog, load_topic_catalog


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def topic(topic_id="tech", name="科技", parent_topic_id=None, **overrides):
    return dict(topic_id=topic_id, name=name, parent_topic_id=parent_topic_id,
                provenance="user_confirmed") | overrides


def catalog(topics=None, **overrides):
    return TopicCatalog.model_validate(dict(taxonomy_version="topics-test-v1", status="draft",
        topics=topics if topics is not None else [topic(), topic("tech.nature", "自然", "tech")]) | overrides)


def test_local_catalog_preserves_confirmed_topics_and_marks_extensions():
    value = load_topic_catalog(DEPLOY / "audience-topics.local.yml")
    assert value.taxonomy_version == "news-interest-demo-v1"
    assert value.status == "draft"
    assert {item.topic_id: item.name for item in value.topics if item.parent_topic_id is None} == {
        "tech": "科技", "finance": "财经", "ai": "AI", "military": "军事", "education": "教育"}
    confirmed = {item.topic_id for item in value.topics if item.provenance == "user_confirmed"}
    assert confirmed == {"tech", "tech.nature", "finance", "ai", "military", "education"}
    assert len([item for item in value.topics if item.parent_topic_id is not None]) == 20
    assert value.ancestor_ids("tech.nature") == ("tech.nature", "tech")
    assert value.ancestor_ids("ai") == ("ai",)


def test_enterprise_template_remains_explicitly_unapproved():
    value = load_topic_catalog(DEPLOY / "audience-topics.enterprise.example.yml")
    assert value.status == "draft"
    assert value.taxonomy_version != "news-interest-demo-v1"


def test_slash_namespace_is_supported_and_serialization_matches_frontend_contract():
    value = catalog([topic(), topic("tech/nature", "自然", "tech")])
    value.validate_ids(("tech", "tech/nature"))
    assert value.ancestor_ids("tech/nature") == ("tech/nature", "tech")
    assert value.model_dump(mode="json")["topics"][1] == dict(
        topic_id="tech/nature", name="自然", parent_topic_id="tech", enabled=True,
        provenance="user_confirmed")


@pytest.mark.parametrize("topics,match", [
    ([topic(), topic()], "unique"),
    ([topic("tech.nature", "自然", "missing")], "unknown parent"),
    ([topic(), topic("finance.nature", "自然", "tech")], "parent namespace"),
    ([topic(), topic("tech.nature", "自然")], "root topic_id"),
    ([topic(), topic("tech.nature", "自然", "tech"), topic("tech.nature.space", "航天", "tech.nature")], "two levels"),
    ([topic(), topic("tech.nature.space", "航天", "tech")], "one leaf segment"),
    ([topic(), topic("tech.nature/space", "航天", "tech")], "one leaf segment"),
    ([topic(enabled=False), topic("tech.nature", "自然", "tech")], "disabled parent"),
    ([topic("tech", parent_topic_id="tech")], "two levels"),
    ([topic("tech", parent_topic_id="finance"), topic("finance", "财经", "tech")], "two levels"),
])
def test_invalid_hierarchy_is_rejected(topics, match):
    with pytest.raises(ValueError, match=match):
        catalog(topics)


def test_disabled_root_and_child_may_remain_in_catalog_but_cannot_be_selected():
    value = catalog([topic(enabled=False), topic("tech.nature", "自然", "tech", enabled=False)])
    for topic_id in ("tech", "tech.nature"):
        with pytest.raises(ValueError, match="disabled"):
            value.validate_ids((topic_id,))
        with pytest.raises(ValueError, match="disabled"):
            value.ancestor_ids(topic_id)


@pytest.mark.parametrize("topic_ids,match", [
    (("missing",), "unknown"), (("tech", "tech"), "duplicates"),
    ("tech", "bounded sequence"), (None, "bounded sequence"), (("tech", None), "contain strings"),
    (("tech",) * (MAX_TOPICS + 1), "bounded sequence"),
])
def test_invalid_requested_topics_are_rejected(topic_ids, match):
    with pytest.raises(ValueError, match=match):
        catalog().validate_ids(topic_ids)


def test_unknown_ancestor_and_empty_selection():
    value = catalog()
    assert value.validate_ids(()) is None
    with pytest.raises(ValueError, match="unknown"):
        value.ancestor_ids("missing")


@pytest.mark.parametrize("overrides", [
    {"topic_id": "Tech"}, {"topic_id": "tech..nature"}, {"topic_id": " tech"},
    {"name": " "}, {"name": " 科技"}, {"name": "科技\n自然"}, {"name": 1},
    {"enabled": "true"}, {"provenance": "enterprise_approved"}, {"unknown": 1},
])
def test_topic_shape_is_strict(overrides):
    with pytest.raises(ValueError):
        Topic.model_validate(topic() | overrides)


@pytest.mark.parametrize("overrides", [
    {"schema_version": True}, {"schema_version": "1"}, {"schema_version": 2},
    {"taxonomy_version": " "}, {"taxonomy_version": " version-v1"},
    {"status": "production"}, {"unknown": 1}, {"topics": []},
])
def test_catalog_shape_is_strict(overrides):
    with pytest.raises(ValueError):
        catalog(**overrides)


def test_catalog_size_is_bounded_and_models_are_frozen():
    with pytest.raises(ValueError):
        catalog([topic(f"topic-{index}") for index in range(MAX_TOPICS + 1)])
    value = catalog()
    with pytest.raises(ValidationError, match="frozen"):
        value.status = "approved"
    with pytest.raises(ValidationError, match="frozen"):
        value.topics[0].name = "改名"


@pytest.mark.parametrize("text", ["", "- not-a-catalog", "[invalid", "!!python/object:object {}"])
def test_loader_rejects_invalid_or_unsafe_yaml(tmp_path, text):
    path = tmp_path / "topics.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_topic_catalog(path)


def test_loader_rejects_duplicate_yaml_keys(tmp_path):
    path = tmp_path / "topics.yml"
    path.write_text((DEPLOY / "audience-topics.local.yml").read_text(encoding="utf-8") + "\nstatus: approved\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_topic_catalog(path)


def test_loader_rejects_missing_oversized_and_invalid_utf8(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        load_topic_catalog(tmp_path / "missing.yml")
    path = tmp_path / "topics.yml"
    path.write_bytes(b" " * (MAX_CATALOG_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        load_topic_catalog(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="safely"):
        load_topic_catalog(path)


def test_yaml_roundtrip_retains_approval_and_provenance(tmp_path):
    value = catalog(status="approved")
    path = tmp_path / "topics.yml"
    path.write_text(yaml.safe_dump(value.model_dump(mode="json")), encoding="utf-8")
    assert load_topic_catalog(path) == value
