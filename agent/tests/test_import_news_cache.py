"""Offline boundary and receipt checks for incremental knowledge imports."""

import importlib.util
from hashlib import sha256
import json
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

import pytest

SPEC = importlib.util.spec_from_file_location("import_news_cache", Path(__file__).parents[1] / "tools/import_news_cache.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TENANT = "11111111-1111-4111-8111-111111111111"


def cache(tmp_path, count=1):
    rows = []
    for index in range(count):
        news_id = f"20261014A{index:06d}00"
        path = tmp_path / "articles" / f"{news_id}.json"
        path.parent.mkdir(exist_ok=True)
        article = {"news_id": news_id, "url": f"https://news.qq.com/rain/a/{news_id}",
                   "content_version": 1, "title": "真实标题", "content": "完整新闻正文",
                   "publish_time": "2026-10-14 12:00:00"}
        path.write_text(json.dumps(article, ensure_ascii=False))
        rows.append({"news_id": news_id, "relative_path": f"articles/{news_id}.json",
                     "sha256": sha256(path.read_bytes()).hexdigest(),
                     "publish_time": "2026-10-14T12:00:00+08:00", "category": "科技"})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "timezone": "Asia/Shanghai",
                                   "start_date": "2026-10-07", "end_date": "2026-10-14", "articles": rows}))
    return manifest


def test_converts_original_body_and_binds_file_and_manifest_hashes(tmp_path):
    path = cache(tmp_path)
    source, docs = MODULE.load_documents(path)
    assert docs[0]["text"] == "完整新闻正文"
    assert docs[0]["publish_time"] == "2026-10-14T12:00:00+08:00"
    assert docs[0]["metadata"]["ingest_manifest_sha256"] == source["manifest_sha256"]
    assert docs[0]["metadata"]["source_file_sha256"] == source["documents"][0]["source_sha256"]


def test_audited_manifest_larger_than_eight_mib_is_supported(tmp_path):
    path = cache(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["collection_audit"] = "x" * (8 * 1024 * 1024)
    path.write_text(json.dumps(manifest))
    source, documents = MODULE.load_documents(path)
    assert len(documents) == 1
    assert source["manifest_sha256"] == sha256(path.read_bytes()).hexdigest()


def test_manifest_size_is_checked_before_reading(tmp_path, monkeypatch):
    path = cache(tmp_path)
    monkeypatch.setattr(MODULE, "MAX_MANIFEST_BYTES", 8)
    with patch.object(Path, "read_bytes", side_effect=AssertionError("oversized input must not be read")):
        with pytest.raises(ValueError, match="size budget"):
            MODULE.load_documents(path)


def test_tampered_input_is_rejected_before_any_write(tmp_path):
    path = cache(tmp_path)
    article = next((tmp_path / "articles").glob("*.json"))
    article.write_text(article.read_text() + " ")
    calls = []
    with pytest.raises(ValueError, match="hash"):
        MODULE.import_cache(manifest_path=path, base_url="http://127.0.0.1:28070", tenant_id=TENANT,
                            token="private-test-token", report_path=tmp_path / "report.json",
                            post=lambda *args: calls.append(args))
    assert calls == []
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("change", ["path", "duplicate", "date", "source", "version"])
def test_invalid_manifest_or_article_is_rejected(tmp_path, change):
    path = cache(tmp_path)
    manifest = json.loads(path.read_text())
    row = manifest["articles"][0]
    if change == "path":
        row["relative_path"] = "../article.json"
    elif change == "duplicate":
        manifest["articles"].append(row.copy())
    elif change == "date":
        manifest["end_date"] = "2026-10-13"
    else:
        article_path = tmp_path / row["relative_path"]
        article = json.loads(article_path.read_text())
        article["url" if change == "source" else "content_version"] = "https://evil.test/secret" if change == "source" else True
        article_path.write_text(json.dumps(article))
        row["sha256"] = sha256(article_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        MODULE.load_documents(path)


@pytest.mark.parametrize("url", ["https://enterprise.test", "http://127.0.0.1.evil.test",
                                 "http://user:pass@localhost:8000", "http://127.0.0.1:8000/path"])
def test_only_loopback_api_roots_are_accepted(url):
    with pytest.raises(ValueError):
        MODULE.validate_local_url(url)


def test_import_batches_and_rerun_receipt_preserve_api_statuses_without_token(tmp_path):
    manifest = cache(tmp_path, 21)
    batches = []
    def post(url, tenant, token, docs):
        batches.append(len(docs))
        return [{"document_id": doc["document_id"], "status": "skipped"} for doc in docs]
    report = MODULE.import_cache(manifest_path=manifest, base_url="http://127.0.0.1:28070", tenant_id=TENANT,
                                 token="private-test-token", report_path=tmp_path / "report.json", post=post)
    assert batches == [20, 1]
    assert report["status"] == "completed"
    assert report["counts"] == {"skipped": 21}
    assert "private-test-token" not in (tmp_path / "report.json").read_text()


def test_failed_batch_stops_and_reports_unknown_outcome_without_token(tmp_path):
    manifest = cache(tmp_path, 21)
    def post(*args):
        raise HTTPError("http://localhost", 503, "do not persist private-test-token", {}, None)
    report = MODULE.import_cache(manifest_path=manifest, base_url="http://localhost:8000", tenant_id=TENANT,
                                 token="private-test-token", report_path=tmp_path / "report.json", post=post)
    assert report["status"] == "partial_failed"
    assert report["errors"] == [{"batch_offset": 0, "kind": "http_error", "status_code": 503}]
    assert "private-test-token" not in (tmp_path / "report.json").read_text()


@pytest.mark.parametrize("payload", [[], None, {"outcomes": [{"document_id": [], "status": "created"}]},
                                     {"outcomes": [{"document_id": "news-20261014A00000000", "status": []}]}])
def test_malformed_api_envelopes_become_audited_failures(tmp_path, payload):
    manifest = cache(tmp_path)
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload).encode()
    with patch.object(MODULE, "build_opener") as opener:
        opener.return_value.open.return_value = response
        report = MODULE.import_cache(manifest_path=manifest, base_url="http://localhost:8000", tenant_id=TENANT,
                                     token="private-test-token", report_path=tmp_path / "report.json")
    assert report["status"] == "partial_failed"
    assert report["errors"] == [{"batch_offset": 0, "kind": "ValueError"}]
