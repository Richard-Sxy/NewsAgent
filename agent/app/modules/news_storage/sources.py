"""Read the project's frozen public news sources without network or credentials.

This boundary preserves source observations, including overlapping packages. It
does not deduplicate news IDs, manufacture missing metadata, combine historical
chunks into an article body, load behavior fixtures, or build a vector index.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .model import STATES, normalize_record


_ID = re.compile(r"^[0-9]{8}[A-Za-z0-9]{4,32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_INCREMENTAL = (
    ("incremental-20261014-v2", "var/news/data/incremental/2026-10-07_2026-10-14-v2", 10,
     "f1262e6f4a8a212b35233363eb91df58f34d9407a45b1f541736729c4bd9dac4"),
    ("incremental-20261014-v1", "var/news/data/incremental/2026-10-07_2026-10-14-v1", 30,
     "13ffd2d5a4145a4f68cd5ba25fead17340fc7d7a70e0efa02632e75ae58fdcee"),
)
_CACHE = "news/data/local_recall/articles-v1"
_CACHE_MANIFEST_SHA = "3578ba41bf131d98db10bca7de75ba7a502a10bef03430286b7840a476a77ef4"
_BGE = "var/knowledge-snapshots/rag-recall-demo"
_BGE_CHUNKS = "data/experiments/offline_recall_fg/chunks.jsonl"
_BGE_CHUNKS_SHA = "571e282d54bbb1834424153f80bb3f9f94292c99e956519b0ecc15a8225ea262"
_HEADLINES = "agent/data/public-headlines/2026-10-07/catalog.json"
_HEADLINES_SHA = "639f81bc3939b9508a248e897d586dc1b6ef6cf8aa2f7502401ccea12ad6b998"
_METADATA_REVIEW_IDS = frozenset({
    "20260819A044YC00", "20260819A0A1SP00", "20260820A05IYL00", "20260820A08IFI00",
    "20260822A09Y1N00", "20260822A09ZJO00", "20260822A0ACGY00",
})


class SourceLoadError(ValueError):
    """A named public source cannot be safely or reproducibly loaded."""


def _path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if (not relative or rel.is_absolute() or ".." in rel.parts
            or any(part.startswith(".env") for part in rel.parts)):
        raise SourceLoadError(f"Invalid public source path: {relative!r}")
    path = root.joinpath(*rel.parts)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceLoadError(f"Missing public source: {relative}") from exc
    # Internal compatibility symlinks are allowed; external source files are not.
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise SourceLoadError(f"Public source escapes repository or is not a file: {relative}")
    return resolved


def _bytes(root: Path, relative: str, *, maximum: int = 16 * 1024 * 1024,
           expected: str | None = None) -> tuple[bytes, str]:
    path = _path(root, relative)
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError as exc:
        raise SourceLoadError(f"Cannot read public source: {relative}") from exc
    if not raw or len(raw) > maximum:
        raise SourceLoadError(f"Empty or oversized public source: {relative}")
    digest = sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise SourceLoadError(f"Frozen source SHA-256 mismatch: {relative}")
    return raw, digest


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceLoadError("Duplicate JSON field in public source")
        result[key] = value
    return result


def _decode(raw: bytes | str, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              SourceLoadError("Non-finite JSON number in public source")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceLoadError(f"Invalid public-source JSON: {label}") from exc


def _json(root: Path, relative: str, **kwargs: Any) -> tuple[dict[str, Any], str]:
    raw, digest = _bytes(root, relative, **kwargs)
    obj = _decode(raw, relative)
    if not isinstance(obj, dict):
        raise SourceLoadError(f"Public source must be a JSON object: {relative}")
    return obj, digest


def _text(value: Any, label: str, maximum: int = 100_000) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\x00" in value):
        raise SourceLoadError(f"Missing or invalid {label}")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SourceLoadError("Invalid public article news_id")
    return value


def _timestamp(value: Any, label: str, *, allow_naive: bool = False) -> str:
    text = _text(value, label, 100)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceLoadError(f"Invalid timestamp: {label}") from exc
    if parsed.tzinfo is None:
        if not allow_naive:
            raise SourceLoadError(f"Timestamp lacks timezone: {label}")
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    if parsed.utcoffset() is None:
        raise SourceLoadError(f"Invalid timezone: {label}")
    return parsed.isoformat()


def _url(value: Any, news_id: str) -> str:
    url = _text(value, f"URL for {news_id}", 2048)
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "news.qq.com"
            or parsed.path != f"/rain/a/{news_id}" or parsed.query or parsed.fragment):
        raise SourceLoadError(f"Public article URL/ID mismatch: {news_id}")
    return url


def _source(package: str, path: str, digest: str, manifest_path: str | None,
            manifest_sha: str | None, *, version: Any = None,
            fetched_at: Any = None) -> dict[str, Any]:
    if version is not None and (type(version) not in (int, str) or not str(version).strip()):
        raise SourceLoadError(f"Invalid source content version: {path}")
    return {"package": package, "path": path, "file_sha256": digest,
            "manifest_path": manifest_path, "manifest_sha256": manifest_sha,
            "source_content_version": None if version is None else str(version),
            "fetched_at": None if fetched_at is None else _timestamp(fetched_at, path)}


def _body_candidate(article: dict[str, Any], source: dict[str, Any], priority: int,
                    *, allow_naive: bool = False) -> dict[str, Any]:
    news_id = _identifier(article.get("news_id"))
    if article.get("news_content_synthetic") is True:
        raise SourceLoadError(f"Synthetic article in public source: {news_id}")
    body = _text(article.get("content"), f"body for {news_id}")
    flags = ["cached_body_completeness_unreviewed"]
    if len(body) < 100:
        flags.append("body_under_100_chars")
    if len(body) < 300:
        flags.append("body_under_300_chars")
    if news_id in _METADATA_REVIEW_IDS:
        flags.append("metadata_review_required")
    raw_time = article.get("publish_time")
    published = _timestamp(raw_time, f"publish_time for {news_id}", allow_naive=allow_naive)
    if allow_naive and datetime.fromisoformat(raw_time.replace("Z", "+00:00")).tzinfo is None:
        flags.append("source_naive_time_assumed_asia_shanghai")
    return {"news_id": news_id, "title": _text(article.get("title"), f"title for {news_id}", 2000),
            "body": body, "url": _url(article.get("url"), news_id), "publish_time": published,
            "content_type": "article", "content_state": "body_available", "fragments": [],
            "quality_flags": sorted(flags), "source": source, "priority": priority}


def _incremental(root: Path, package: str, base: str, priority: int,
                 expected_manifest: str) -> list[dict[str, Any]]:
    manifest_path = f"{base}/manifest.json"
    manifest, manifest_sha = _json(root, manifest_path, expected=expected_manifest)
    rows = manifest.get("articles")
    if not isinstance(rows, list) or manifest.get("success") != len(rows) or not rows:
        raise SourceLoadError(f"Invalid incremental manifest count: {manifest_path}")
    seen: set[str] = set()
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceLoadError(f"Invalid incremental article entry: {manifest_path}")
        news_id = _identifier(row.get("news_id"))
        if news_id in seen:
            raise SourceLoadError(f"Duplicate incremental article ID: {news_id}")
        seen.add(news_id)
        relative = row.get("relative_path")
        if relative != f"articles/{news_id}.json":
            raise SourceLoadError(f"Incremental article path/ID mismatch: {news_id}")
        expected_sha = row.get("sha256")
        if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
            raise SourceLoadError(f"Missing source SHA-256: {news_id}")
        path = f"{base}/{relative}"
        article, digest = _json(root, path, maximum=2 * 1024 * 1024, expected=expected_sha)
        if article.get("news_id") != news_id or article.get("url") != row.get("url"):
            raise SourceLoadError(f"Incremental manifest/article identity mismatch: {news_id}")
        source = _source(package, path, digest, manifest_path, manifest_sha,
                         version=article.get("content_version"), fetched_at=article.get("fetched_at"))
        candidate = _body_candidate(article, source, priority)
        document_sha = sha256((candidate["title"] + "\x1f" + candidate["body"]).encode()).hexdigest()
        if (document_sha != row.get("document_sha256")
                or document_sha != article.get("document_sha256")
                or candidate["publish_time"] != _timestamp(row.get("publish_time"), manifest_path)):
            raise SourceLoadError(f"Incremental content/time contract mismatch: {news_id}")
        result.append(candidate)
    return result


def _cached_articles(root: Path) -> list[dict[str, Any]]:
    manifest_path = f"{_CACHE}/collection_manifest.json"
    manifest, manifest_sha = _json(root, manifest_path, expected=_CACHE_MANIFEST_SHA)
    ids = manifest.get("successful_news_ids")
    if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids) or manifest.get("success") != len(ids)):
        raise SourceLoadError("Invalid cached-article manifest IDs/count")
    result, originals = [], []
    for value in ids:
        news_id = _identifier(value)
        path = f"{_CACHE}/{news_id}.json"
        article, digest = _json(root, path, maximum=2 * 1024 * 1024)
        if article.get("news_id") != news_id:
            raise SourceLoadError(f"Cached article path/ID mismatch: {news_id}")
        originals.append(article)
        source = _source("public-recall-cache-v1", path, digest, manifest_path, manifest_sha,
                         version=article.get("content_version"), fetched_at=article.get("fetched_at"))
        result.append(_body_candidate(article, source, 20, allow_naive=True))
    canonical = json.dumps(originals, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if sha256(canonical.encode()).hexdigest() != manifest.get("hash"):
        raise SourceLoadError("Frozen cached article collection hash mismatch")
    return result


def _historical_fragments(root: Path) -> list[dict[str, Any]]:
    manifest_path = f"{_BGE}/SHA256SUMS"
    manifest_raw, manifest_sha = _bytes(root, manifest_path, maximum=64 * 1024)
    expected_line = f"{_BGE_CHUNKS_SHA}  ./{_BGE_CHUNKS}"
    try:
        if manifest_raw.decode("utf-8").splitlines().count(expected_line) != 1:
            raise SourceLoadError("Historical BGE chunks missing from SHA256SUMS")
    except UnicodeDecodeError as exc:
        raise SourceLoadError("Invalid historical BGE checksum manifest") from exc
    path = f"{_BGE}/{_BGE_CHUNKS}"
    raw, digest = _bytes(root, path, maximum=96 * 1024 * 1024, expected=_BGE_CHUNKS_SHA)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line_number, line in enumerate(raw.splitlines(), start=1):
        obj = _decode(line, f"{path}:{line_number}")
        if not isinstance(obj, dict):
            raise SourceLoadError(f"Invalid historical chunk at line {line_number}")
        news_id = _identifier(obj.get("news_id"))
        index = obj.get("chunk_index")
        if type(index) is not int or index < 0:
            raise SourceLoadError(f"Invalid historical chunk index: {news_id}")
        # The frozen export has no structured source metadata. Do not parse it
        # from prose or assume that repeated chunk indices describe one version.
        if obj.get("url") or obj.get("publish_time"):
            raise SourceLoadError(f"Historical chunk metadata contract changed: {news_id}")
        grouped[news_id].append({"chunk_index": index,
                                 "text": _text(obj.get("text"), f"fragment for {news_id}"),
                                 "chunk_id": f"bge-base:{line_number}:{sha256(line).hexdigest()[:16]}"})
    source = _source("historical-bge-public-fragments", path, digest, manifest_path, manifest_sha)
    result = []
    for news_id in sorted(grouped):
        fragments = grouped[news_id]
        indices = [part["chunk_index"] for part in fragments]
        flags = ["fragment_only", "source_title_missing", "source_url_missing",
                 "source_publish_time_missing", "source_content_version_missing"]
        if len(set(indices)) != len(indices):
            flags.append("duplicate_chunk_index")
        if sorted(set(indices)) != list(range(max(indices) + 1)):
            flags.append("missing_chunk_index")
        result.append({"news_id": news_id, "title": None, "body": None, "url": None,
                       "publish_time": None, "content_type": "article", "content_state": "fragments_only",
                       "fragments": fragments, "quality_flags": sorted(flags),
                       "source": dict(source), "priority": 100})
    return result


def _headlines(root: Path) -> list[dict[str, Any]]:
    catalog, digest = _json(root, _HEADLINES, expected=_HEADLINES_SHA)
    rows = catalog.get("articles")
    if catalog.get("catalog_version") != "qq-public-headlines-2026-10-07-v1" or not isinstance(rows, list):
        raise SourceLoadError("Invalid frozen public headline catalog")
    source = _source("public-headlines-20261007", _HEADLINES, digest, _HEADLINES, digest,
                     fetched_at=catalog.get("collected_at"))
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise SourceLoadError("Invalid public headline record")
        news_id = _identifier(row.get("news_id"))
        if news_id in seen or row.get("content_type") not in {"article", "video"}:
            raise SourceLoadError(f"Duplicate headline ID or invalid content type: {news_id}")
        seen.add(news_id)
        result.append({"news_id": news_id, "title": _text(row.get("title"), f"title for {news_id}", 2000),
                       "body": None, "url": _url(row.get("url"), news_id),
                       "publish_time": _timestamp(row.get("published_at"), f"publish_time for {news_id}"),
                       "content_type": row["content_type"], "content_state": "title_only", "fragments": [],
                       "quality_flags": ["title_only", "body_missing"], "source": dict(source), "priority": 200})
    return result


def load_existing_sources(root: Path) -> list[dict[str, Any]]:
    """Load all five source families, retaining overlapping observations.

    Failures raise SourceLoadError; a missing package is never silently omitted.
    Returned bodies remain untrusted public content. Article/video types in the
    frozen title catalog are preserved, and no behavior metrics are imported.
    """
    root = Path(root).resolve(strict=True)
    result: list[dict[str, Any]] = []
    for args in _INCREMENTAL:
        result.extend(_incremental(root, *args))
    result.extend(_cached_articles(root))
    result.extend(_historical_fragments(root))
    result.extend(_headlines(root))
    return sorted(result, key=lambda item: (item["news_id"], item["priority"], item["source"]["path"]))


def source_summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Return metadata-only inventory using the release model's selection rule."""
    selected: dict[str, dict[str, Any]] = {}
    selected_keys: dict[str, tuple[int, int, str]] = {}
    observed_body_hashes: dict[str, set[str]] = defaultdict(set)
    package_records: Counter[str] = Counter()
    package_news: dict[str, set[str]] = defaultdict(set)
    package_files: dict[str, set[str]] = defaultdict(set)
    for item in candidates:
        record = normalize_record(item)
        priority = item["priority"]
        if type(priority) is not int or priority < 0:
            raise ValueError("source priority must be a nonnegative integer")
        news_id = record["news_id"]
        key = (STATES[record["content_state"]], priority, record["record_sha256"])
        if news_id not in selected_keys or key < selected_keys[news_id]:
            selected_keys[news_id] = key
            selected[news_id] = record
        if record["body_sha256"] is not None:
            observed_body_hashes[news_id].add(record["body_sha256"])
        package = item["source"]["package"]
        package_records[package] += 1
        package_news[package].add(news_id)
        package_files[package].add(item["source"]["path"])
    bodies: dict[str, list[str]] = defaultdict(list)
    selected_flags: Counter[str] = Counter()
    for item in selected.values():
        flags = set(item["quality_flags"])
        if len(observed_body_hashes[item["news_id"]]) > 1:
            flags.add("conflicting_body_variants")
        selected_flags.update(flags)
        if item["body"] is not None:
            bodies[item["body_sha256"]].append(item["news_id"])
    duplicates = [ids for ids in bodies.values() if len(ids) > 1]
    return {
        "candidate_records": len(candidates), "unique_news_ids": len(selected),
        "overlapping_source_records": len(candidates) - len(selected),
        "selected_content_states": dict(sorted(Counter(row["content_state"] for row in selected.values()).items())),
        "selected_content_types": dict(sorted(Counter(row["content_type"] for row in selected.values()).items())),
        "selected_quality_flags": dict(sorted(selected_flags.items())),
        "historical_fragment_rows": sum(len(row["fragments"]) for row in candidates),
        "duplicate_body_groups": len(duplicates), "duplicate_body_extra_news_ids": sum(len(ids) - 1 for ids in duplicates),
        "packages": [{"package": name, "candidate_records": package_records[name],
                      "unique_news_ids": len(package_news[name]), "source_files": len(package_files[name])}
                     for name in sorted(package_records)],
        "boundaries": ["public_content_only", "no_enterprise_behavior_imported", "cached_body_completeness_not_certified",
                       "historical_fragments_not_reconstructed", "headline_article_video_type_preserved",
                       "short_or_metadata_review_records_retained_with_flags"],
    }
