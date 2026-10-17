"""Independent full-body verification; fake binary assets prove no model/NumPy loading."""

import hashlib
import json
from pathlib import Path

import pytest

from tools.verify_bulk_news_index import CORPUS, file_sha256, verify_bulk_index


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bind_package(package):
    (package / "SHA256SUMS").write_text("".join(
        f"{file_sha256(path)}  {path.relative_to(package).as_posix()}\n"
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.name not in {"SHA256SUMS", "build_report.json"}
    ), encoding="utf-8")
    return file_sha256(package / "SHA256SUMS")


@pytest.fixture
def inputs(tmp_path):
    source, base, package = (tmp_path / name for name in ("source", "base", "package"))
    entries, chunks = [], []
    for index, (news_id, body) in enumerate((("20261007AFIRST", "完整原文的连续段落。" * 120),
                                            ("20261007ASECOND", "新闻简讯也需要完整保留。"))):
        published = "2026-10-07T09:00:00+08:00"
        article = {"news_id": news_id, "url": f"https://news.qq.com/rain/a/{news_id}", "title": f"新闻标题{index}",
                   "content": body, "content_version": 1, "publish_time": published}
        document_sha = hashlib.sha256((article["title"] + "\x1f" + body).encode()).hexdigest()
        article["document_sha256"] = document_sha
        article_path = source / "articles" / f"{news_id}.json"
        write_json(article_path, article)
        entries.append({"news_id": news_id, "url": article["url"], "relative_path": f"articles/{news_id}.json",
                        "sha256": file_sha256(article_path), "document_sha256": document_sha,
                        "publish_time": published})
        header = f"# {article['title']}\n来源地址: {article['url']}\n发布时间: {published}\n\n"
        parts = []
        for chunk_index, offset in enumerate(range(0, len(body), 720)):
            parts.append({"news_id": news_id, "url": article["url"], "publish_time": published,
                          "content_version": 1, "document_sha256": document_sha, "chunk_index": chunk_index,
                          "text": header + body[offset:offset + 800]})
            if offset + 800 >= len(body):
                break
        chunks.append(parts)
    manifest = source / "manifest.json"
    write_json(manifest, {"schema_version": 1, "timezone": "Asia/Shanghai", "start_date": "2026-10-07",
                          "end_date": "2026-10-07", "articles": entries, "counts_by_date": {"2026-10-07": 2}})
    # The first source article already belongs to the frozen base. Verification
    # must still check all its slices, not only the newly appended article.
    for destination, rows in ((base, chunks[0]), (package, chunks[0] + chunks[1])):
        corpus = destination / CORPUS
        corpus.mkdir(parents=True)
        (corpus / "chunks.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        (corpus / "embeddings.npy").write_bytes(b"deliberately-not-a-numpy-file")
        (corpus / "index.bin").write_bytes(b"deliberately-not-an-hnsw-file")
        model = destination / "data/models/bge-large-zh-v1.5"
        model.mkdir(parents=True)
        (model / "config.json").write_bytes(b"{}")
        (model / "pytorch_model.bin").write_bytes(b"deliberately-not-model-weights")
    base_sha = bind_package(base)
    package_sha = bind_package(package)
    report = {"status": "completed", "snapshot_version": "test-v2", "package_manifest_sha256": package_sha,
              "base_manifest_sha256": base_sha, "input": {"manifest_sha256": file_sha256(manifest)},
              "total_articles": 2, "total_chunks": len(chunks[0]) + len(chunks[1])}
    write_json(package / "build_report.json", report)
    return {"manifest_path": manifest, "package": package, "base_package": base,
            "expected_manifest_sha256": file_sha256(manifest), "expected_base_manifest_sha256": base_sha,
            "expected_articles": 2, "receipt_path": tmp_path / "receipt.json"}


def rebind_changed_corpus(inputs, change):
    path = inputs["package"] / CORPUS / "chunks.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    change(rows)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    package_sha = bind_package(inputs["package"])
    report_path = inputs["package"] / "build_report.json"
    report = json.loads(report_path.read_text())
    report.update(package_manifest_sha256=package_sha, total_chunks=len(rows))
    write_json(report_path, report)


def test_verifies_every_original_slice_including_existing_base_news_without_binary_loaders(inputs):
    before = file_sha256(inputs["base_package"] / "SHA256SUMS")
    result = verify_bulk_index(**inputs)
    assert result["status"] == "completed" and all(result["checks"].values())
    assert result["counts"]["verified_source_articles"] == 2
    assert result["counts"]["verified_source_chunks"] == result["counts"]["expected_source_chunks"] == 3
    assert result["counts_by_date"] == {"2026-10-07": 2}
    assert file_sha256(inputs["base_package"] / "SHA256SUMS") == before
    assert json.loads(inputs["receipt_path"].read_text()) == result
    assert verify_bulk_index(**inputs)["verified_document_roster_sha256"] == result["verified_document_roster_sha256"]


@pytest.mark.parametrize("field,value", [("document_sha256", "0" * 64), ("url", "https://news.qq.com/rain/a/OTHER"),
                                       ("publish_time", "2026-10-08T09:00:00+08:00"), ("content_version", 2),
                                       ("text", "# 错误标题\n仅有摘要，不是全文。")])
def test_rehashed_package_cannot_hide_wrong_article_metadata_or_body(inputs, field, value):
    rebind_changed_corpus(inputs, lambda rows: rows[-1].update({field: value}))
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed"
    assert "BGE" in result["failure"]["message"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra_index"])
def test_full_body_chunk_coverage_is_required_exactly_once(inputs, change):
    def alter(rows):
        if change == "missing":
            rows.pop()
        elif change == "duplicate":
            rows.append(dict(rows[-1]))
        else:
            rows[-1]["chunk_index"] = 99
    rebind_changed_corpus(inputs, alter)
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed"
    assert any(word in result["failure"]["message"] for word in ("missing", "duplicate", "extra"))


def test_base_manifest_mutation_and_source_file_mutation_write_failure_receipts(inputs):
    original = (inputs["base_package"] / "SHA256SUMS").read_bytes()
    (inputs["base_package"] / "SHA256SUMS").write_bytes(original + b"\n")
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed" and "pinned" in result["failure"]["message"]
    (inputs["base_package"] / "SHA256SUMS").write_bytes(original)
    article = next((inputs["manifest_path"].parent / "articles").glob("*.json"))
    article.write_bytes(article.read_bytes() + b" ")
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed" and "source file SHA-256" in result["failure"]["message"]


def test_rehashed_package_must_preserve_frozen_base_jsonl_bytes(inputs):
    rebind_changed_corpus(inputs, lambda rows: rows[0].update(text=rows[0]["text"] + " "))
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed" and "base byte prefix" in result["failure"]["message"]


def test_rehashed_report_cannot_authorize_an_extra_news_id(inputs):
    def add_unknown_article(rows):
        rows.append({**rows[-1], "news_id": "20261007AUNAUTHORIZED"})
    rebind_changed_corpus(inputs, add_unknown_article)
    report_path = inputs["package"] / "build_report.json"
    report = json.loads(report_path.read_text())
    report["total_articles"] += 1
    write_json(report_path, report)
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed" and "unauthorized news_id" in result["failure"]["message"]


def test_rehashed_report_cannot_add_an_extra_slice_to_a_base_only_article(inputs):
    # Keep the first article in the frozen base, but remove it from this input
    # manifest so its appended slices are governed by the exact total row rule.
    manifest_path = inputs["manifest_path"]
    manifest = json.loads(manifest_path.read_text())
    removed = manifest["articles"].pop(0)
    (manifest_path.parent / removed["relative_path"]).unlink()
    manifest["counts_by_date"]["2026-10-07"] = 1
    write_json(manifest_path, manifest)
    inputs.update(expected_articles=1, expected_manifest_sha256=file_sha256(manifest_path))
    report_path = inputs["package"] / "build_report.json"
    report = json.loads(report_path.read_text())
    report["input"]["manifest_sha256"] = inputs["expected_manifest_sha256"]
    write_json(report_path, report)
    rebind_changed_corpus(inputs, lambda rows: rows.append({**rows[0], "chunk_index": 999}))
    result = verify_bulk_index(**inputs)
    assert result["status"] == "failed" and "base rows plus" in result["failure"]["message"]


def test_receipt_cannot_overwrite_immutable_inputs(inputs):
    inputs["receipt_path"] = inputs["package"] / "receipt.json"
    with pytest.raises(ValueError, match="outside"):
        verify_bulk_index(**inputs)
