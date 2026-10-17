#!/usr/bin/env python3
"""Independently verify a completed public-news BGE backfill using only stdlib.

The source manifest and frozen base SHA256SUMS must be explicitly pinned. Model
weights, NumPy arrays and HNSW files are only streamed through SHA-256; none are
loaded into an inference runtime or deserialized. Every expected full-body
800/80 chunk is compared with the published JSONL, including existing news IDs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
CORPUS = Path("data/experiments/offline_recall_fg")
CHUNK_CHARS, CHUNK_OVERLAP = 800, 80
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 4 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
NEWS_ID = re.compile(r"\d{8}[A-Za-z0-9_-]{1,152}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, *, limit: int = MAX_JSON_BYTES) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"missing, linked or oversized JSON input: {path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("publication time must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("publication time must include a timezone")
    return result.astimezone(TZ)


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _verify_package(package: Path, expected: str | None = None) -> tuple[str, dict[Path, str]]:
    manifest = package / "SHA256SUMS"
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
        raise ValueError("package SHA256SUMS is missing, linked or oversized")
    raw = manifest.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("package SHA256SUMS differs from the pinned SHA-256")
    entries = {}
    for line in raw.decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if match is None:
            raise ValueError("invalid package SHA256SUMS line")
        relative = Path(match[2])
        path = package / relative
        if (relative.is_absolute() or ".." in relative.parts or relative in entries
                or path.is_symlink() or not path.resolve().is_relative_to(package)
                or not path.is_file()):
            raise ValueError("unsafe, duplicate or missing package asset")
        entries[relative] = match[1].lower()
        if file_sha256(path) != entries[relative]:
            raise ValueError(f"package asset SHA-256 mismatch: {relative}")
    required = {CORPUS / name for name in ("chunks.jsonl", "embeddings.npy", "index.bin")}
    model_dir = package / "data/models/bge-large-zh-v1.5"
    if not model_dir.is_dir():
        raise ValueError("BGE model asset directory is missing")
    required.update(path.relative_to(package) for path in model_dir.rglob("*") if path.is_file())
    if not required <= entries.keys():
        raise ValueError("package SHA256SUMS omits required corpus or model assets")
    return digest, entries


def _source_documents(manifest_path: Path, expected_sha: str, expected_articles: int):
    manifest = _json(manifest_path)
    if file_sha256(manifest_path) != expected_sha:
        raise ValueError("source manifest differs from the pinned SHA-256")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or manifest.get("timezone") != "Asia/Shanghai"):
        raise ValueError("unsupported source manifest schema or timezone")
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    if start > end:
        raise ValueError("reversed source publication window")
    entries = manifest.get("articles")
    if not isinstance(entries, list) or len(entries) != expected_articles:
        raise ValueError("source manifest article count differs from the expected count")
    root, expected, source_files, counts = manifest_path.parent, {}, set(), Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("source manifest article must be an object")
        news_id = entry.get("news_id")
        if not isinstance(news_id, str) or NEWS_ID.fullmatch(news_id) is None or news_id in expected:
            raise ValueError("invalid or duplicate source news_id")
        relative = Path(entry.get("relative_path", ""))
        path = root / relative
        if (relative != Path("articles") / f"{news_id}.json" or path.is_symlink()
                or path.resolve().parent != (root / "articles").resolve()):
            raise ValueError(f"source article path differs from its identity: {news_id}")
        article = _json(path, limit=MAX_ARTICLE_BYTES)
        if not _valid_sha(entry.get("sha256")) or file_sha256(path) != entry["sha256"]:
            raise ValueError(f"source file SHA-256 mismatch: {news_id}")
        title, body = article.get("title"), article.get("content")
        if (not isinstance(title, str) or not title.strip() or len(title) > 500
                or "\n" in title or "\r" in title or not isinstance(body, str) or not body.strip()):
            raise ValueError(f"invalid source title or full body: {news_id}")
        url = f"https://news.qq.com/rain/a/{news_id}"
        version = article.get("content_version")
        if (article.get("news_id") != news_id or article.get("url") != url
                or entry.get("url") != url or type(version) is not int or version < 1):
            raise ValueError(f"source identity, URL or version mismatch: {news_id}")
        published = _timestamp(article.get("publish_time"))
        if not start <= published.date() <= end or published != _timestamp(entry.get("publish_time")):
            raise ValueError(f"source actual publication time mismatch: {news_id}")
        document_sha = hashlib.sha256(f"{title}\x1f{body}".encode("utf-8")).hexdigest()
        if (entry.get("document_sha256") != document_sha
                or article.get("document_sha256", document_sha) != document_sha):
            raise ValueError(f"source document SHA-256 mismatch: {news_id}")
        header = f"# {title.strip()}\n来源地址: {url}\n发布时间: {published.isoformat()}\n\n"
        cleaned, chunk_hashes, offset = body.strip(), [], 0
        while offset < len(cleaned):
            stop = min(offset + CHUNK_CHARS, len(cleaned))
            chunk_hashes.append(hashlib.sha256((header + cleaned[offset:stop]).encode("utf-8")).hexdigest())
            if stop == len(cleaned):
                break
            offset = stop - CHUNK_OVERLAP
        expected[news_id] = {"title": title.strip(), "url": url, "published": published,
                             "content_version": version, "document_sha256": document_sha,
                             "source_sha256": entry["sha256"], "chunk_hashes": chunk_hashes,
                             "seen_indices": set()}
        source_files.add(path.resolve())
        counts[published.date().isoformat()] += 1
    if source_files != {path.resolve() for path in (root / "articles").glob("*.json")}:
        raise ValueError("source manifest does not exactly cover the article files")
    if manifest.get("counts_by_date") != dict(sorted(counts.items())):
        raise ValueError("source manifest daily counts differ from actual publication dates")
    return expected, dict(sorted(counts.items()))


def _verify_prefix(base: Path, output: Path) -> None:
    with base.open("rb") as source, output.open("rb") as destination:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            if destination.read(len(block)) != block:
                raise ValueError("published JSONL does not preserve the frozen base byte prefix")


def _base_inventory(path: Path) -> tuple[int, set[str]]:
    rows, news_ids = 0, set()
    with path.open("rb") as source:
        while raw := source.readline(MAX_JSONL_LINE_BYTES + 1):
            if len(raw) > MAX_JSONL_LINE_BYTES:
                raise ValueError("base BGE JSONL line exceeds its size budget")
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get("news_id"), str) or not row["news_id"]:
                raise ValueError("invalid base BGE JSONL row identity")
            rows += 1
            news_ids.add(row["news_id"])
    return rows, news_ids


def _verify_chunks(path: Path, expected: dict, *, allowed_ids: set[str] | None = None,
                   expected_rows: int | None = None) -> tuple[int, int, int]:
    total, matched, all_ids = 0, 0, set()
    with path.open("rb") as source:
        while raw := source.readline(MAX_JSONL_LINE_BYTES + 1):
            if len(raw) > MAX_JSONL_LINE_BYTES:
                raise ValueError("BGE JSONL line exceeds its size budget")
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get("news_id"), str):
                raise ValueError("invalid BGE JSONL row identity")
            total += 1
            news_id = row["news_id"]
            if allowed_ids is not None and news_id not in allowed_ids:
                raise ValueError(f"BGE contains an unauthorized news_id outside base and source: {news_id}")
            all_ids.add(news_id)
            if news_id not in expected:
                continue
            wanted, index, text = expected[news_id], row.get("chunk_index"), row.get("text")
            if (type(index) is not int or not 0 <= index < len(wanted["chunk_hashes"])
                    or index in wanted["seen_indices"]):
                raise ValueError(f"duplicate, extra or invalid BGE chunk index: {news_id}")
            if (row.get("document_sha256") != wanted["document_sha256"]
                    or type(row.get("content_version")) is not int
                    or row["content_version"] != wanted["content_version"]
                    or row.get("url") != wanted["url"]
                    or _timestamp(row.get("publish_time")) != wanted["published"]):
                raise ValueError(f"BGE document SHA, URL, actual publication time or version differs: {news_id}")
            if (not isinstance(text, str) or text.split("\n", 1)[0] != "# " + wanted["title"]
                    or hashlib.sha256(text.encode("utf-8")).hexdigest() != wanted["chunk_hashes"][index]):
                raise ValueError(f"BGE title or complete original body slice differs: {news_id}:{index}")
            wanted["seen_indices"].add(index)
            matched += 1
    missing = [news_id for news_id, row in expected.items() if len(row["seen_indices"]) != len(row["chunk_hashes"])]
    if missing:
        raise ValueError(f"BGE missing documents or full-body slices: count={len(missing)}, first={missing[0]}")
    if allowed_ids is not None and all_ids != allowed_ids:
        raise ValueError("BGE news_id set differs from the exact base and source union")
    if expected_rows is not None and total != expected_rows:
        raise ValueError("BGE row count differs from base rows plus newly introduced source slices")
    return total, len(all_ids), matched


def _write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name + ".", delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def verify_bulk_index(*, manifest_path: Path, package: Path, base_package: Path,
                      expected_manifest_sha256: str, expected_base_manifest_sha256: str,
                      receipt_path: Path, expected_articles: int = 8000,
                      expected_package_manifest_sha256: str | None = None) -> dict:
    manifest_path, package, base_package = manifest_path.resolve(), package.resolve(), base_package.resolve()
    receipt_path = receipt_path.resolve()
    if any(receipt_path.is_relative_to(root) for root in (package, base_package, manifest_path.parent)):
        raise ValueError("receipt must be outside the immutable source and index directories")
    receipt = {"schema_version": 1, "status": "processing", "generated_at": datetime.now(timezone.utc).isoformat(),
               "manifest_path": str(manifest_path), "package": str(package), "base_package": str(base_package),
               "counts": {}, "checks": {}}
    try:
        if (type(expected_articles) is not int or expected_articles < 1
                or not _valid_sha(expected_manifest_sha256) or not _valid_sha(expected_base_manifest_sha256)
                or (expected_package_manifest_sha256 is not None and not _valid_sha(expected_package_manifest_sha256))):
            raise ValueError("expected article count or pinned SHA-256 is invalid")
        expected, counts = _source_documents(manifest_path, expected_manifest_sha256, expected_articles)
        receipt.update(manifest_sha256=expected_manifest_sha256, counts_by_date=counts)
        receipt["counts"].update(source_articles=len(expected), expected_source_chunks=sum(len(row["chunk_hashes"]) for row in expected.values()))
        base_digest, _ = _verify_package(base_package, expected_base_manifest_sha256)
        package_digest, _ = _verify_package(package, expected_package_manifest_sha256)
        report = _json(package / "build_report.json")
        if (report.get("status") != "completed" or report.get("package_manifest_sha256") != package_digest
                or report.get("base_manifest_sha256") != base_digest
                or report.get("input", {}).get("manifest_sha256") != expected_manifest_sha256):
            raise ValueError("completed build report differs from pinned inputs or published package")
        _verify_prefix(base_package / CORPUS / "chunks.jsonl", package / CORPUS / "chunks.jsonl")
        base_rows, base_ids = _base_inventory(base_package / CORPUS / "chunks.jsonl")
        allowed_ids = base_ids | expected.keys()
        expected_rows = base_rows + sum(len(row["chunk_hashes"]) for news_id, row in expected.items()
                                        if news_id not in base_ids)
        rows, articles, matched = _verify_chunks(package / CORPUS / "chunks.jsonl", expected,
                                                allowed_ids=allowed_ids, expected_rows=expected_rows)
        if report.get("total_chunks") != rows or report.get("total_articles") != articles:
            raise ValueError("published row or document count differs from the completed build report")
        roster = [{"news_id": news_id, "document_sha256": row["document_sha256"], "title": row["title"],
                   "url": row["url"], "publish_time": row["published"].isoformat(),
                   "source_sha256": row["source_sha256"], "full_body_chunks": len(row["chunk_hashes"])}
                  for news_id, row in sorted(expected.items())]
        receipt.update(status="completed", package_manifest_sha256=package_digest,
                       base_manifest_sha256=base_digest, snapshot_version=report.get("snapshot_version"),
                       verified_document_roster_sha256=hashlib.sha256(json.dumps(roster, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest())
        receipt["counts"].update(verified_source_articles=len(expected), verified_source_chunks=matched,
                                 base_package_articles=len(base_ids), base_package_chunks=base_rows,
                                 expected_package_articles=len(allowed_ids), expected_package_chunks=expected_rows,
                                 total_package_articles=articles, total_package_chunks=rows)
        receipt["checks"] = {"source_file_and_document_hashes": True, "all_source_news_ids": True,
                             "all_titles_urls_publication_times_and_versions": True,
                             "every_full_body_slice_exactly_once": True, "all_package_asset_hashes": True,
                             "exact_base_union_source_news_ids": True, "exact_base_plus_new_source_chunk_count": True,
                             "base_package_manifest_unchanged": True, "base_jsonl_byte_prefix_preserved": True}
        # Detect source or package mutation during the streaming verification.
        for path, digest in ((manifest_path, expected_manifest_sha256),
                             (base_package / "SHA256SUMS", base_digest), (package / "SHA256SUMS", package_digest)):
            if file_sha256(path) != digest:
                raise ValueError("input or package manifest changed during verification")
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["failure"] = {"kind": type(exc).__name__, "message": str(exc)}
    _write_receipt(receipt_path, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--base-package", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-base-manifest-sha256", required=True)
    parser.add_argument("--expected-package-manifest-sha256")
    parser.add_argument("--expected-articles", type=int, default=8000)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    receipt = verify_bulk_index(manifest_path=args.manifest, package=args.package, base_package=args.base_package,
        expected_manifest_sha256=args.expected_manifest_sha256,
        expected_base_manifest_sha256=args.expected_base_manifest_sha256,
        expected_package_manifest_sha256=args.expected_package_manifest_sha256,
        expected_articles=args.expected_articles, receipt_path=args.receipt)
    print(json.dumps({"status": receipt["status"], "counts": receipt["counts"],
                      "failure": receipt.get("failure"), "receipt": str(args.receipt)}, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
