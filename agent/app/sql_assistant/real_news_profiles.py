"""Agent-facing profile for the real-content v8 warehouse.

Content comes from the frozen catalog plus the v2 real-article extension.
Behavior rows exist only for articles with approved v8 metrics; the 7,459
content-only articles are deliberately absent from the ranking view. No value
is fabricated for an article that has no approved behavior evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from app.sql_assistant.scaled_profiles import ScaledTableHasher, TABLES, TENANTS
from simulation.warehouse_simulation.real_news_catalog import (
    EXTENSION_SHA256, EXTENSION_VERSION, load_catalog, load_extension,
)

REAL_NEWS_PROFILE = "real-news-v8"
REAL_NEWS_DATASET_VERSION = "real-news-warehouse-v8-20261014-extension-v2"
REAL_NEWS_COUNT = 8120
REAL_NEWS_WINDOW_START = datetime(2026, 9, 30, tzinfo=timezone(timedelta(hours=8)))
REAL_NEWS_WINDOW_END = datetime(2026, 10, 14, 20, tzinfo=timezone(timedelta(hours=8)))
REAL_NEWS_SOURCE_LABEL = "腾讯新闻公开页面缓存"
REAL_NEWS_CATEGORIES = ("科技", "财经", "体育", "社会", "汽车", "教育", "娱乐")
_DEFAULT_DATASET = Path(__file__).resolve().parents[3] / "var/warehouse/real-news-v8-20261014"
_MEASURE_FIELDS = ("impressions", "clicks", "unique_users", "total_duration_seconds",
                   "effective_consumptions", "interactions")


def validate_real_news_count(value: int) -> int:
    if type(value) is not int or value != REAL_NEWS_COUNT:
        raise ValueError("real-news-v8 must contain exactly 8,120 real articles per tenant")
    return value


def real_news_dataset_path() -> Path:
    configured = os.environ.get("REAL_NEWS_WAREHOUSE_DATASET")
    path = Path(configured).expanduser() if configured else _DEFAULT_DATASET
    if not path.is_dir() or path.is_symlink():
        raise ValueError("real-news-v8 warehouse package is unavailable")
    return path


@lru_cache(maxsize=1)
def _articles() -> tuple[dict[str, Any], ...]:
    catalog = load_catalog()
    extension = load_extension()
    if len(catalog) != 661 or len(extension) != 7459:
        raise ValueError("real-news-v8 catalog or extension count changed")
    rows = []
    for article in catalog:
        rows.append({"news_id": article["news_id"], "title": article["title"],
                     "category": article["category"], "publish_time": article["publish_time"],
                     "metric_available": True})
    for article in extension:
        rows.append({"news_id": article["news_id"], "title": article["title"],
                     "category": article["category"], "publish_time": article["publish_time"],
                     "metric_available": False})
    rows.sort(key=lambda item: item["news_id"])
    if len(rows) != REAL_NEWS_COUNT or len({row["news_id"] for row in rows}) != REAL_NEWS_COUNT:
        raise ValueError("real-news-v8 article roster is not unique or complete")
    return tuple(rows)


def real_news_news_ids(news_per_tenant: int = REAL_NEWS_COUNT) -> tuple[str, ...]:
    validate_real_news_count(news_per_tenant)
    return tuple(article["news_id"] for article in _articles())


@lru_cache(maxsize=1)
def _package_identity() -> dict[str, Any]:
    root = real_news_dataset_path()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("real-news-v8 manifest is unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("dataset_version") != "real-news-business-warehouse-v8-20261014"
            or manifest.get("real_article_count") != REAL_NEWS_COUNT
            or manifest.get("catalog_count") != 661
            or manifest.get("extension_count") != 7459
            or manifest.get("metric_article_count") != 661
            or manifest.get("extension_sha256") != EXTENSION_SHA256
            or manifest.get("news_content_synthetic") is not False
            or manifest.get("metric_data_synthetic") is not True):
        raise ValueError("real-news-v8 package identity or provenance changed")
    extension_path = root / manifest["extension_path"]
    if extension_path.is_symlink() or not extension_path.is_file():
        raise ValueError("real-news-v8 extension manifest is unavailable")
    if sha256(extension_path.read_bytes()).hexdigest() != EXTENSION_SHA256:
        raise ValueError("real-news-v8 extension digest changed")
    return manifest


def _valid_hours() -> dict[tuple[str, str], int]:
    root = real_news_dataset_path()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    with (root / "window_snapshot.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            key = (row["tenant_id"], row["event_time"])
            current = latest.get(key)
            if current is None or row["snapshot_revision"] > current["snapshot_revision"]:
                latest[key] = row
    return {key: row["snapshot_revision"] for key, row in latest.items()
            if row["completeness"] in {"complete", "corrected"} and row["quality_status"] == "passed"}


def _iter_metrics(table: str, valid: dict[tuple[str, str], int]) -> Iterator[dict[str, Any]]:
    root = real_news_dataset_path()
    with (root / f"{table}.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            key = (row["tenant_id"], row["event_time"])
            if key not in valid:
                continue
            if table == "news_metric_hourly":
                if row["snapshot_revision"] != valid[key]:
                    continue
                yield {
                    "tenant_id": UUID(row["tenant_id"]), "news_id": row["news_id"],
                    "event_time": datetime.fromisoformat(row["event_time"]),
                    **{name: row[name] for name in _MEASURE_FIELDS if name != "total_duration_seconds"},
                    "total_duration_seconds": Decimal(row["total_duration_seconds"]),
                }
            else:
                if all(row[name] is None for name in (
                        "baseline_impressions", "baseline_clicks",
                        "baseline_effective_consumptions", "baseline_interactions")):
                    continue
                yield {
                    "tenant_id": UUID(row["tenant_id"]), "news_id": row["news_id"],
                    "event_time": datetime.fromisoformat(row["event_time"]),
                    **{name: Decimal(row[name]) for name in (
                        "baseline_impressions", "baseline_clicks",
                        "baseline_effective_consumptions", "baseline_interactions")},
                }


def iter_real_news_rows(table: str, news_per_tenant: int = REAL_NEWS_COUNT) -> Iterator[dict[str, Any]]:
    validate_real_news_count(news_per_tenant)
    if table not in TABLES:
        raise ValueError("unsupported real-news-v8 fixture table")
    if table == "dim_news":
        for tenant in TENANTS:
            for article in _articles():
                yield {
                    "tenant_id": tenant, "news_id": article["news_id"],
                    "title": article["title"], "content_type": "article",
                    "category": article["category"], "source": REAL_NEWS_SOURCE_LABEL,
                    "publish_time": datetime.fromisoformat(article["publish_time"]),
                }
        return
    valid = _valid_hours()
    yield from _iter_metrics(table, valid)


@lru_cache(maxsize=1)
def _cached_manifest() -> dict[str, Any]:
    _package_identity()
    hashes = {}
    for table in TABLES:
        hasher = ScaledTableHasher(table)
        for row in iter_real_news_rows(table):
            hasher.update(row)
        hashes[table] = {"sha256": hasher.digest(), "rows": hasher.count}
    identity = {
        "dataset_profile": REAL_NEWS_PROFILE,
        "dataset_version": REAL_NEWS_DATASET_VERSION,
        "news_per_tenant": REAL_NEWS_COUNT,
        "total_tenants": len(TENANTS),
        "window_start": REAL_NEWS_WINDOW_START.isoformat(),
        "window_end": REAL_NEWS_WINDOW_END.isoformat(),
        "hash_protocol": "news-scaled-fixture-v1",
        "tables": hashes,
        "catalog_count": 661,
        "extension_count": 7459,
        "extension_sha256": EXTENSION_SHA256,
        "extension_version": EXTENSION_VERSION,
        "news_content_synthetic": False,
        "metric_data_synthetic": True,
        "metric_available_article_count": 661,
        "content_only_article_count": 7459,
        "enterprise_scenarios": [],
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    return {**identity, "dataset_sha256": sha256(encoded).hexdigest()}


def real_news_manifest(news_per_tenant: int = REAL_NEWS_COUNT) -> dict[str, Any]:
    validate_real_news_count(news_per_tenant)
    real_news_dataset_path()
    return deepcopy(_cached_manifest())
