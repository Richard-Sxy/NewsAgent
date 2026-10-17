"""Frozen public news content with explicitly synthetic timeline metrics.

The corpus is an immutable intersection of the existing Tencent page cache and
read-only BGE snapshot. Article text is data, never instructions. Publication
times come only from cache metadata; categories are reproducible demo labels,
not original editorial sections. No source cache or BGE index is modified.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.sql_assistant.scaled_profiles import TABLES, TENANTS, ScaledTableHasher
from app.sql_assistant.timeline_profiles import END, HOURS, START, iter_timeline_rows, timeline_scenarios


CORPUS_PROFILE = "corpus-v6"
CORPUS_DATASET_VERSION = "news-corpus-real-content-120-v1"
CORPUS_COUNT = 120
CORPUS_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data/corpus-news-v6/catalog.json"
CORPUS_CATALOG_SHA256 = "bb6e550a3881091355dcc7e5e866a184f62f77b41f0100b1f9bdfeb56c49bc3d"
BGE_MANIFEST_DIGEST = "e68c268dadcf434d81c5e397e578321b1c614215d42a81a5ae82848ba4901dab"
BGE_CHUNKS_SHA256 = "571e282d54bbb1834424153f80bb3f9f94292c99e956519b0ecc15a8225ea262"
NEWS_CONTENT_SOURCE = "tencent-news-cache"
NEWS_SOURCE_LABEL = "腾讯新闻公开页面缓存"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MIN_BODY_CHARS = 300
MAX_BODY_CHARS = 100_000
_CACHE_DIR = "news/data/local_recall/articles-v1"
_BGE_CORPUS_PATH = "rag-recall-demo/data/experiments/offline_recall_fg/chunks.jsonl"
_METADATA_EXCLUSIONS = (
    "20260819A044YC00", "20260819A0A1SP00", "20260820A05IYL00", "20260820A08IFI00",
    "20260822A09Y1N00", "20260822A09ZJO00", "20260822A0ACGY00",
)
_CATEGORY_RULES = (
    ("科技", ("人工智能", "智能", "机器人", "算力", "模型", "芯片", "AI", "WAIC", "量子",
              "卫星", "开源", "数据安全", "科技", "半导体")),
    ("财经", ("经济", "金融", "营收", "财报", "利润", "股", "融资", "估值", "消费", "投资",
              "财政", "银行", "市场", "资本", "现金流", "烧钱", "贸易")),
    ("体育", ("足球", "篮球", "球迷", "球员", "围棋", "网球", "乒乓", "联赛", "冠军", "奥运",
              "马拉松", "赛事", "体育", "曼联", "皇马", "国足", "英超", "中超", "武磊")),
)
_CATALOG_KEYS = frozenset({
    "catalog_version", "dataset_profile", "dataset_version", "corpus_count", "news_content_source",
    "metric_data_synthetic", "category_data_synthetic", "category_rule_version",
    "category_description", "bge_manifest_digest", "bge_chunks_sha256", "selection_policy",
    "selection", "articles",
})
_ARTICLE_KEYS = frozenset({
    "news_id", "title", "content", "content_version", "url", "publish_time", "publish_time_raw",
    "fetched_at", "category", "content_type", "source", "source_file", "source_sha256",
})
_SELECTION_KEYS = frozenset({
    "source_cache_dir", "bge_corpus_path", "cache_article_count", "bge_article_count",
    "bge_chunk_count", "intersection_count", "eligible_count", "minimum_body_chars",
    "reviewed_metadata_exclusion_ids",
})


def validate_corpus_news_count(value: int) -> int:
    if type(value) is not int or value != CORPUS_COUNT:
        raise ValueError("corpus-v6 news count must be exactly 120")
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate corpus catalog JSON key")
        result[key] = value
    return result


def _keys(value: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid corpus {label} fields")


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"invalid corpus {label}")
    return value


def _cache_timestamp(value: Any, label: str) -> datetime:
    text = _text(value, label, 80)
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"invalid corpus {label}") from exc
    # Match TencentNewsCacheRepository; do not fabricate a date from an ID or body.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if parsed.utcoffset() is None:
        raise ValueError(f"invalid corpus {label} timezone")
    return parsed


def _demo_category(title: str) -> str:
    folded = title.casefold()
    return next((category for category, terms in _CATEGORY_RULES
                 if any(term.casefold() in folded for term in terms)), "社会")


def _validate_catalog(payload: Any) -> dict[str, Any]:
    _keys(payload, _CATALOG_KEYS, "catalog")
    expected = {
        "catalog_version": "news-corpus-catalog-v1",
        "dataset_profile": CORPUS_PROFILE, "dataset_version": CORPUS_DATASET_VERSION,
        "news_content_source": NEWS_CONTENT_SOURCE, "category_rule_version": "title-keyword-demo-v1",
        "category_description": "演示板块分类，按标题关键词确定；不是新闻原始栏目。",
        "bge_manifest_digest": BGE_MANIFEST_DIGEST, "bge_chunks_sha256": BGE_CHUNKS_SHA256,
        "selection_policy": "cache-bge-intersection-sorted-news-id-v1",
    }
    if any(payload[key] != value for key, value in expected.items()):
        raise ValueError("corpus catalog identity differs from the frozen profile")
    validate_corpus_news_count(payload["corpus_count"])
    if payload["metric_data_synthetic"] is not True or payload["category_data_synthetic"] is not True:
        raise ValueError("corpus catalog must label metrics and categories synthetic")
    selection = payload["selection"]
    _keys(selection, _SELECTION_KEYS, "selection")
    if (selection["source_cache_dir"] != _CACHE_DIR or
            selection["bge_corpus_path"] != _BGE_CORPUS_PATH or
            type(selection["minimum_body_chars"]) is not int or
            selection["minimum_body_chars"] != MIN_BODY_CHARS or
            selection["reviewed_metadata_exclusion_ids"] != list(_METADATA_EXCLUSIONS)):
        raise ValueError("corpus catalog selection differs from the frozen policy")
    for field in ("cache_article_count", "bge_article_count", "bge_chunk_count",
                  "intersection_count", "eligible_count"):
        if type(selection[field]) is not int or selection[field] < CORPUS_COUNT:
            raise ValueError("invalid corpus selection count")
    if not (selection["eligible_count"] <= selection["intersection_count"] <=
            min(selection["cache_article_count"], selection["bge_article_count"]) <=
            selection["bge_chunk_count"]):
        raise ValueError("inconsistent corpus selection counts")
    articles = payload["articles"]
    if not isinstance(articles, list) or len(articles) != CORPUS_COUNT:
        raise ValueError("corpus catalog must contain exactly 120 articles")
    ids = []
    for article in articles:
        _keys(article, _ARTICLE_KEYS, "article")
        news_id = _text(article["news_id"], "news_id", 16)
        if not re.fullmatch(r"[0-9]{8}[A-Z0-9]{8}", news_id) or news_id in _METADATA_EXCLUSIONS:
            raise ValueError("invalid corpus news_id")
        title = _text(article["title"], "title", 1024)
        body = _text(article["content"], "content", MAX_BODY_CHARS)
        if len(body.strip()) < MIN_BODY_CHARS:
            raise ValueError("corpus article body is too short")
        if type(article["content_version"]) is not int or article["content_version"] < 1:
            raise ValueError("invalid corpus content_version")
        url = _text(article["url"], "url", 512)
        parsed_url = urlsplit(url)
        if (any(char.isspace() or ord(char) < 32 for char in url) or
                parsed_url.scheme != "https" or parsed_url.netloc != "news.qq.com" or
                parsed_url.path != f"/rain/a/{news_id}" or parsed_url.query or parsed_url.fragment or
                url != f"https://news.qq.com/rain/a/{news_id}"):
            raise ValueError("corpus URL must match the exact HTTPS news_id")
        published = _cache_timestamp(article["publish_time_raw"], "publish_time_raw")
        if article["publish_time"] != published.isoformat():
            raise ValueError("corpus publish_time differs from cache metadata")
        _cache_timestamp(article["fetched_at"], "fetched_at")
        if (article["content_type"] != "article" or article["source"] != NEWS_SOURCE_LABEL or
                article["category"] != _demo_category(title)):
            raise ValueError("invalid corpus article display metadata")
        if article["source_file"] != f"{_CACHE_DIR}/{news_id}.json":
            raise ValueError("corpus source file differs from news_id")
        if not isinstance(article["source_sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", article["source_sha256"]):
            raise ValueError("invalid corpus source SHA-256")
        ids.append(news_id)
    if ids != sorted(set(ids)):
        raise ValueError("corpus article news_ids must be unique and sorted")
    return payload


def corpus_catalog() -> dict[str, Any]:
    """Read and verify every call; missing/tampered data never falls back to fake content."""
    try:
        with CORPUS_CATALOG_PATH.open("rb") as source:
            encoded = source.read(MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise ValueError("corpus catalog is unavailable") from exc
    if not encoded or len(encoded) > MAX_CATALOG_BYTES:
        raise ValueError("corpus catalog exceeds the bounded size")
    if sha256(encoded).hexdigest() != CORPUS_CATALOG_SHA256:
        raise ValueError("corpus catalog SHA-256 differs from the frozen profile")
    try:
        payload = json.loads(encoded, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid corpus catalog JSON") from exc
    return _validate_catalog(payload)


def corpus_article(news_id: str) -> dict[str, Any] | None:
    if not isinstance(news_id, str):
        raise ValueError("corpus news_id must be a string")
    return next((article for article in corpus_catalog()["articles"]
                 if article["news_id"] == news_id), None)


def corpus_news_ids(news_per_tenant: int = CORPUS_COUNT) -> tuple[str, ...]:
    validate_corpus_news_count(news_per_tenant)
    return tuple(article["news_id"] for article in corpus_catalog()["articles"])


def iter_corpus_rows(table: str, news_per_tenant: int = CORPUS_COUNT) -> Iterator[dict[str, Any]]:
    count = validate_corpus_news_count(news_per_tenant)
    if table not in TABLES:
        raise ValueError("unsupported corpus table")
    articles = corpus_catalog()["articles"]
    if table == "dim_news":
        for tenant_id in TENANTS:
            for article in articles:
                yield {"tenant_id": tenant_id, "news_id": article["news_id"], "title": article["title"],
                       "content_type": "article", "category": article["category"],
                       "source": NEWS_SOURCE_LABEL,
                       "publish_time": datetime.fromisoformat(article["publish_time"])}
        return
    identities = {f"scale-news-{index:06d}": article["news_id"]
                  for index, article in enumerate(articles, 1)}
    for row in iter_timeline_rows(table, count):
        row["news_id"] = identities[row["news_id"]]
        yield row


@lru_cache(maxsize=1)
def _cached_manifest(catalog_sha256: str) -> dict[str, Any]:
    hashes = {}
    for table in TABLES:
        hasher = ScaledTableHasher(table)
        for row in iter_corpus_rows(table, CORPUS_COUNT):
            hasher.update(row)
        hashes[table] = {"sha256": hasher.digest(), "rows": hasher.count}
    identity = {
        "dataset_profile": CORPUS_PROFILE, "dataset_version": CORPUS_DATASET_VERSION,
        "news_per_tenant": CORPUS_COUNT, "hours_per_news": HOURS, "total_tenants": len(TENANTS),
        "window_start": START.isoformat(), "window_end": END.isoformat(),
        "hash_protocol": "news-scaled-fixture-v1", "tables": hashes,
        "enterprise_scenarios": timeline_scenarios(), "corpus_catalog_sha256": catalog_sha256,
        "news_content_source": NEWS_CONTENT_SOURCE, "metric_data_synthetic": True,
        "metric_data_source": "iter_timeline_rows(table, 120); deterministic synthetic hourly aggregates",
        "category_data_synthetic": True, "category_rule_version": "title-keyword-demo-v1",
        "category_description": "演示板块分类，按标题关键词确定；不是新闻原始栏目。",
        "corpus_count": CORPUS_COUNT, "bge_manifest_digest": BGE_MANIFEST_DIGEST,
        "bge_chunks_sha256": BGE_CHUNKS_SHA256,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    return {**identity, "dataset_sha256": sha256(encoded).hexdigest()}


def corpus_manifest(news_per_tenant: int = CORPUS_COUNT) -> dict[str, Any]:
    validate_corpus_news_count(news_per_tenant)
    # Even a cached identity must not conceal subsequent catalog removal/tampering.
    corpus_catalog()
    return deepcopy(_cached_manifest(CORPUS_CATALOG_SHA256))
