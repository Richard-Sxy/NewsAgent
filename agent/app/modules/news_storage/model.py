"""Deterministic, versioned content releases. No Settings, network or models."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlparse


SCHEMA_VERSION = "unified-news-content-v1"
SELECTION_POLICY = "prefer-cached-body-source-priority-v1"
STATES = {"body_available": 0, "fragments_only": 1, "title_only": 2}
RECORD_KEYS = {"news_id", "title", "body", "url", "publish_time", "content_type",
               "content_state", "fragments", "quality_flags", "body_sha256", "record_sha256"}


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("publication timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat()


def _text(value, name, maximum, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError(f"invalid {name}")
    return value


def normalize_record(candidate: dict) -> dict:
    row = {key: candidate.get(key) for key in (
        "news_id", "title", "body", "url", "publish_time", "content_type", "content_state")}
    _text(row["news_id"], "news_id", 160)
    _text(row["title"], "title", 2000, nullable=True)
    _text(row["body"], "body", 2_000_000, nullable=True)
    _text(row["url"], "url", 4096, nullable=True)
    if row["url"] is not None:
        url = urlparse(row["url"])
        if url.scheme != "https" or not url.netloc or url.username or url.password:
            raise ValueError("source URL must be credential-free HTTPS")
    row["publish_time"] = timestamp(row["publish_time"])
    if row["content_type"] not in {"article", "video"} or row["content_state"] not in STATES:
        raise ValueError("invalid content type or completeness state")
    fragments = candidate.get("fragments", [])
    if not isinstance(fragments, list) or len(fragments) > 10000:
        raise ValueError("invalid fragments")
    for part in fragments:
        if not isinstance(part, dict) or not {"chunk_index", "text"} <= part.keys():
            raise ValueError("invalid fragment fields")
        if type(part["chunk_index"]) is not int or part["chunk_index"] < 0:
            raise ValueError("invalid fragment index")
        _text(part["text"], "fragment text", 2_000_000)
    row["fragments"] = json.loads(canonical_json(fragments))
    if row["content_state"] == "body_available" and row["body"] is None:
        raise ValueError("body_available needs captured body")
    if row["content_state"] != "body_available" and row["body"] is not None:
        raise ValueError("partial evidence cannot masquerade as a body")
    if row["content_state"] == "fragments_only" and not fragments:
        raise ValueError("fragments_only needs fragments")
    if row["content_state"] == "title_only" and not row["title"]:
        raise ValueError("title_only needs title")
    flags = candidate.get("quality_flags", [])
    if not isinstance(flags, list) or any(not isinstance(x, str) or not x.strip() for x in flags):
        raise ValueError("invalid quality flags")
    flags = set(flags)
    for key in ("title", "url", "publish_time"):
        if row[key] is None:
            flags.add(f"missing_{key}")
    if row["body"] is not None and len(row["body"].strip()) < 100:
        flags.add("short_body_lt_100")
    row["quality_flags"] = sorted(flags)
    row["body_sha256"] = sha256(row["body"].encode("utf-8")).hexdigest() if row["body"] is not None else None
    row["record_sha256"] = digest(row)
    return row


def _validate_source(source):
    if not isinstance(source, dict):
        raise ValueError("source provenance must be an object")
    for key in ("package", "path", "file_sha256"):
        _text(source.get(key), f"source {key}", 4096)
    if not re.fullmatch(r"[0-9a-f]{64}", source["file_sha256"]):
        raise ValueError("invalid source file SHA-256")
    for key in ("path", "manifest_path"):
        value = source.get(key)
        if value is not None and (PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts):
            raise ValueError("source paths must be repository-relative")
    if source.get("manifest_sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", source["manifest_sha256"]):
        raise ValueError("invalid source manifest SHA-256")
    canonical_json(source)


def _identity(package):
    manifest = package["manifest"]
    return {
        "tenant_id": manifest["tenant_id"], "schema_version": manifest["schema_version"],
        "selection_policy": manifest["selection_policy"], "versions": package["versions"],
        "provenance": package["provenance"], "items": package["items"],
    }


def _counts(package):
    by_key = {(r["news_id"], r["record_sha256"]): r for r in package["versions"]}
    selected = [by_key[(r["news_id"], r["record_sha256"])] for r in package["items"]]
    return {
        "news_count": len(selected), "version_count": len(package["versions"]),
        "provenance_count": len(package["provenance"]),
        **{f"{state}_count": sum(r["content_state"] == state for r in selected) for state in STATES},
        "conflicting_body_news_count": sum("conflicting_body_variants" in r["quality_flags"] for r in package["items"]),
    }


def build_package(candidates: list[dict], *, tenant_id: str) -> dict:
    _text(tenant_id, "tenant_id", 128)
    if not candidates:
        raise ValueError("cannot publish an empty content release")
    versions, provenance, choices = {}, {}, defaultdict(list)
    for candidate in candidates:
        record = normalize_record(candidate)
        source = candidate["source"]
        _validate_source(source)
        priority = candidate["priority"]
        if type(priority) is not int or priority < 0:
            raise ValueError("source priority must be a nonnegative integer")
        key = (record["news_id"], record["record_sha256"])
        versions[key] = record
        evidence = {"news_id": key[0], "record_sha256": key[1], "source": dict(source, selection_priority=priority)}
        provenance[digest(evidence)] = evidence
        choices[key[0]].append((STATES[record["content_state"]], priority, key[1]))
    body_hashes = defaultdict(set)
    for record in versions.values():
        if record["body_sha256"]:
            body_hashes[record["news_id"]].add(record["body_sha256"])
    items = []
    for news_id, options in sorted(choices.items()):
        selected_hash = min(options)[2]
        flags = set(versions[(news_id, selected_hash)]["quality_flags"])
        if len(body_hashes[news_id]) > 1:
            flags.add("conflicting_body_variants")
        items.append({"news_id": news_id, "record_sha256": selected_hash, "quality_flags": sorted(flags)})
    package = {
        "manifest": {"tenant_id": tenant_id, "schema_version": SCHEMA_VERSION, "selection_policy": SELECTION_POLICY},
        "versions": [versions[k] for k in sorted(versions)],
        "provenance": sorted(provenance.values(), key=lambda r: (r["news_id"], r["record_sha256"], digest(r["source"]))),
        "items": items,
    }
    identity = digest(_identity(package))
    package["manifest"].update(release_id=f"news-{identity[:24]}", identity_sha256=identity,
                               counts=_counts(package))
    validate_package(package)
    return package


def validate_package(package: dict) -> None:
    if not isinstance(package, dict) or set(package) != {"manifest", "versions", "provenance", "items"}:
        raise ValueError("invalid package fields")
    manifest = package["manifest"]
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("selection_policy") != SELECTION_POLICY:
        raise ValueError("unsupported content release contract")
    _text(manifest.get("tenant_id"), "tenant_id", 128)
    keys = set()
    for row in package["versions"]:
        if set(row) != RECORD_KEYS or normalize_record(row) != row:
            raise ValueError("content record/hash mismatch")
        key = (row["news_id"], row["record_sha256"])
        if key in keys:
            raise ValueError("duplicate content version")
        keys.add(key)
    if not keys:
        raise ValueError("empty content release")
    evidenced, source_keys, choices = set(), set(), defaultdict(list)
    for row in package["provenance"]:
        _validate_source(row["source"])
        key = (row["news_id"], row["record_sha256"])
        if key not in keys or digest(row) in source_keys:
            raise ValueError("invalid or duplicate source binding")
        priority = row["source"].get("selection_priority")
        if type(priority) is not int or priority < 0:
            raise ValueError("source selection priority is required")
        source_keys.add(digest(row)); evidenced.add(key)
        choices[key[0]].append((priority, key[1]))
    if evidenced != keys:
        raise ValueError("every version needs provenance")
    records = {(r["news_id"], r["record_sha256"]): r for r in package["versions"]}
    body_hashes = defaultdict(set)
    for row in package["versions"]:
        if row["body_sha256"]:
            body_hashes[row["news_id"]].add(row["body_sha256"])
    ids = set()
    for item in package["items"]:
        key = (item["news_id"], item["record_sha256"])
        if key not in keys or key[0] in ids:
            raise ValueError("invalid or duplicate release item")
        selected = min((STATES[records[(key[0], h)]["content_state"]], p, h) for p, h in choices[key[0]])[2]
        expected_flags = set(records[key]["quality_flags"])
        if len(body_hashes[key[0]]) > 1:
            expected_flags.add("conflicting_body_variants")
        if selected != key[1] or sorted(expected_flags) != item["quality_flags"]:
            raise ValueError("release selection differs from policy")
        ids.add(key[0])
    if ids != {k[0] for k in keys}:
        raise ValueError("release omits an observed news identity")
    if _counts(package) != manifest.get("counts"):
        raise ValueError("release counts mismatch")
    identity = digest(_identity(package))
    if identity != manifest.get("identity_sha256") or manifest.get("release_id") != f"news-{identity[:24]}":
        raise ValueError("release identity mismatch")
