"""Immutable real public-news content for the independent v8 warehouse.

This is an offline content roster, not a news crawler or an Agent adapter. It
never reads credentials, writes source caches, or claims simulated statistics
are Tencent's internal measurements. Article content remains untrusted data.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = Path(__file__).resolve().parents[2] / "data/real-news-warehouse-v8/catalog.json"
CATALOG_SHA256 = "1af51f4fba5f190829d255fe8ee2ce2328811f09102af6d9e814e2b49d4353f0"
CATALOG_VERSION = "real-news-warehouse-catalog-v8"
DATASET_VERSION = "real-news-warehouse-v8-20261014"
ARTICLE_COUNT = 661
SOURCE_LABEL = "腾讯新闻公开页面缓存"
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")
CORPUS_MANIFEST = "agent/data/corpus-news-v6/catalog.json"
CORPUS_MANIFEST_SHA256 = "bb6e550a3881091355dcc7e5e866a184f62f77b41f0100b1f9bdfeb56c49bc3d"
INCREMENTAL_MANIFEST = "news/data/incremental/2026-10-07_2026-10-14-v1/manifest.json"
INCREMENTAL_MANIFEST_SHA256 = "13ffd2d5a4145a4f68cd5ba25fead17340fc7d7a70e0efa02632e75ae58fdcee"
SOURCE_PACKAGES = {
    "corpus-v6": (CORPUS_MANIFEST, CORPUS_MANIFEST_SHA256, 120,
                  "news/data/local_recall/articles-v1", "title-keyword-demo-v1"),
    "incremental-20261014-v1": (INCREMENTAL_MANIFEST, INCREMENTAL_MANIFEST_SHA256, 541,
                               "news/data/incremental/2026-10-07_2026-10-14-v1/articles",
                               "public-discovery-channel-map-v1"),
}
CHANNEL_LABELS = {"tech": "科技", "finance": "财经", "sports": "体育", "ent": "娱乐",
                  "auto": "汽车", "edu": "教育"}
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


def _source_path(root: Path, relative: str, directory: str, news_id: str) -> Path:
    expected = f"{directory}/{news_id}.json"
    if (not isinstance(relative, str) or relative != expected
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts):
        raise ValueError("real-news source path or identity mismatch")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("real-news source path escapes the repository")
    return path


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
            for package, values in SOURCE_PACKAGES.items()]


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
            or dict(packages) != {package: values[2] for package, values in SOURCE_PACKAGES.items()}):
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
    incremental = _bounded_json(root / INCREMENTAL_MANIFEST, MAX_SOURCE_BYTES, INCREMENTAL_MANIFEST_SHA256)
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
