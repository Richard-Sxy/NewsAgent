"""Immutable real public-news content for the independent v8 warehouse.

This is an offline content roster, not a news crawler or an Agent adapter. It
never reads credentials, writes source caches, or claims simulated statistics
are Tencent's internal measurements. Article content remains untrusted data.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from zoneinfo import ZoneInfo

from simulation.warehouse_simulation.content_features import derive_content_features, validate_content_features

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = Path(__file__).resolve().parents[2] / "data/real-news-warehouse-v8/catalog.json"
CATALOG_SHA256 = "1af51f4fba5f190829d255fe8ee2ce2328811f09102af6d9e814e2b49d4353f0"
CATALOG_VERSION = "real-news-warehouse-catalog-v8"
DATASET_VERSION = "real-news-warehouse-v8-20261014"
ARTICLE_COUNT = 661
SOURCE_LABEL = "腾讯新闻公开页面缓存"
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")
CORPUS_MANIFEST = "agent/data/corpus-news-v6/catalog.json"
CORPUS_MANIFEST_SHA256 = "bb6e550a3881091355dcc7e5e866a184f62f77b41f0100b1f9bdfeb56c49bc3d"
INCREMENTAL_MANIFEST = "news/data/incremental/2026-10-07_2026-10-14-v1/manifest.json"
INCREMENTAL_MANIFEST_SHA256 = "13ffd2d5a4145a4f68cd5ba25fead17340fc7d7a70e0efa02632e75ae58fdcee"
INCREMENTAL_V2_MANIFEST = "news/data/incremental/2026-10-07_2026-10-14-v2/manifest.json"
INCREMENTAL_V2_MANIFEST_SHA256 = "f1262e6f4a8a212b35233363eb91df58f34d9407a45b1f541736729c4bd9dac4"
EXTENSION_PATH = Path(__file__).resolve().parents[2] / "data/real-news-warehouse-v8/catalog-extension-v2.json"
EXTENSION_VERSION = "real-news-warehouse-extension-v2-20261018"
EXTENSION_COUNT = 7459
EXTENSION_SHA256 = "6b7e65df3590ce84974adcf77d4505bc494e05ecbd5a0b1e7c5eec1732ad766f"
# Windows v3 is accepted explicitly; the established WSL v2 stays the default.
V3_EXTENSION_VERSION = "real-news-warehouse-extension-v3-20261023"
V3_EXTENSION_SHA256 = "2cf4566f54c5641076d45f6a18d25f157f9d45c2019fa364eac9d728c9fc12dc"
CATALOG_SOURCE_PACKAGES = {
    "corpus-v6": (CORPUS_MANIFEST, CORPUS_MANIFEST_SHA256, 120,
                  "news/data/local_recall/articles-v1", "title-keyword-demo-v1"),
    "incremental-20261014-v1": (INCREMENTAL_MANIFEST, INCREMENTAL_MANIFEST_SHA256, 541,
                               "news/data/incremental/2026-10-07_2026-10-14-v1/articles",
                               "public-discovery-channel-map-v1"),
}
SOURCE_PACKAGES = {
    **CATALOG_SOURCE_PACKAGES,
    "incremental-20261014-v2": (INCREMENTAL_V2_MANIFEST, INCREMENTAL_V2_MANIFEST_SHA256, 8000,
                               "news/data/incremental/2026-10-07_2026-10-14-v2/articles",
                               "public-discovery-channel-map-v1"),
}
CHANNEL_LABELS = {"tech": "科技", "finance": "财经", "sports": "体育", "ent": "娱乐",
                  "auto": "汽车", "edu": "教育", "public": "社会"}
CATEGORY_COUNTS = {"体育": 87, "教育": 101, "汽车": 148, "社会": 62, "科技": 94,
                   "财经": 89, "娱乐": 80}
CATEGORY_DESCRIPTION = (
    "演示业务标签；v6按标题关键词分类，增量按公开列表发现入口映射。"
    "不代表原文的可信编辑栏目，不据此编造原始媒体或内容类型。"
)
REFERENCE_NEWS_ID = "20260903A05YQE00"
REFERENCE_CONTENT_SHA256 = "fa81c93c8f95e95b1463a7ad480f7c4cff5b4a090c7c1ebefb6acda451f5ebe2"
_ID = re.compile(r"^[0-9]{8}[A-Za-z0-9]{4,32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_ARTICLE_KEYS = frozenset({
    "news_id", "title", "content", "url", "publish_time", "publish_time_raw",
    "content_version", "fetched_at", "category", "category_data_synthetic",
    "category_label_source", "source_label", "content_type", "news_content_synthetic",
    "content_sha256", "source_file", "source_sha256", "source_package",
    "source_manifest_path", "source_manifest_sha256", "source_html_sha256",
})
_CATALOG_KEYS = frozenset({
    "catalog_version", "dataset_version", "article_count", "source_label",
    "news_content_synthetic", "metric_data_synthetic", "category_description",
    "category_counts", "sources", "provenance", "articles",
})
_EXTENSION_KEYS = frozenset({
    "schema_version", "extension_version", "catalog_version", "dataset_version",
    "source_label", "source_package", "source_manifest_path", "source_manifest_sha256",
    "source_directory", "catalog_sha256", "article_count", "category_counts",
    "news_content_synthetic", "metric_data_synthetic", "metric_available",
    "category_description", "articles",
})
_EXTENSION_ARTICLE_KEYS = frozenset({
    "news_id", "title", "url", "publish_time", "publish_time_raw", "content_version",
    "fetched_at", "category", "category_data_synthetic", "category_label_source",
    "source_label", "content_type", "news_content_synthetic", "metric_available",
    "content_sha256", "source_file", "source_sha256", "source_package",
    "source_manifest_path", "source_manifest_sha256", "source_html_sha256",
    "title_chars", "content_chars", "paragraph_count", "sentence_count",
    "digit_token_count", "keyword_top",
})


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in real-news catalog or source")
        result[key] = value
    return result


def _bounded_json(path: Path, maximum: int, expected_digest: str | None = None) -> Any:
    # This module only accepts explicit JSON artifacts, never environment files.
    if path.suffix != ".json" or path.is_symlink():
        raise ValueError("real-news source must be a regular JSON artifact")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise ValueError("real-news artifact exceeds its size budget or is empty")
    if expected_digest is not None and sha256(raw).hexdigest() != expected_digest:
        raise ValueError("real-news artifact SHA-256 differs from frozen identity")
    try:
        return json.loads(raw, object_pairs_hook=_strict_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValueError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 JSON real-news artifact") from exc


def _text(value: Any, label: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\x00" in value):
        raise ValueError(f"invalid real-news {label}")
    return value


def _timestamp(value: Any, *, allow_naive: bool = False) -> datetime:
    raw = _text(value, "timestamp", 80)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid real-news timestamp") from exc
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("real-news timestamp requires a timezone")
        parsed = parsed.replace(tzinfo=TZ)
    if parsed.utcoffset() is None:
        raise ValueError("invalid real-news timezone")
    return parsed


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"invalid real-news {label} digest")
    return value


def _resolve_source(root: Path, relative: str) -> Path:
    """Resolve frozen source names across the data-root move, without symlinks."""
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or "\\" in relative or ":" in relative:
        raise ValueError("real-news source path escapes the repository")
    path = root / relative
    # Windows checkouts may not carry the old news/data/incremental symlink.
    # Never fall back from an existing but corrupt legacy artifact.
    if not path.exists() and not path.is_symlink() and relative.startswith("news/data/incremental/"):
        path = root / "var" / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("real-news source path escapes the repository")
    return path


def _source_path(root: Path, relative: str, directory: str, news_id: str) -> Path:
    expected = f"{directory}/{news_id}.json"
    if (not isinstance(relative, str) or relative != expected
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts):
        raise ValueError("real-news source path or identity mismatch")
    return _resolve_source(root, relative)


def _validate_article(row: Any) -> None:
    if not isinstance(row, dict) or set(row) != _ARTICLE_KEYS:
        raise ValueError("invalid real-news catalog article fields")
    news_id = row["news_id"]
    if not isinstance(news_id, str) or _ID.fullmatch(news_id) is None:
        raise ValueError("invalid real-news news_id")
    if row["url"] != f"https://news.qq.com/rain/a/{news_id}":
        raise ValueError("real-news source URL or news_id mismatch")
    _text(row["title"], "title", 500)
    body = _text(row["content"], "content", 200_000)
    if _digest(row["content_sha256"], "content") != sha256(body.encode("utf-8")).hexdigest():
        raise ValueError("real-news body SHA-256 mismatch")
    if type(row["content_version"]) is not int or not 1 <= row["content_version"] <= 100_000:
        raise ValueError("invalid real-news content version")
    published = _timestamp(row["publish_time"])
    if published != _timestamp(row["publish_time_raw"], allow_naive=True):
        raise ValueError("real-news publication differs from original metadata")
    if row["publish_time"] != published.astimezone(TZ).isoformat():
        raise ValueError("real-news publication must be normalized to Asia/Shanghai")
    _timestamp(row["fetched_at"])
    if (row["source_label"] != SOURCE_LABEL or row["content_type"] != "article"
            or row["news_content_synthetic"] is not False
            or row["category_data_synthetic"] is not True):
        raise ValueError("real-news content or demo-label provenance mismatch")
    package = row["source_package"]
    if not isinstance(package, str) or package not in SOURCE_PACKAGES:
        raise ValueError("unknown real-news source package")
    manifest, manifest_digest, _, directory, label_source = SOURCE_PACKAGES[package]
    if (row["source_manifest_path"] != manifest or row["source_manifest_sha256"] != manifest_digest
            or row["source_file"] != f"{directory}/{news_id}.json"
            or row["category_label_source"] != label_source):
        raise ValueError("real-news source path or manifest provenance mismatch")
    _digest(row["source_sha256"], "source file")
    allowed = {"科技", "财经", "体育", "社会"} if package == "corpus-v6" else set(CHANNEL_LABELS.values())
    if not isinstance(row["category"], str) or row["category"] not in allowed:
        raise ValueError("invalid real-news category label")
    if package == "corpus-v6":
        if row["source_html_sha256"] is not None:
            raise ValueError("old cache has no verified HTML digest")
    else:
        _digest(row["source_html_sha256"], "source HTML")
        if not "2026-10-07" <= published.date().isoformat() <= "2026-10-14":
            raise ValueError("incremental real-news publication outside frozen window")


def _sources() -> list[dict[str, Any]]:
    return [{"source_package": package, "manifest_path": values[0],
             "manifest_sha256": values[1], "article_count": values[2]}
            for package, values in CATALOG_SOURCE_PACKAGES.items()]


def _provenance() -> dict[str, Any]:
    return {
        "verification_scope": "单条推荐新闻在线复核；全量来源文件及冻结manifest离线校验",
        "reference_article": {
            "news_id": REFERENCE_NEWS_ID,
            "title": "WorkBuddy开放平台上线，AI办公迎来硬件入口之争",
            "url": f"https://news.qq.com/rain/a/{REFERENCE_NEWS_ID}",
            "publish_time": "2026-09-03T12:18:15+08:00",
            "content_sha256": REFERENCE_CONTENT_SHA256,
            "verification_date": "2026-10-14", "http_status": 200,
            "method": "公开页面HTTP读取，现有fetch_html/parse_article解析；标题、URL、时间、完整正文与v6冻结缓存一致",
        },
        "limitations": [
            "661篇来自已有公开缓存，不构成腾讯新闻全量内容档案。",
            "除推荐参考新闻外，冻结时不重新发起全量网络请求。",
            "指标、行为、运营标签和生命周期场景由独立模拟数仓生成，不能代表新闻真实热度或真实撤稿。",
            "没有可信原始媒体名称的样本只标公开页面缓存，不补造媒体或视频元数据。",
            "本目录供离线数仓使用；Agent、企业行为数据和向量库清理尚未因本次冻结而接入。",
        ],
    }


def _validate_payload(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, dict) or set(payload) != _CATALOG_KEYS:
        raise ValueError("invalid real-news catalog fields")
    if (payload["catalog_version"] != CATALOG_VERSION or payload["dataset_version"] != DATASET_VERSION
            or type(payload["article_count"]) is not int or payload["article_count"] != ARTICLE_COUNT
            or payload["source_label"] != SOURCE_LABEL or payload["news_content_synthetic"] is not False
            or payload["metric_data_synthetic"] is not True
            or payload["category_description"] != CATEGORY_DESCRIPTION
            or payload["sources"] != _sources() or payload["provenance"] != _provenance()):
        raise ValueError("real-news catalog version or provenance mismatch")
    articles = payload["articles"]
    if not isinstance(articles, list) or len(articles) != ARTICLE_COUNT:
        raise ValueError("invalid real-news catalog article count")
    ids, categories, packages = [], Counter(), Counter()
    for row in articles:
        _validate_article(row)
        ids.append(row["news_id"])
        categories[row["category"]] += 1
        packages[row["source_package"]] += 1
    if ids != sorted(set(ids)):
        raise ValueError("real-news catalog must contain sorted unique news IDs")
    if (dict(categories) != CATEGORY_COUNTS or payload["category_counts"] != CATEGORY_COUNTS
            or any(type(value) is not int for value in payload["category_counts"].values())
            or dict(packages) != {package: values[2] for package, values in CATALOG_SOURCE_PACKAGES.items()}):
        raise ValueError("real-news catalog category or source counts mismatch")
    reference = next((row for row in articles if row["news_id"] == REFERENCE_NEWS_ID), None)
    if reference is None:
        raise ValueError("verified real-news reference article is missing")
    if any(reference[key] != value for key, value in _provenance()["reference_article"].items()
           if key in {"news_id", "title", "url", "publish_time", "content_sha256"}):
        raise ValueError("real-news reference article differs from verified source")
    return tuple(deepcopy(articles))


def load_catalog(path: Path | str | None = None) -> tuple[dict[str, Any], ...]:
    """Read a copy of the fixed v8 catalog; never consult mutable source caches."""
    selected = CATALOG_PATH if path is None else Path(path)
    if selected.name != "catalog.json":
        raise ValueError("frozen real-news catalog must be named catalog.json")
    return _validate_payload(_bounded_json(selected, MAX_CATALOG_BYTES, CATALOG_SHA256))


def _source_article(root: Path, source: dict[str, Any], *, package: str,
                    source_file: str, source_digest: str, category: str) -> dict[str, Any]:
    _, _, _, directory, label_source = SOURCE_PACKAGES[package]
    news_id = source.get("news_id")
    if not isinstance(news_id, str) or _ID.fullmatch(news_id) is None:
        raise ValueError("invalid source article ID")
    path = _source_path(root, source_file, directory, news_id)
    cached = _bounded_json(path, MAX_SOURCE_BYTES, _digest(source_digest, "source file"))
    base_keys = {"url", "news_id", "title", "content", "publish_time", "content_version", "fetched_at"}
    extra_keys = {"publish_time_original", "category", "discovery_channels", "feed_publish_time",
                  "source_html_sha256", "document_sha256"} if package != "corpus-v6" else set()
    if package == "incremental-20261014-v2":
        extra_keys.add("candidate_date_hint")
    if not isinstance(cached, dict) or set(cached) != base_keys | extra_keys:
        raise ValueError("invalid frozen source article fields")
    if cached["news_id"] != news_id or cached["url"] != f"https://news.qq.com/rain/a/{news_id}":
        raise ValueError("frozen source article URL or identity mismatch")
    published = _timestamp(cached["publish_time"], allow_naive=True).astimezone(TZ)
    if package == "corpus-v6":
        if any(source.get(key) != cached[key] for key in ("title", "content", "url", "content_version", "fetched_at")):
            raise ValueError("v6 frozen metadata or body differs from original source")
        raw_time = cached["publish_time"]
        if source.get("publish_time_raw") != raw_time or _timestamp(source.get("publish_time")) != published:
            raise ValueError("v6 frozen publication differs from original source")
        html_digest = None
    else:
        raw_time = cached["publish_time_original"]
        if _timestamp(raw_time, allow_naive=True) != published or _timestamp(source.get("publish_time")) != published:
            raise ValueError("incremental publication differs from original source")
        if cached["category"] != source.get("category"):
            raise ValueError("incremental discovery category differs from manifest")
        body_digest = sha256((cached["title"] + "\x1f" + cached["content"]).encode()).hexdigest()
        if (cached["document_sha256"] != body_digest or source.get("document_sha256") != body_digest
                or source.get("content_characters") != len(cached["content"])):
            raise ValueError("incremental document body digest or length mismatch")
        html_digest = _digest(cached["source_html_sha256"], "source HTML")
        if html_digest != source.get("source_html_sha256"):
            raise ValueError("incremental source HTML differs from manifest")
    manifest, manifest_digest, *_ = SOURCE_PACKAGES[package]
    row = {
        "news_id": news_id, "title": cached["title"], "content": cached["content"],
        "url": cached["url"], "publish_time": published.isoformat(), "publish_time_raw": raw_time,
        "content_version": cached["content_version"], "fetched_at": cached["fetched_at"],
        "category": category, "category_data_synthetic": True, "category_label_source": label_source,
        "source_label": SOURCE_LABEL, "content_type": "article", "news_content_synthetic": False,
        "content_sha256": sha256(cached["content"].encode()).hexdigest(),
        "source_file": source_file, "source_sha256": source_digest, "source_package": package,
        "source_manifest_path": manifest, "source_manifest_sha256": manifest_digest,
        "source_html_sha256": html_digest,
    }
    _validate_article(row)
    return row


def build_catalog(repository_root: Path | str = REPOSITORY_ROOT) -> dict[str, Any]:
    """Rebuild the exact catalog from two independently hash-pinned packages."""
    root = Path(repository_root).resolve()
    corpus = _bounded_json(root / CORPUS_MANIFEST, MAX_SOURCE_BYTES, CORPUS_MANIFEST_SHA256)
    incremental = _bounded_json(_resolve_source(root, INCREMENTAL_MANIFEST), MAX_SOURCE_BYTES, INCREMENTAL_MANIFEST_SHA256)
    if (not isinstance(corpus, dict) or corpus.get("dataset_profile") != "corpus-v6"
            or corpus.get("corpus_count") != 120 or not isinstance(corpus.get("articles"), list)
            or len(corpus["articles"]) != 120 or not isinstance(incremental, dict)
            or incremental.get("schema_version") != 1 or incremental.get("timezone") != "Asia/Shanghai"
            or incremental.get("start_date") != "2026-10-07" or incremental.get("end_date") != "2026-10-14"
            or not isinstance(incremental.get("articles"), list) or len(incremental["articles"]) != 541):
        raise ValueError("invalid frozen source manifest identity or count")
    articles: dict[str, dict[str, Any]] = {}
    for source in corpus["articles"]:
        row = _source_article(root, source, package="corpus-v6", source_file=source["source_file"],
                              source_digest=source["source_sha256"], category=source["category"])
        if row["news_id"] in articles:
            raise ValueError("duplicate article in v6 frozen source")
        articles[row["news_id"]] = row
    for source in incremental["articles"]:
        news_id = source.get("news_id")
        if source.get("relative_path") != f"articles/{news_id}.json" or source.get("url") != f"https://news.qq.com/rain/a/{news_id}":
            raise ValueError("incremental manifest path or source URL mismatch")
        category = CHANNEL_LABELS.get(source.get("category"))
        if category is None:
            raise ValueError("unknown incremental discovery category")
        row = _source_article(root, source, package="incremental-20261014-v1",
                              source_file=str(PurePosixPath(INCREMENTAL_MANIFEST).parent / source["relative_path"]),
                              source_digest=source["sha256"], category=category)
        if row["news_id"] in articles:
            # Never merge a title, timestamp, body, or label from another version.
            if any(articles[row["news_id"]][key] != row[key]
                   for key in ("title", "content", "url", "publish_time", "content_version", "category")):
                raise ValueError("conflicting metadata for duplicate real-news ID")
            continue
        articles[row["news_id"]] = row
    payload = {
        "catalog_version": CATALOG_VERSION, "dataset_version": DATASET_VERSION,
        "article_count": len(articles), "source_label": SOURCE_LABEL,
        "news_content_synthetic": False, "metric_data_synthetic": True,
        "category_description": CATEGORY_DESCRIPTION,
        "category_counts": dict(sorted(Counter(row["category"] for row in articles.values()).items())),
        "sources": _sources(), "provenance": _provenance(),
        "articles": [articles[news_id] for news_id in sorted(articles)],
    }
    _validate_payload(payload)
    return payload


def freeze_catalog(output_dir: Path | str, *, repository_root: Path | str = REPOSITORY_ROOT) -> dict[str, Any]:
    """Validate before creating a new directory; refuse any existing destination."""
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise ValueError("real-news catalog output directory already exists; refusing overwrite")
    payload = build_catalog(repository_root)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("real-news catalog exceeds size budget")
    output.mkdir(parents=True, exist_ok=False)
    target = output / "catalog.json"
    with target.open("xb") as stream:
        stream.write(raw)
    return {"catalog_path": str(target.absolute()), "catalog_sha256": sha256(raw).hexdigest(),
            "article_count": payload["article_count"], "category_counts": payload["category_counts"],
            "sources": payload["sources"], "news_content_synthetic": False,
            "metric_data_synthetic": True}


def _validate_extension_article(row: Any) -> None:
    """Validate metadata/features without reading the source body again."""

    if not isinstance(row, dict) or set(row) != _EXTENSION_ARTICLE_KEYS:
        raise ValueError("invalid real-news extension article fields")
    news_id = row["news_id"]
    if not isinstance(news_id, str) or _ID.fullmatch(news_id) is None:
        raise ValueError("invalid real-news extension news_id")
    if row["url"] != f"https://news.qq.com/rain/a/{news_id}":
        raise ValueError("real-news extension URL or news_id mismatch")
    _text(row["title"], "title", 500)
    if (row["source_label"] != SOURCE_LABEL or row["content_type"] != "article"
            or row["news_content_synthetic"] is not False or row["metric_available"] is not False
            or row["category_data_synthetic"] is not True):
        raise ValueError("real-news extension provenance mismatch")
    if row["category"] not in set(CHANNEL_LABELS.values()):
        raise ValueError("real-news extension category is not whitelisted")
    if type(row["content_version"]) is not int or not 1 <= row["content_version"] <= 100_000:
        raise ValueError("invalid real-news extension content version")
    published = _timestamp(row["publish_time"])
    if published != _timestamp(row["publish_time_raw"], allow_naive=True):
        raise ValueError("real-news extension publication differs from original metadata")
    if row["publish_time"] != published.astimezone(TZ).isoformat():
        raise ValueError("real-news extension publication must be normalized to Asia/Shanghai")
    _timestamp(row["fetched_at"])
    _digest(row["content_sha256"], "content")
    _digest(row["source_sha256"], "source")
    _digest(row["source_html_sha256"], "source HTML")
    if row["source_package"] != "incremental-20261014-v2":
        raise ValueError("real-news extension source package is not approved")
    if row["source_file"] != (
            f"{PurePosixPath(INCREMENTAL_V2_MANIFEST).parent}/articles/{news_id}.json"):
        raise ValueError("real-news extension source path or identity mismatch")
    if row["source_manifest_path"] != INCREMENTAL_V2_MANIFEST or row["source_manifest_sha256"] != INCREMENTAL_V2_MANIFEST_SHA256:
        raise ValueError("real-news extension source manifest differs from the frozen identity")
    for name in ("title_chars", "content_chars", "paragraph_count", "sentence_count", "digit_token_count"):
        if type(row[name]) is not int or row[name] < 0:
            raise ValueError("invalid real-news extension content feature count")
    keywords = row["keyword_top"]
    if (not isinstance(keywords, list) or len(keywords) > 8
            or any(not isinstance(value, str) or not value for value in keywords)
            or len(keywords) != len(set(keywords))):
        raise ValueError("invalid real-news extension keyword list")


def _validate_extension(payload: Any, *, version: str = EXTENSION_VERSION) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, dict) or set(payload) != _EXTENSION_KEYS:
        raise ValueError("invalid real-news extension fields")
    if (payload["schema_version"] != 1 or payload["extension_version"] != version
            or payload["catalog_version"] != CATALOG_VERSION or payload["dataset_version"] != DATASET_VERSION
            or payload["source_label"] != SOURCE_LABEL or payload["source_package"] != "incremental-20261014-v2"
            or payload["source_manifest_path"] != INCREMENTAL_V2_MANIFEST
            or payload["source_manifest_sha256"] != INCREMENTAL_V2_MANIFEST_SHA256
            or payload["source_directory"] != f"{PurePosixPath(INCREMENTAL_V2_MANIFEST).parent}/articles"
            or payload["catalog_sha256"] != CATALOG_SHA256
            or type(payload["article_count"]) is not int or payload["article_count"] != EXTENSION_COUNT
            or payload["news_content_synthetic"] is not False or payload["metric_data_synthetic"] is not True
            or payload["metric_available"] is not False or payload["category_description"] != CATEGORY_DESCRIPTION):
        raise ValueError("real-news extension version or provenance mismatch")
    articles = payload["articles"]
    if not isinstance(articles, list) or len(articles) != EXTENSION_COUNT:
        raise ValueError("invalid real-news extension article count")
    ids: list[str] = []
    categories: Counter[str] = Counter()
    for row in articles:
        _validate_extension_article(row)
        ids.append(row["news_id"])
        categories[row["category"]] += 1
    if ids != sorted(set(ids)):
        raise ValueError("real-news extension must contain sorted unique news IDs")
    if dict(categories) != payload["category_counts"]:
        raise ValueError("real-news extension category counts mismatch")
    return tuple(deepcopy(articles))


def load_extension(path: Path | str | None = None) -> tuple[dict[str, Any], ...]:
    """Read the frozen real-content metadata/feature manifest, never source caches."""

    selected = EXTENSION_PATH if path is None else Path(path)
    if selected.name == EXTENSION_PATH.name:
        version, digest = EXTENSION_VERSION, EXTENSION_SHA256
    elif selected.name == "catalog-extension-v3.json":
        version, digest = V3_EXTENSION_VERSION, V3_EXTENSION_SHA256
    else:
        raise ValueError("frozen real-news extension must use its versioned filename")
    return _validate_extension(_bounded_json(selected, MAX_MANIFEST_BYTES, digest), version=version)


def build_extension(repository_root: Path | str = REPOSITORY_ROOT) -> dict[str, Any]:
    """Build metadata/features for v2 public articles not already in catalog.json."""

    root = Path(repository_root).resolve()
    catalog = load_catalog()
    catalog_ids = {row["news_id"] for row in catalog}
    manifest = _bounded_json(_resolve_source(root, INCREMENTAL_V2_MANIFEST), MAX_MANIFEST_BYTES,
                             INCREMENTAL_V2_MANIFEST_SHA256)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("timezone") != "Asia/Shanghai"
            or manifest.get("start_date") != "2026-10-07" or manifest.get("end_date") != "2026-10-14"
            or not isinstance(manifest.get("articles"), list) or len(manifest["articles"]) != 8000):
        raise ValueError("invalid incremental v2 source manifest identity or count")
    rows: dict[str, dict[str, Any]] = {}
    package = "incremental-20261014-v2"
    for source in manifest["articles"]:
        news_id = source.get("news_id")
        if not isinstance(news_id, str) or _ID.fullmatch(news_id) is None:
            raise ValueError("invalid incremental v2 article ID")
        if source.get("relative_path") != f"articles/{news_id}.json" or source.get("url") != f"https://news.qq.com/rain/a/{news_id}":
            raise ValueError("incremental v2 manifest path or source URL mismatch")
        if news_id in catalog_ids:
            continue
        category = CHANNEL_LABELS.get(source.get("category"))
        if category is None:
            raise ValueError("unknown incremental v2 discovery category")
        source_file = str(PurePosixPath(INCREMENTAL_V2_MANIFEST).parent / source["relative_path"])
        row = _source_article(root, source, package=package, source_file=source_file,
                              source_digest=source["sha256"], category=category)
        if news_id in rows:
            raise ValueError("duplicate article in incremental v2 source")
        features = derive_content_features(row["title"], row["content"])
        metadata = {key: row[key] for key in _ARTICLE_KEYS if key != "content"}
        metadata.update(metric_available=False, **features)
        _validate_extension_article(metadata)
        rows[news_id] = metadata
    articles = [rows[news_id] for news_id in sorted(rows)]
    category_counts = dict(sorted(Counter(row["category"] for row in articles).items()))
    payload = {
        "schema_version": 1,
        "extension_version": EXTENSION_VERSION,
        "catalog_version": CATALOG_VERSION,
        "dataset_version": DATASET_VERSION,
        "source_label": SOURCE_LABEL,
        "source_package": package,
        "source_manifest_path": INCREMENTAL_V2_MANIFEST,
        "source_manifest_sha256": INCREMENTAL_V2_MANIFEST_SHA256,
        "source_directory": f"{PurePosixPath(INCREMENTAL_V2_MANIFEST).parent}/articles",
        "catalog_sha256": CATALOG_SHA256,
        "article_count": len(articles),
        "category_counts": category_counts,
        "news_content_synthetic": False,
        "metric_data_synthetic": True,
        "metric_available": False,
        "category_description": CATEGORY_DESCRIPTION,
        "articles": articles,
    }
    _validate_extension(payload)
    return payload


def freeze_extension(output_path: Path | str = EXTENSION_PATH, *,
                     repository_root: Path | str = REPOSITORY_ROOT,
                     overwrite: bool = False) -> dict[str, Any]:
    """Write a reviewed extension manifest; do not embed article bodies in Git."""

    target = Path(output_path)
    if target.exists() and not overwrite:
        raise ValueError("real-news extension output already exists; refusing overwrite")
    payload = build_extension(repository_root)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if sha256(raw).hexdigest() != EXTENSION_SHA256:
        raise ValueError("rebuilt extension differs from the frozen version; create a reviewed new version")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("real-news extension exceeds its size budget")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
    temporary.replace(target)
    return {"extension_path": str(target.absolute()), "extension_sha256": sha256(raw).hexdigest(),
            "article_count": payload["article_count"], "category_counts": payload["category_counts"],
            "news_content_synthetic": False, "metric_data_synthetic": True,
            "metric_available": False}


def _extension_content(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    """Read one approved source body and verify metadata/features before generation."""

    path = _resolve_source(root, row["source_file"])
    cached = _bounded_json(path, MAX_SOURCE_BYTES, row["source_sha256"])
    expected_keys = {"url", "news_id", "title", "content", "publish_time", "content_version", "fetched_at",
                     "publish_time_original", "category", "discovery_channels", "feed_publish_time",
                     "candidate_date_hint", "source_html_sha256", "document_sha256"}
    if not isinstance(cached, dict) or set(cached) != expected_keys:
        raise ValueError("invalid incremental v2 source article fields")
    news_id = row["news_id"]
    if cached["news_id"] != news_id or cached["url"] != row["url"] or cached["title"] != row["title"]:
        raise ValueError("real-news extension identity or title differs from source")
    if cached["content_version"] != row["content_version"]:
        raise ValueError("real-news extension content version differs from source")
    published = _timestamp(cached["publish_time"], allow_naive=True).astimezone(TZ)
    if published != _timestamp(row["publish_time"]):
        raise ValueError("real-news extension publication differs from source")
    if cached["source_html_sha256"] != row["source_html_sha256"]:
        raise ValueError("real-news extension source HTML differs from source")
    body = cached["content"]
    if sha256(body.encode()).hexdigest() != row["content_sha256"]:
        raise ValueError("real-news extension content SHA-256 differs from source")
    document_digest = sha256((cached["title"] + "\x1f" + body).encode()).hexdigest()
    if document_digest != cached["document_sha256"]:
        raise ValueError("real-news extension document digest differs from source")
    validate_content_features({name: row[name] for name in (
        "title_chars", "content_chars", "paragraph_count", "sentence_count",
        "digit_token_count", "keyword_top")}, cached["title"], body)
    result = {key: row[key] for key in _ARTICLE_KEYS if key != "content"}
    result.update(content=body, metric_available=False, content_features={name: row[name] for name in (
        "title_chars", "content_chars", "paragraph_count", "sentence_count",
        "digit_token_count", "keyword_top")})
    return result


@lru_cache(maxsize=2)
def _load_extended_catalog(root_text: str, extension_digest: str) -> tuple[dict[str, Any], ...]:
    root = Path(root_text)
    catalog = load_catalog(root / "agent/data/real-news-warehouse-v8/catalog.json")
    extension = load_extension(root / "agent/data/real-news-warehouse-v8" / EXTENSION_PATH.name)
    ids = {row["news_id"] for row in catalog}
    merged = list(catalog)
    for row in extension:
        if row["news_id"] in ids:
            raise ValueError("real-news extension duplicates a frozen catalog news_id")
        merged.append(_extension_content(root, row))
        ids.add(row["news_id"])
    if len(merged) != ARTICLE_COUNT + EXTENSION_COUNT:
        raise ValueError("extended real-news catalog count differs from the frozen identity")
    return tuple(sorted(merged, key=lambda item: item["news_id"]))


def load_extended_catalog() -> tuple[dict[str, Any], ...]:
    """Return catalog bodies plus verified v2 extension bodies; no source cache guessing."""

    if not EXTENSION_SHA256:
        raise ValueError("real-news extension manifest digest has not been frozen")
    return _load_extended_catalog(str(REPOSITORY_ROOT), EXTENSION_SHA256)
