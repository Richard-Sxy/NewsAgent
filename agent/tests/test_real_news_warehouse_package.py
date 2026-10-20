"""Independent package verification cannot be bypassed by rewritten hashes."""

from hashlib import sha256
import json
import shutil

import pytest

from simulation.warehouse_simulation.real_news import RealNewsSimulationConfig, export_dataset
from tools.validate_real_news_warehouse import validate_package


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rewrite_manifest(directory, edit):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_bytes())
    edit(manifest)
    manifest.pop("dataset_sha256")
    manifest["dataset_sha256"] = sha256(canonical(manifest).encode()).hexdigest()
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    path = tmp_path_factory.mktemp("v8-package") / "export"
    export_dataset(RealNewsSimulationConfig(closed_hours=1), path)
    return path


def private_copy(package, tmp_path):
    path = tmp_path / "copy"
    shutil.copytree(package, path)
    return path


def test_valid_full_real_catalog_package_matches_streamed_counts(package):
    from simulation.warehouse_simulation import real_news_catalog as catalog
    if not catalog.EXTENSION_PATH.exists():
        pytest.skip("run agent/tools/freeze_real_news_warehouse_extension.py first")
    result = validate_package(package)
    assert result["status"] == "passed"
    assert result["distinct_real_articles"] == 8120
    assert result["extension_count"] == 7459
    assert result["metric_article_count"] == 661
    assert result["tables"]["dim_news"]["rows"] == 16240
    assert result["tables"]["news_content_feature"]["rows"] == 16240
    assert result["total_persisted_rows"] > 10_000
    assert result["news_content_synthetic"] is False
    assert result["metric_data_synthetic"] is True


def test_manifest_rewrite_without_new_digest_is_rejected(package, tmp_path):
    path = private_copy(package, tmp_path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["news_content_synthetic"] = True
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest dataset digest"):
        validate_package(path)


def test_rewritten_hashes_cannot_authorize_a_fabricated_news_catalog(package, tmp_path):
    path = private_copy(package, tmp_path)
    catalog_path = path / "catalog.json"
    content = json.loads(catalog_path.read_bytes())
    content["articles"][0]["title"] = "fabricated replacement title"
    catalog_path.write_text(json.dumps(content, ensure_ascii=False))
    rewrite_manifest(path, lambda value: value.update(catalog_sha256=sha256(catalog_path.read_bytes()).hexdigest()))
    with pytest.raises(ValueError, match="independently frozen"):
        validate_package(path)


def test_metric_row_corruption_is_rejected(package, tmp_path):
    path = private_copy(package, tmp_path)
    with (path / "news_metric_hourly.jsonl").open("ab") as stream:
        stream.write(b'{}\n')
    with pytest.raises(ValueError, match="columns|tenant|count or digest"):
        validate_package(path)


def test_raw_identity_is_rejected_even_when_row_hash_and_columns_are_rewritten(package, tmp_path):
    path = private_copy(package, tmp_path)
    table = path / "news_metric_hourly.jsonl"
    lines = table.read_text().splitlines()
    row = json.loads(lines[0])
    row["user_id"] = "raw-event-identity"
    lines[0] = canonical(row)
    table.write_text("\n".join(lines) + "\n")
    def edit(manifest):
        manifest["tables"]["news_metric_hourly"]["sha256"] = sha256(table.read_bytes()).hexdigest()
        manifest["tables"]["news_metric_hourly"]["columns"].append("user_id")
    rewrite_manifest(path, edit)
    with pytest.raises(ValueError, match="identities must not be persisted"):
        validate_package(path)


def test_source_code_corruption_and_symlinked_table_are_rejected(package, tmp_path):
    path = private_copy(package, tmp_path)
    source = path / "generator.py"
    source.write_bytes(source.read_bytes() + b"\n# altered source\n")
    with pytest.raises(ValueError, match="artifact digest mismatch: generator.py"):
        validate_package(path)
    source.write_bytes((package / "generator.py").read_bytes())
    table = path / "news_metric_hourly.jsonl"
    table.unlink()
    table.symlink_to(package / "news_metric_hourly.jsonl")
    with pytest.raises(ValueError, match="invalid table artifact"):
        validate_package(path)
