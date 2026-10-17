"""Real-content provenance and fail-closed boundaries for the offline v8 roster."""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from simulation.warehouse_simulation import real_news_catalog as catalog


@pytest.fixture(scope="module")
def payload():
    return json.loads(catalog.CATALOG_PATH.read_bytes())


def test_frozen_package_retains_exact_real_content_and_original_identity():
    rows = catalog.load_catalog()
    assert len(rows) == len({row["news_id"] for row in rows}) == 661
    assert Counter(row["category"] for row in rows) == catalog.CATEGORY_COUNTS
    assert {row["content_type"] for row in rows} == {"article"}
    assert all(row["news_content_synthetic"] is False for row in rows)
    assert all(row["category_data_synthetic"] is True for row in rows)
    for row in rows:
        original_path = catalog.REPOSITORY_ROOT / row["source_file"]
        raw = original_path.read_bytes()
        original = json.loads(raw)
        assert sha256(raw).hexdigest() == row["source_sha256"]
        assert all(original[key] == row[key] for key in ("news_id", "title", "content", "url", "content_version", "fetched_at"))
        assert row["publish_time_raw"] == original.get("publish_time_original", original["publish_time"])
        assert sha256(row["content"].encode()).hexdigest() == row["content_sha256"]


def test_rebuild_is_deterministic_and_matches_pinned_catalog_identity():
    rebuilt = catalog.build_catalog()
    raw = (json.dumps(rebuilt, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    assert sha256(raw).hexdigest() == catalog.CATALOG_SHA256
    reference = next(row for row in rebuilt["articles"] if row["news_id"] == catalog.REFERENCE_NEWS_ID)
    assert reference["content_sha256"] == catalog.REFERENCE_CONTENT_SHA256
    assert rebuilt["provenance"]["reference_article"]["http_status"] == 200


def test_exported_copy_loads_without_mutable_original_cache(tmp_path, monkeypatch):
    copied = tmp_path / "catalog.json"
    copied.write_bytes(catalog.CATALOG_PATH.read_bytes())
    monkeypatch.setattr(catalog, "REPOSITORY_ROOT", tmp_path / "missing-cache")
    rows = catalog.load_catalog(copied)
    rows[0]["title"] = "caller mutated its private copy"
    assert catalog.load_catalog(copied)[0]["title"] != rows[0]["title"]


def test_tampered_catalog_and_environment_file_are_rejected_before_use(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_bytes(catalog.CATALOG_PATH.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        catalog.load_catalog(path)
    with pytest.raises(ValueError, match="named catalog.json"):
        catalog.load_catalog(tmp_path / ".env")


@pytest.mark.parametrize("value", [b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff'])
def test_json_reader_rejects_duplicate_keys_non_finite_and_invalid_encoding(tmp_path, value):
    path = tmp_path / "fixture.json"
    path.write_bytes(value)
    with pytest.raises(ValueError):
        catalog._bounded_json(path, 1024)


def test_json_reader_enforces_size_and_symlink_boundaries(tmp_path):
    source = tmp_path / "source.json"
    source.write_text('{"content":"oversized"}', encoding="utf-8")
    with pytest.raises(ValueError, match="size budget"):
        catalog._bounded_json(source, 8)
    link = tmp_path / "linked.json"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="regular JSON"):
        catalog._bounded_json(link, 1024)


@pytest.mark.parametrize("key,value", [
    ("news_id", "../../.env"),
    ("url", "https://other.example/rain/a/20260903A05YQE00"),
    ("url", "https://news.qq.com/rain/a/20260903A05YQE00?redirect=1"),
    ("source_file", "news/data/local_recall/articles-v1/../../.env"),
    ("source_manifest_sha256", "0" * 64),
    ("content", "a fabricated article replacing the original"),
    ("publish_time_raw", "2026-09-04 12:18:15"),
    ("publish_time", "2026-09-03T12:18:15"),
    ("content_type", "video"),
    ("source_label", "invented original publisher"),
    ("news_content_synthetic", True),
    ("category_data_synthetic", False),
    ("content_version", True),
    ("source_html_sha256", "0" * 64),
])
def test_runtime_validation_rejects_content_identity_and_provenance_drift(payload, key, value):
    article = deepcopy(next(row for row in payload["articles"] if row["news_id"] == catalog.REFERENCE_NEWS_ID))
    article[key] = value
    with pytest.raises(ValueError):
        catalog._validate_article(article)


def test_runtime_validation_rejects_unrecognized_fields_and_duplicate_ids(payload):
    unknown = deepcopy(payload["articles"][0])
    unknown["user_id"] = "must-never-be-a-content-field"
    with pytest.raises(ValueError, match="fields"):
        catalog._validate_article(unknown)
    duplicated = deepcopy(payload)
    duplicated["articles"][1] = deepcopy(duplicated["articles"][0])
    with pytest.raises(ValueError, match="sorted unique"):
        catalog._validate_payload(duplicated)


@pytest.mark.parametrize("field", ["title", "content", "publish_time_raw"])
def test_builder_refuses_changed_v6_metadata_even_when_source_file_is_intact(field):
    original = json.loads((catalog.REPOSITORY_ROOT / catalog.CORPUS_MANIFEST).read_bytes())["articles"][0]
    changed = deepcopy(original)
    changed[field] = "2000-01-01 00:00:00" if field == "publish_time_raw" else "changed untrusted metadata"
    with pytest.raises(ValueError, match="differs"):
        catalog._source_article(catalog.REPOSITORY_ROOT, changed, package="corpus-v6",
                                source_file=original["source_file"], source_digest=original["source_sha256"],
                                category=original["category"])


def test_builder_refuses_source_file_tampering_before_freezing(tmp_path):
    original = json.loads((catalog.REPOSITORY_ROOT / catalog.CORPUS_MANIFEST).read_bytes())["articles"][0]
    path = tmp_path / original["source_file"]
    path.parent.mkdir(parents=True)
    path.write_text('{"news_id":"tampered"}', encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        catalog._source_article(tmp_path, original, package="corpus-v6", source_file=original["source_file"],
                                source_digest=original["source_sha256"], category=original["category"])


def test_freeze_refuses_overwrite_and_reuses_valid_frozen_sources(tmp_path):
    output = tmp_path / "new-version"
    report = catalog.freeze_catalog(output)
    assert report["article_count"] == 661
    assert report["catalog_sha256"] == catalog.CATALOG_SHA256
    assert len(catalog.load_catalog(output / "catalog.json")) == 661
    before = (output / "catalog.json").read_bytes()
    with pytest.raises(ValueError, match="refusing overwrite"):
        catalog.freeze_catalog(output)
    assert (output / "catalog.json").read_bytes() == before
