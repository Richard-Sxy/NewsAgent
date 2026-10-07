"""Versioned public headline references with isolated synthetic hourly metrics.

Only a frozen local catalog is accepted. No runtime web requests or fabricated
article body is involved; publication dates and simulated metric dates differ.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterator
from urllib.parse import urlparse

from app.sql_assistant.scaled_profiles import (
    HOURS_PER_NEWS, START, TABLES, TENANTS, ScaledTableHasher,
    _metrics, _normal, scaled_scenarios,
)

PUBLIC_HEADLINES_PROFILE = "public-headlines-v3"
PUBLIC_DATASET_VERSION = "qq-public-headlines-2026-10-07-v1"
CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "public-headlines" / "2026-10-07" / "catalog.json"
CATALOG_SHA256 = "639f81bc3939b9508a248e897d586dc1b6ef6cf8aa2f7502401ccea12ad6b998"
ALLOWED_PUBLIC_NEWS_COUNTS = (120, 1200)
_ARTICLE_KEYS = {"news_id", "title", "source", "url", "published_at", "category", "content_type"}


def validate_public_news_count(value: int) -> int:
    if type(value) is not int or value not in ALLOWED_PUBLIC_NEWS_COUNTS:
        raise ValueError("public headline count must be 120 or 1200 per tenant")
    return value


def _aware(value: str) -> datetime:
    if type(value) is not str:
        raise ValueError("public catalog date must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("public catalog date must include timezone")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("public catalog JSON contains duplicate object fields")
        result[key] = value
    return result


@lru_cache(maxsize=2)
def _parse_catalog(contents: bytes, expected_sha256: str) -> dict[str, Any]:
    if sha256(contents).hexdigest() != expected_sha256:
        raise ValueError("public headline catalog SHA-256 mismatch; use a new version")
    catalog = json.loads(contents, object_pairs_hook=_unique_object)
    if type(catalog) is not dict or set(catalog) != {"catalog_version", "collected_at", "articles"}:
        raise ValueError("public headline catalog fields are invalid")
    if catalog["catalog_version"] != PUBLIC_DATASET_VERSION:
        raise ValueError("public headline catalog version mismatch")
    _aware(catalog["collected_at"])
    articles = catalog["articles"]
    if type(articles) is not list or not 120 <= len(articles) <= 1200:
        raise ValueError("public headline catalog must contain 120 to 1200 articles")
    identifiers, titles = [], set()
    for article in articles:
        if type(article) is not dict or set(article) != _ARTICLE_KEYS:
            raise ValueError("public article fields are invalid")
        for key in ("news_id", "title", "source", "url"):
            value = article[key]
            maximum = 500 if key == "title" else 2048 if key == "url" else 200
            if type(value) is not str or not value.strip() or value != value.strip() or len(value) > maximum:
                raise ValueError(f"public article {key} is invalid")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"public article {key} contains control characters")
        identifier = article["news_id"]
        if not re.fullmatch(r"[0-9]{8}[A-Z][0-9A-Z]{5,20}", identifier):
            raise ValueError("public article must use its original QQ article ID")
        parsed = urlparse(article["url"])
        if (parsed.scheme != "https" or parsed.netloc != "news.qq.com"
                or parsed.path != f"/rain/a/{identifier}" or parsed.query or parsed.fragment):
            raise ValueError("public article URL must match its QQ article ID")
        if type(article["category"]) is not str or article["category"] not in {"科技", "财经", "体育", "社会"}:
            raise ValueError("public article category is outside the approved schema")
        if type(article["content_type"]) is not str or article["content_type"] not in {"article", "video"}:
            raise ValueError("public article content type is outside the approved schema")
        _aware(article["published_at"])
        normalized_title = " ".join(article["title"].split()).casefold()
        if normalized_title in titles:
            raise ValueError("public headline catalog contains duplicate titles")
        titles.add(normalized_title)
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers) or identifiers != sorted(identifiers):
        raise ValueError("public headline catalog IDs must be unique and sorted")
    return catalog


def public_catalog() -> dict[str, Any]:
    try:
        if not CATALOG_PATH.is_file() or CATALOG_PATH.stat().st_size > 4_000_000:
            raise ValueError("frozen public headline catalog is unavailable or oversized")
        contents = CATALOG_PATH.read_bytes()
    except OSError as exc:
        raise ValueError("frozen public headline catalog is unavailable") from exc
    return deepcopy(_parse_catalog(contents, CATALOG_SHA256))


def _selected(count: int) -> list[dict[str, Any]]:
    validate_public_news_count(count)
    articles = public_catalog()["articles"]
    if len(articles) < count:
        raise ValueError("frozen public headline catalog is smaller than the configured dataset")
    return articles[:count]


def public_news_ids(news_per_tenant: int = 1200) -> tuple[str, ...]:
    return tuple(article["news_id"] for article in _selected(news_per_tenant))


def public_article(news_id: str) -> dict[str, Any]:
    for article in public_catalog()["articles"]:
        if article["news_id"] == news_id:
            return article
    raise ValueError("public headline ID is absent from its frozen catalog")


def iter_public_rows(table: str, news_per_tenant: int = 1200) -> Iterator[dict[str, Any]]:
    articles = _selected(news_per_tenant)
    if table not in TABLES:
        raise ValueError("unsupported public headline fixture table")
    for tenant_index, tenant_id in enumerate(TENANTS):
        multiplier = 1 if tenant_index == 0 else 8
        for index, article in enumerate(articles, start=1):
            identity = {"tenant_id": tenant_id, "news_id": article["news_id"]}
            if table == "dim_news":
                yield {**identity, "title": article["title"], "content_type": article["content_type"],
                       "category": article["category"], "source": article["source"],
                       "publish_time": _aware(article["published_at"])}
                continue
            for hour in range(HOURS_PER_NEWS):
                row = {**identity, "event_time": START + timedelta(hours=hour)}
                if table == "news_metric_hourly":
                    values = _metrics(index, hour, news_per_tenant, content_type=article["content_type"])
                    row.update({key: value * multiplier for key, value in values.items()})
                    row["total_duration_seconds"] = Decimal(row["total_duration_seconds"])
                else:
                    normal = _normal(index)
                    row.update({f"baseline_{key}": Decimal(0 if hour in {12, 13} else normal[key] * multiplier)
                                for key in ("impressions", "clicks", "effective_consumptions", "interactions")})
                yield row


@lru_cache(maxsize=4)
def _cached_manifest(news_per_tenant: int, catalog_sha256: str) -> dict[str, Any]:
    hashes = {}
    for table in TABLES:
        hasher = ScaledTableHasher(table)
        for row in iter_public_rows(table, news_per_tenant):
            hasher.update(row)
        hashes[table] = {"sha256": hasher.digest(), "rows": hasher.count}
    identity = {"dataset_profile": PUBLIC_HEADLINES_PROFILE, "dataset_version": PUBLIC_DATASET_VERSION,
                "catalog_sha256": catalog_sha256, "news_per_tenant": news_per_tenant,
                "hours_per_news": HOURS_PER_NEWS, "total_tenants": len(TENANTS),
                "hash_protocol": "news-scaled-fixture-v1", "tables": hashes,
                "enterprise_scenarios": scaled_scenarios()}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {**identity, "dataset_sha256": sha256(encoded).hexdigest()}


def public_manifest(news_per_tenant: int = 1200) -> dict[str, Any]:
    # Verify file even on a manifest cache hit, so a runtime edit never passes.
    _selected(news_per_tenant)
    return deepcopy(_cached_manifest(news_per_tenant, CATALOG_SHA256))
