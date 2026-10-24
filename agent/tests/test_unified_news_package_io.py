"""Immutable release files round-trip and reject damaged or ambiguous input."""

from hashlib import sha256
import json
import pytest

from app.modules.news_storage.model import build_package
from app.modules.news_storage import package_io


def small_package():
    rows = []
    for index in (1, 2):
        news_id = f"20261023A0000{index}00"
        rows.append({
            "news_id": news_id,
            "title": f"第 {index} 篇测试新闻",
            "body": "测试保存的正文。" * 20,
            "url": f"https://news.qq.com/rain/a/{news_id}",
            "publish_time": "2026-10-23T10:00:00+08:00",
            "content_type": "article", "content_state": "body_available",
            "fragments": [], "quality_flags": ["cached_body_completeness_unreviewed"],
            "source": {"package": "file-test-fixture", "path": f"fixtures/{news_id}.json",
                       "file_sha256": "a" * 64},
            "priority": 10,
        })
    return build_package(rows, tenant_id="tenant-files-test")


@pytest.fixture
def saved(tmp_path):
    directory = tmp_path / "release"
    package_io.save_package(small_package(), directory, inventory={"fixture": True})
    return directory


def read_manifest(directory):
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def write_manifest(directory, manifest):
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def replace_file_with_matching_file_checksum(directory, filename, content):
    """Keep the file envelope valid so JSON/model validation is exercised."""
    (directory / filename).write_bytes(content)
    manifest = read_manifest(directory)
    manifest["files"][filename].update(sha256=sha256(content).hexdigest(), bytes=len(content))
    write_manifest(directory, manifest)


def test_file_roundtrip_preserves_records_and_bound_sources(tmp_path):
    package = small_package()
    directory = tmp_path / "new-parent" / "release"
    inventory = {"fixture": True, "说明": "仅为测试来源清单"}
    wrapper = package_io.save_package(package, directory, inventory=inventory)
    assert package_io.load_package(directory) == package
    assert read_manifest(directory) == wrapper
    assert wrapper["source_inventory"] == inventory
    assert {path.name for path in directory.iterdir()} == {
        "manifest.json", "versions.jsonl", "provenance.jsonl", "release_items.jsonl"}
    for name, expected in wrapper["files"].items():
        raw = (directory / name).read_bytes()
        assert raw.endswith(b"\n")
        assert expected["bytes"] == len(raw)
        assert expected["rows"] == len(raw.splitlines())
        assert expected["sha256"] == sha256(raw).hexdigest()


@pytest.mark.parametrize("kind", ["empty-directory", "occupied-directory", "file"])
def test_save_refuses_any_existing_output_and_preserves_it(tmp_path, kind):
    output = tmp_path / "existing"
    if kind == "file":
        output.write_text("do not replace", encoding="utf-8")
    else:
        output.mkdir()
        if kind == "occupied-directory":
            (output / "marker").write_text("do not replace", encoding="utf-8")
    with pytest.raises(FileExistsError):
        package_io.save_package(small_package(), output)
    if kind == "file":
        assert output.read_text(encoding="utf-8") == "do not replace"
    else:
        assert set(path.name for path in output.iterdir()) == (
            {"marker"} if kind == "occupied-directory" else set())
    assert not list(tmp_path.glob(".existing.building-*"))


def test_save_does_not_replace_output_created_while_serializing(tmp_path, monkeypatch):
    output = tmp_path / "concurrent-release"
    original_load = package_io.load_package

    def another_writer_claims_output(directory):
        package = original_load(directory)
        output.mkdir()
        return package

    monkeypatch.setattr(package_io, "load_package", another_writer_claims_output)
    with pytest.raises(FileExistsError):
        package_io.save_package(small_package(), output)
    assert output.is_dir()
    assert list(output.iterdir()) == []
    assert not list(tmp_path.glob(".concurrent-release.building-*"))


def test_failed_serialization_leaves_no_discoverable_or_staging_release(tmp_path):
    output = tmp_path / "invalid-inventory"
    with pytest.raises(ValueError):
        package_io.save_package(small_package(), output, inventory={"invalid": float("nan")})
    assert not output.exists()
    assert not list(tmp_path.glob(".invalid-inventory.building-*"))


@pytest.mark.parametrize("filename", package_io.FILES)
def test_same_size_byte_changes_cannot_pass_saved_file_checksum(saved, filename):
    path = saved / filename
    original = path.read_bytes()
    changed = original.replace(b"20261023", b"20261024", 1)
    assert changed != original and len(changed) == len(original)
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="checksum/count mismatch"):
        package_io.load_package(saved)


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("rows", 99), ("bytes", 1)])
def test_file_envelope_checksum_row_and_size_tampering_is_rejected(saved, field, value):
    manifest = read_manifest(saved)
    manifest["files"]["versions.jsonl"][field] = value
    write_manifest(saved, manifest)
    with pytest.raises(ValueError, match="mismatch"):
        package_io.load_package(saved)


def test_duplicate_manifest_json_key_is_rejected(saved):
    path = saved / "manifest.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"source_inventory":', '"source_inventory":null,"source_inventory":', 1),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        package_io.load_package(saved)


def test_duplicate_record_json_key_is_rejected_even_with_matching_file_checksum(saved):
    raw = (saved / "versions.jsonl").read_bytes()
    changed = raw.replace(b'"news_id":', b'"news_id":"duplicate","news_id":', 1)
    replace_file_with_matching_file_checksum(saved, "versions.jsonl", changed)
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        package_io.load_package(saved)


def test_nonfinite_json_constant_is_rejected_even_with_matching_file_checksum(saved):
    raw = (saved / "versions.jsonl").read_bytes()
    changed = raw.replace(b'"title":', b'"unexpected":NaN,"title":', 1)
    replace_file_with_matching_file_checksum(saved, "versions.jsonl", changed)
    with pytest.raises(ValueError, match="invalid JSON constant"):
        package_io.load_package(saved)


@pytest.mark.parametrize("filename", ["manifest.json", *package_io.FILES])
def test_symlinked_release_files_are_rejected_even_with_valid_target(saved, tmp_path, filename):
    path = saved / filename
    external = tmp_path / f"external-{filename}"
    path.rename(external)
    path.symlink_to(external)
    with pytest.raises(ValueError, match="manifest file|path mismatch"):
        package_io.load_package(saved)


def test_manifest_cannot_redirect_loader_to_an_additional_file(saved):
    manifest = read_manifest(saved)
    manifest["files"]["../outside.jsonl"] = {"sha256": "a" * 64, "bytes": 1, "rows": 1}
    write_manifest(saved, manifest)
    with pytest.raises(ValueError, match="invalid release files"):
        package_io.load_package(saved)


def test_matching_file_checksums_do_not_bypass_record_hash_validation(saved):
    raw = (saved / "versions.jsonl").read_bytes()
    changed = raw.replace("测试保存的正文".encode(), "已篡改的正文".encode(), 1)
    replace_file_with_matching_file_checksum(saved, "versions.jsonl", changed)
    with pytest.raises(ValueError, match="record/hash mismatch"):
        package_io.load_package(saved)


def test_release_counts_are_validated_after_file_checksums(saved):
    manifest = read_manifest(saved)
    manifest["release"]["counts"]["news_count"] += 1
    write_manifest(saved, manifest)
    with pytest.raises(ValueError, match="counts mismatch"):
        package_io.load_package(saved)
