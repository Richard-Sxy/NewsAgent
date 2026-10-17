"""Bounded, hash-verified loader for the v8 aggregate warehouse.

The input has news/hour aggregates, not reader identities.  Each returned
observation keeps its own UV; this module never adds UV across any boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
from typing import Any, Iterator
from uuid import UUID
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
MAX_MANIFEST_BYTES = 1_048_576
MAX_FILE_BYTES = 268_435_456
MAX_LINE_BYTES = 2_097_152
MAX_TABLE_ROWS = 500_000
_COUNTS = tuple("impressions clicks detail_page_views direct_page_views total_duration_seconds "
                "effective_consumptions interactions likes comments shares favorites negative_feedback "
                "unique_users detail_visitors".split())
_BASES = tuple("baseline_impressions baseline_clicks baseline_effective_consumptions baseline_interactions".split())
_COLUMNS = {
    "dim_news": set("tenant_id news_id title content source_url content_type category category_data_synthetic "
        "source publish_time publish_time_raw source_content_version content_version fixture_content_version "
        "content_sha256 source_file source_sha256 source_package source_manifest_sha256 source_html_sha256 "
        "news_content_synthetic metric_data_synthetic lifecycle_data_synthetic lifecycle_scenario status "
        "lifecycle_revision status_effective_at".split()),
    "window_snapshot": set("tenant_id event_time snapshot_revision window_end watermark_at snapshot_at "
        "completeness quality_status policy_version metric_data_synthetic".split()),
    "news_metric_hourly": set("tenant_id news_id event_time snapshot_revision uv_method policy_version".split()) | set(_COUNTS),
    "news_metric_baseline_hourly": set("tenant_id news_id event_time sample_count baseline_version".split()) | set(_BASES),
    "content_tombstone": set("tenant_id news_id event_id blocked_through_version lifecycle_revision status "
        "effective_at lifecycle_data_synthetic".split()),
}


class WarehouseSourceError(ValueError):
    """The frozen source or its aggregate contract failed validation."""


@dataclass(frozen=True)
class WarehouseSource:
    articles: tuple[dict, ...]
    observations: tuple[dict, ...]
    source_identity: dict
    stats: dict
    window_start: datetime
    window_end: datetime
    as_of: datetime


def _fail(message: str) -> None:
    raise WarehouseSourceError(message)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate JSON key")
        value[key] = item
    return value


def _constant(value):
    _fail("non-finite JSON number")


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WarehouseSourceError("invalid source JSON") from exc
    if not isinstance(value, dict):
        _fail("source JSON must be an object")
    return value


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _regular(path: Path, maximum: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WarehouseSourceError("required source artifact is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        _fail("source artifact must be a bounded regular file")


def _time(value: Any, field: str) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(TZ)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WarehouseSourceError(f"{field} must be a timezone-aware ISO timestamp") from exc


def _hour(value, field):
    result = _time(value, field)
    if result.minute or result.second or result.microsecond:
        _fail(f"{field} must be hour aligned")
    return result


def _integer(value, field, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"invalid {field}")
    return value


def _text(value, field, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        _fail(f"invalid {field}")
    return value


def _digest(value, field):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail(f"invalid {field} digest")
    return value


def _tenant(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise WarehouseSourceError("tenant_id must be a canonical UUID") from exc
    return value


def _news(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{8}[A-Za-z0-9]{4,32}", value) is None:
        _fail("invalid news_id")
    return value


def _rows(directory: Path, manifest: dict, name: str) -> Iterator[dict]:
    spec = manifest.get("tables", {}).get(name)
    if not isinstance(spec, dict):
        _fail(f"missing table contract: {name}")
    expected = _digest(spec.get("sha256"), name)
    count = _integer(spec.get("rows"), f"{name} row count", maximum=MAX_TABLE_ROWS)
    columns = spec.get("columns")
    if (not isinstance(columns, list) or len(columns) != len(_COLUMNS[name])
            or any(not isinstance(item, str) for item in columns) or set(columns) != _COLUMNS[name]):
        _fail(f"invalid {name} columns")
    path = directory / f"{name}.jsonl"
    _regular(path, MAX_FILE_BYTES)
    digest, actual, total = sha256(), 0, 0
    try:
        with path.open("rb") as stream:
            while raw := stream.readline(MAX_LINE_BYTES + 1):
                total += len(raw)
                actual += 1
                if len(raw) > MAX_LINE_BYTES or total > MAX_FILE_BYTES or actual > MAX_TABLE_ROWS:
                    _fail(f"{name} exceeds its read budget")
                digest.update(raw)
                row = _json(raw)
                if set(row) != _COLUMNS[name]:
                    _fail(f"{name} row differs from its frozen columns")
                yield row
    except OSError as exc:
        raise WarehouseSourceError(f"cannot read {name}") from exc
    if actual != count or digest.hexdigest() != expected:
        _fail(f"{name} row count or SHA-256 mismatch")


def load_warehouse_source(directory: str | Path, *, tenant_ids: tuple[str, ...] | None = None,
                          as_of: datetime | None = None) -> WarehouseSource:
    """Load verified hourly observations, selecting visible revisions before gates.

    ``as_of`` is a frozen visibility cutoff, never later than manifest ``asof``.
    Lifecycle eligibility uses the current dimension and tombstones fail closed;
    this is deliberately not reconstruction of historical publication status.
    """
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    _regular(manifest_path, MAX_MANIFEST_BYTES)
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise WarehouseSourceError("cannot read manifest") from exc
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        _fail("manifest exceeds its read budget")
    manifest = _json(raw_manifest)
    unsigned = dict(manifest)
    expected = _digest(unsigned.pop("dataset_sha256", None), "dataset")
    if sha256(_canonical(unsigned)).hexdigest() != expected:
        _fail("manifest dataset SHA-256 mismatch")
    if (manifest.get("schema") != "news_sim_v8" or manifest.get("metric_data_synthetic") is not True
            or manifest.get("news_content_synthetic") is not False):
        _fail("only the v8 public-content/synthetic-aggregate contract is supported")
    _text(manifest.get("dataset_version"), "dataset_version")
    _digest(manifest.get("catalog_sha256"), "catalog")
    declared = manifest.get("tenants")
    if not isinstance(declared, list) or not 1 <= len(declared) <= 16:
        _fail("invalid tenant directory")
    all_tenants = tuple(_tenant(item) for item in declared)
    if len(set(all_tenants)) != len(all_tenants):
        _fail("duplicate manifest tenant")
    if tenant_ids is not None and (not isinstance(tenant_ids, tuple) or not tenant_ids):
        _fail("tenant_ids must be a nonempty tuple")
    selected = all_tenants if tenant_ids is None else tuple(_tenant(item) for item in tenant_ids)
    if len(set(selected)) != len(selected) or not set(selected) <= set(all_tenants):
        _fail("unknown or duplicate selected tenant")
    start = _hour(manifest.get("closed_window_start"), "closed_window_start")
    frozen_end = _hour(manifest.get("closed_window_end"), "closed_window_end")
    frozen_asof = _time(manifest.get("asof"), "asof")
    if not start < frozen_end <= frozen_asof or frozen_end - start > timedelta(days=62):
        _fail("invalid frozen coverage")
    cutoff = frozen_asof if as_of is None else _time(as_of, "as_of")
    if not start < cutoff <= frozen_asof:
        _fail("as_of must be inside frozen visibility coverage")
    end = min(frozen_end, cutoff.replace(minute=0, second=0, microsecond=0))
    if end <= start:
        _fail("as_of contains no closed hour")
    provenance = manifest.get("metric_provenance")
    if not isinstance(provenance, dict):
        _fail("missing metric provenance")
    policy = _text(provenance.get("policy_version"), "policy_version")
    uv = _text(provenance.get("uv_method"), "uv_method")
    if provenance.get("event_storage") != "aggregate only; temporary audience intervals never saved":
        _fail("source must contain aggregate-only behavior")
    loaded = {}
    articles, published = {}, {}

    def identity(row):
        tenant = _tenant(row["tenant_id"])
        if tenant not in all_tenants:
            _fail("row tenant is absent from the manifest")
        return tenant, _news(row["news_id"])

    for row in _rows(directory, manifest, "dim_news"):
        key = identity(row)
        if key in articles:
            _fail("duplicate news dimension ID")
        _text(row["title"], "title", 1024)
        content = _text(row["content"], "content", 1_000_000)
        if sha256(content.encode()).hexdigest() != _digest(row["content_sha256"], "content"):
            _fail("article content SHA-256 mismatch")
        if not _text(row["source_url"], "source_url", 4096).startswith("https://"):
            _fail("public article URL must use HTTPS")
        pub = _time(row["publish_time"], "publish_time")
        effective = _time(row["status_effective_at"], "status_effective_at")
        if not pub <= effective <= frozen_asof:
            _fail("invalid article publication/lifecycle time")
        if (row["content_type"] != "article" or row["news_content_synthetic"] is not False
                or row["metric_data_synthetic"] is not True or row["lifecycle_data_synthetic"] is not True
                or type(row["category_data_synthetic"]) is not bool
                or row["status"] not in {"published", "withdrawn", "restricted"}):
            _fail("article type, lifecycle or provenance mismatch")
        source_version = _integer(row["source_content_version"], "source_content_version", 1)
        if _integer(row["content_version"], "content_version", 1) != source_version:
            _fail("source and content versions differ")
        _integer(row["fixture_content_version"], "fixture_content_version", source_version)
        _integer(row["lifecycle_revision"], "lifecycle_revision", 1)
        articles[key], published[key] = row, pub
    loaded["dim_news"] = len(articles)

    tombstones = set()
    tombstone_details = {}
    for row in _rows(directory, manifest, "content_tombstone"):
        key = identity(row)
        if key not in articles or key in tombstones:
            _fail("unknown or duplicate tombstone news ID")
        article = articles[key]
        _digest(row["event_id"], "tombstone event_id")
        _integer(row["blocked_through_version"], "blocked_through_version", 1, article["fixture_content_version"])
        _integer(row["lifecycle_revision"], "tombstone lifecycle_revision", 1)
        stamp = _time(row["effective_at"], "tombstone effective_at")
        if (not published[key] <= stamp <= frozen_asof or row["status"] not in {"withdrawn", "restricted"}
                or row["lifecycle_data_synthetic"] is not True):
            _fail("invalid tombstone lifecycle")
        tombstones.add(key)
        tombstone_details[key] = row
    loaded["content_tombstone"] = len(tombstones)

    snapshots, latest = {}, {}
    for row in _rows(directory, manifest, "window_snapshot"):
        tenant = _tenant(row["tenant_id"])
        stamp = _hour(row["event_time"], "snapshot event_time")
        revision = _integer(row["snapshot_revision"], "snapshot_revision", 1)
        key = tenant, stamp, revision
        window_end = _hour(row["window_end"], "snapshot window_end")
        watermark = _time(row["watermark_at"], "watermark_at")
        snapshot_at = _time(row["snapshot_at"], "snapshot_at")
        if key in snapshots:
            _fail("duplicate snapshot revision")
        if (tenant not in all_tenants or not start <= stamp <= frozen_end
                or window_end != stamp + timedelta(hours=1)
                or not stamp <= watermark <= snapshot_at <= frozen_asof
                or row["policy_version"] != policy or row["metric_data_synthetic"] is not True
                or row["completeness"] not in {"complete", "corrected", "incomplete"}
                or row["quality_status"] not in {"passed", "pending", "failed"}):
            _fail("snapshot time, policy or quality contract mismatch")
        snapshots[key] = row
        window_key = tenant, stamp
        if snapshot_at <= cutoff and (window_key not in latest or revision > latest[window_key]["snapshot_revision"]):
            latest[window_key] = row
    loaded["window_snapshot"] = len(snapshots)
    relevant = {key: row for key, row in latest.items() if key[0] in selected and start <= key[1] < end}
    qualified = {key: row for key, row in relevant.items()
                 if row["completeness"] in {"complete", "corrected"} and row["quality_status"] == "passed"
                 and _time(row["watermark_at"], "watermark_at") >= _time(row["window_end"], "window_end")}

    seen_metrics, observations = set(), {}
    excluded_lifecycle = 0
    for row in _rows(directory, manifest, "news_metric_hourly"):
        article_key = identity(row)
        stamp = _hour(row["event_time"], "metric event_time")
        revision = _integer(row["snapshot_revision"], "metric snapshot_revision", 1)
        key = *article_key, stamp, revision
        snapshot_key = article_key[0], stamp, revision
        if key in seen_metrics:
            _fail("duplicate hourly metric ID/revision")
        seen_metrics.add(key)
        if article_key not in articles or snapshot_key not in snapshots:
            _fail("hourly metric has an unknown news ID or snapshot revision")
        if (stamp + timedelta(hours=1) <= published[article_key]
                or row["policy_version"] != policy or row["uv_method"] != uv):
            _fail("metric publication time, policy or UV method mismatch")
        for field in _COUNTS:
            _integer(row[field], field)
        if (row["clicks"] > row["impressions"] or row["unique_users"] > row["clicks"]
                or row["detail_visitors"] > row["detail_page_views"]
                or row["direct_page_views"] > row["detail_page_views"]
                or row["effective_consumptions"] > row["clicks"]
                or row["interactions"] != sum(row[field] for field in ("likes", "comments", "shares", "favorites"))):
            _fail("hourly metric count relationships are invalid")
        window = qualified.get((article_key[0], stamp))
        if window is None or revision != window["snapshot_revision"]:
            continue
        article = articles[article_key]
        if article["status"] != "published" or article_key in tombstones or published[article_key] > cutoff:
            excluded_lifecycle += 1
            continue
        observations[(*article_key, stamp)] = {**row, "event_time": stamp.isoformat(),
            "window_start": stamp.isoformat(), "window_end": window["window_end"],
            **{field: window[field] for field in ("watermark_at", "snapshot_at", "completeness", "quality_status")},
            "sample_count": None, "baseline_version": None, **dict.fromkeys(_BASES),
            "ctr": row["clicks"] / row["impressions"] if row["impressions"] else None,
            "source_content_version": article["source_content_version"], "content_sha256": article["content_sha256"]}
    loaded["news_metric_hourly"] = len(seen_metrics)

    seen_baselines, baseline_versions = set(), set()
    snapshot_windows = {key[:2] for key in snapshots}
    for row in _rows(directory, manifest, "news_metric_baseline_hourly"):
        article_key = identity(row)
        stamp = _hour(row["event_time"], "baseline event_time")
        key = *article_key, stamp
        if key in seen_baselines or article_key not in articles or not start <= stamp <= frozen_end:
            _fail("duplicate, unknown or out-of-window baseline ID")
        seen_baselines.add(key)
        if stamp + timedelta(hours=1) <= published[article_key]:
            _fail("baseline precedes article publication")
        # A historical cutoff may precede the visibility of a valid frozen row.
        if (article_key[0], stamp) not in snapshot_windows:
            _fail("baseline has no corresponding publication window")
        samples = _integer(row["sample_count"], "sample_count", 0, 3)
        baseline_versions.add(_text(row["baseline_version"], "baseline_version"))
        if len(baseline_versions) > 1:
            _fail("baseline versions differ within the frozen source")
        for field in _BASES:
            value = row[field]
            if samples == 0:
                if value is not None:
                    _fail("zero-sample baseline must remain null")
                continue
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise WarehouseSourceError("invalid baseline number") from exc
            if isinstance(value, bool) or not number.is_finite() or number < 0 or number > 2**63 - 1:
                _fail("invalid baseline number")
        if key in observations:
            observations[key].update({field: row[field] for field in ("sample_count", "baseline_version", *_BASES)})
    loaded["news_metric_baseline_hourly"] = len(seen_baselines)
    eligible = tuple(row for key, row in sorted(articles.items())
                     if key[0] in selected and row["status"] == "published" and key not in tombstones
                     and published[key] <= cutoff)
    identity_out = {
        "source_schema": "news_sim_v8",
        "dataset_version": manifest["dataset_version"], "dataset_sha256": expected,
        "manifest_sha256": sha256(raw_manifest).hexdigest(), "catalog_sha256": manifest["catalog_sha256"],
        "input_file_sha256": {f"{name}.jsonl": manifest["tables"][name]["sha256"] for name in _COLUMNS},
        "policy_version": policy, "uv_method": uv,
        "baseline_version": next(iter(baseline_versions), None),
        "metric_data_synthetic": True, "news_content_synthetic": False, "tenant_ids": list(selected),
        "uv_aggregation": "per-news-per-hour only; never summed",
        "as_of": cutoff.isoformat(),
        "blocked_news_identities": [
            {"tenant_id": key[0], "news_id": key[1],
             "status": tombstone_details.get(key, row)["status"],
             "lifecycle_revision": max(row["lifecycle_revision"],
                 tombstone_details.get(key, row)["lifecycle_revision"]),
             "reason": "content_tombstone" if key in tombstones else "current_lifecycle_status"}
            for key, row in sorted(articles.items())
            if key[0] in selected and (key in tombstones or row["status"] != "published")],
    }
    stats = {"loaded_rows": loaded, "selected_articles": len(eligible), "selected_observations": len(observations),
        "latest_windows": len(relevant), "qualified_windows": len(qualified),
        "excluded_windows": len(relevant) - len(qualified),
        "excluded_lifecycle_observations": excluded_lifecycle,
        "not_historical_lifecycle_reconstruction": True}
    return WarehouseSource(eligible, tuple(observations[key] for key in sorted(observations)),
                           identity_out, stats, start, end, cutoff)
