#!/usr/bin/env python3
"""Stream-verify an exported real-content / simulated-metric warehouse package.

This checks the artifact before a PostgreSQL import. Database constraints and
cross-grain reconciliation are checked separately by the versioned SQL checks.
It reads no credentials and never connects to a database or a vector store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


TABLES = (
    "dim_behavior_policy", "dim_channel", "dim_news", "news_content_feature",
    "window_snapshot", "news_metric_5min", "news_metric_channel_hourly",
    "news_metric_hourly", "news_metric_daily", "news_metric_baseline_hourly",
    "content_change_event", "content_tombstone", "index_cleanup_task",
)
FORBIDDEN_FIELDS = frozenset({
    "user_id", "user_ids", "device_id", "session_id", "session_ids",
    "view_id", "anonymous_id", "audience_intervals", "user_intervals",
})


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, maximum: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"invalid package JSON file: {path.name}")
    result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    if not isinstance(result, dict):
        raise ValueError(f"expected an object: {path.name}")
    return result


def validate_package(directory: Path) -> dict[str, Any]:
    directory = directory.absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("expected a real dataset directory")
    manifest = _read_json(directory / "manifest.json", 1024 * 1024)
    unsigned = dict(manifest)
    expected_dataset_digest = unsigned.pop("dataset_sha256", None)
    actual_dataset_digest = hashlib.sha256(_json(unsigned).encode("utf-8")).hexdigest()
    if expected_dataset_digest != actual_dataset_digest:
        raise ValueError("manifest dataset digest mismatch")
    if manifest.get("schema") != "news_sim_v8" or set(manifest.get("tables", {})) != set(TABLES):
        raise ValueError("unexpected versioned warehouse schema or tables")
    if manifest.get("news_content_synthetic") is not False or manifest.get("metric_data_synthetic") is not True:
        raise ValueError("real-content / simulated-metric provenance is required")
    tenants = manifest.get("tenants")
    if not isinstance(tenants, list) or len(tenants) != 2 or len(set(tenants)) != 2:
        raise ValueError("expected two isolated fixture tenants")
    artifacts = {
        "schema.sql": "schema_sha256", "generator.py": "generator_sha256",
        "catalog.json": "catalog_sha256", "catalog_loader.py": "catalog_loader_sha256",
        "warehouse.sql": "warehouse_sql_sha256",
    }
    if manifest.get("extension_sha256"):
        artifacts[manifest["extension_path"]] = "extension_sha256"
    for name, field in artifacts.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or file_sha256(path) != manifest.get(field):
            raise ValueError(f"artifact digest mismatch: {name}")
    catalog = _read_json(directory / "catalog.json", 16 * 1024 * 1024)
    # A rewritten manifest cannot authorize a different article roster. Bind it
    # to the independently frozen source identity, then verify its full schema.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from simulation.warehouse_simulation.content_features import derive_content_features
    from simulation.warehouse_simulation.real_news_catalog import (
        CATALOG_SHA256, load_catalog, load_extension,
    )

    if manifest["catalog_sha256"] != CATALOG_SHA256:
        raise ValueError("catalog differs from the independently frozen real-news identity")
    load_catalog(directory / "catalog.json")
    articles = catalog.get("articles")
    if not isinstance(articles, list) or not articles:
        raise ValueError("catalog articles are missing")
    by_id: dict[str, dict[str, Any]] = {}
    for article in articles:
        news_id = article.get("news_id")
        if not isinstance(news_id, str) or news_id in by_id:
            raise ValueError("invalid or duplicate real news identity")
        if article.get("news_content_synthetic") is not False:
            raise ValueError("a synthetic article appeared in the real-news catalog")
        if article.get("url") != f"https://news.qq.com/rain/a/{news_id}":
            raise ValueError("article URL does not match its real news identity")
        body = article.get("content")
        if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != article.get("content_sha256"):
            raise ValueError("real article body digest mismatch")
        article["metric_available"] = True
        article["content_features"] = derive_content_features(article["title"], body)
        by_id[news_id] = article
    extension_rows: tuple[dict[str, Any], ...] = ()
    if manifest.get("extension_sha256"):
        if manifest.get("extension_count") != 7459 or manifest.get("metric_article_count") != 661:
            raise ValueError("extension or metric-eligible article count is invalid")
        extension_rows = load_extension(directory / manifest["extension_path"])
        for article in extension_rows:
            news_id = article["news_id"]
            if news_id in by_id:
                raise ValueError("extension duplicates a frozen catalog identity")
            if article.get("metric_available") is not False:
                raise ValueError("extension article unexpectedly claims behavior metrics")
            by_id[news_id] = article
    observed: dict[str, dict[str, Any]] = {}
    dim_identities: set[tuple[str, str]] = set()
    feature_identities: set[tuple[str, str]] = set()
    ineligible = {news_id for news_id, article in by_id.items() if not article.get("metric_available", True)}
    for table in TABLES:
        path = directory / f"{table}.jsonl"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"invalid table artifact: {table}")
        digest, count = hashlib.sha256(), 0
        expected = manifest["tables"][table]
        columns = expected.get("columns")
        if columns is not None and (not isinstance(columns, list) or len(set(columns)) != len(columns)):
            raise ValueError("invalid table column manifest")
        with path.open("rb") as stream:
            for line in stream:
                if len(line) > 1024 * 1024:
                    raise ValueError("table row exceeded its bounded size")
                digest.update(line)
                row = json.loads(line, object_pairs_hook=_strict_object)
                if not isinstance(row, dict) or FORBIDDEN_FIELDS.intersection(row):
                    raise ValueError("raw user or deduplication identities must not be persisted")
                if columns is not None and set(row) != set(columns):
                    raise ValueError("table columns differ from the frozen manifest")
                if row.get("tenant_id") not in tenants:
                    raise ValueError("unknown fixture tenant")
                if "news_id" in row and row["news_id"] not in by_id:
                    raise ValueError("a table contains an unknown real-news identity")
                if table.startswith("news_metric_") and row["news_id"] in ineligible:
                    raise ValueError("a content-only article has behavior metric rows")
                if table == "dim_news":
                    identity = (row["tenant_id"], row["news_id"])
                    if identity in dim_identities:
                        raise ValueError("duplicate news dimension identity")
                    dim_identities.add(identity)
                    source = by_id[row["news_id"]]
                    for field in ("title", "publish_time", "content_sha256"):
                        if row.get(field) != source.get(field):
                            raise ValueError(f"news dimension changed original {field}")
                    if row.get("source_url") != source["url"]:
                        raise ValueError("news dimension changed original URL")
                    if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row.get("content_sha256"):
                        raise ValueError("news dimension body digest mismatch")
                    if source.get("content") is not None and row.get("content") != source.get("content"):
                        raise ValueError("news dimension changed original body")
                    if row.get("news_content_synthetic") is not False:
                        raise ValueError("synthetic content entered the news dimension")
                    if row.get("metric_available") is not source.get("metric_available", True):
                        raise ValueError("news dimension metric availability differs from its source")
                if table == "news_content_feature":
                    identity = (row["tenant_id"], row["news_id"])
                    if identity in feature_identities:
                        raise ValueError("duplicate content feature identity")
                    feature_identities.add(identity)
                    source = by_id[row["news_id"]]
                    features = source.get("content_features") or {
                        key: source[key] for key in (
                            "title_chars", "content_chars", "paragraph_count", "sentence_count",
                            "digit_token_count", "keyword_top")
                    }
                    for field in ("title_chars", "content_chars", "paragraph_count", "sentence_count",
                                  "digit_token_count", "keyword_top"):
                        if row.get(field) != features[field]:
                            raise ValueError(f"content feature {field} differs from its source")
                    if row.get("content_sha256") != source.get("content_sha256"):
                        raise ValueError("content feature body digest differs from its source")
                    if row.get("metric_available") is not source.get("metric_available", True):
                        raise ValueError("content feature metric availability differs from its source")
                if table == "index_cleanup_task" and row.get("status") != "pending":
                    raise ValueError("fixture cleanup tasks must remain pending")
                count += 1
        actual = {"rows": count, "sha256": digest.hexdigest()}
        if any(actual[key] != expected.get(key) for key in ("rows", "sha256")):
            raise ValueError(f"table count or digest mismatch: {table}")
        observed[table] = actual
    expected_identities = {(tenant, news_id) for tenant in tenants for news_id in by_id}
    if dim_identities != expected_identities:
        raise ValueError("news dimensions do not contain the complete corpus in both tenants")
    if feature_identities != expected_identities:
        raise ValueError("content features do not contain the complete corpus in both tenants")
    return {
        "status": "passed", "validation_kind": "streamed_artifact_integrity",
        "dataset_version": manifest["dataset_version"], "dataset_sha256": actual_dataset_digest,
        "news_content_synthetic": False, "metric_data_synthetic": True,
        "distinct_real_articles": len(by_id), "tenants": tenants,
        "extension_count": len(extension_rows), "metric_article_count": len(by_id) - len(extension_rows),
        "total_persisted_rows": sum(table["rows"] for table in observed.values()),
        "tables": observed,
        "database_checks": "separate_versioned_sql_checks_required",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--report", type=Path, help="New integrity report; will not overwrite")
    args = parser.parse_args()
    try:
        result = validate_package(args.dataset)
        if args.report:
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Validation failed: {exc}\n")
    print(json.dumps({key: value for key, value in result.items() if key != "tables"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
