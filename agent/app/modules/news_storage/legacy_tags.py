"""Read only the legacy topic artifacts and explicitly scoped simulated aggregates.

The personal event/profile artifacts are intentionally never opened. Legacy topic
labels are evidence to compare, not authoritative tags for a new content version.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
import stat

from app.modules.behavior_tags.tagging import BEHAVIOR_RULE_VERSION, TopicPolicy
from app.modules.news_storage.model import canonical_json
from app.modules.news_storage.package_io import _json


PACKAGE_VERSION = "behavior-tags-local-v1"
MAX_MANIFEST_BYTES = 1_048_576
MAX_CONTENT_BYTES = 134_217_728
MAX_LINE_BYTES = 2_097_152
MAX_SQLITE_BYTES = 4_294_967_296
_BEHAVIOR_FIELDS = frozenset("""tenant_id news_id content_sha256 source_content_sha256
source_content_version source_news_id source_dataset_version scenario_id metric_definition_id
metric_policy_version policy_version window_start window_end event_time snapshot_at
snapshot_revision completeness quality_status watermark_at uv_method baseline_version
sample_count baseline_clicks baseline_effective_consumptions baseline_impressions
baseline_interactions impressions clicks unique_users detail_page_views detail_visitors
direct_page_views total_duration_seconds effective_consumptions interactions likes comments
shares favorites follows negative_feedback video_starts video_completions label_ids reasons
behavior_rule_version behavior_label_scope behavior_labels_are_inferred metric_data_synthetic
""".split())


def _sha(value, name):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"invalid {name} SHA-256")
    return value


def _text(value, name, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"invalid {name}")
    return value


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("legacy query timestamps must be timezone-aware") from exc


def _regular(path: Path, maximum: int):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError("legacy artifact must be a bounded regular file")
    return info


def _manifest(directory):
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("legacy package must be a local non-symlink directory")
    directory = directory.resolve(strict=True)
    path = directory / "manifest.json"
    _regular(path, MAX_MANIFEST_BYTES)
    raw = path.read_bytes()
    manifest = _json(raw)
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("package_version") != PACKAGE_VERSION
            or manifest.get("generator_version") != "warehouse-guided-synthetic-events-v1"
            or manifest.get("behavior_rule_version") != BEHAVIOR_RULE_VERSION):
        raise ValueError("unsupported non-local legacy package/version")
    identity = dict(manifest)
    expected = _sha(identity.pop("identity_sha256", None), "manifest identity")
    if sha256(canonical_json(identity).encode()).hexdigest() != expected:
        raise ValueError("legacy manifest identity mismatch")
    tenants = manifest.get("tenants")
    if (not isinstance(tenants, list) or not tenants or len(tenants) > 32
            or any(not isinstance(x, str) or not x or len(x) > 128 for x in tenants)
            or len(set(tenants)) != len(tenants)):
        raise ValueError("invalid legacy tenant directory")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("missing legacy artifacts")
    for name in artifacts:
        if not isinstance(name, str) or Path(name).name != name or name in {".", "..", "manifest.json"}:
            raise ValueError("invalid legacy artifact name")
    window = manifest.get("source_window", {})
    if _time(window.get("window_start")) >= _time(window.get("window_end")):
        raise ValueError("invalid legacy source window")
    return directory, manifest, sha256(raw).hexdigest()


def _artifact(directory, manifest, name, maximum, *, read=False):
    metadata = manifest["artifacts"].get(name)
    if not isinstance(metadata, dict) or type(metadata.get("bytes")) is not int:
        raise ValueError(f"missing legacy artifact metadata: {name}")
    expected = _sha(metadata.get("sha256"), name)
    path = directory / name
    before = _regular(path, maximum)
    if before.st_size != metadata["bytes"]:
        raise ValueError(f"legacy artifact size mismatch: {name}")
    digest, chunks = sha256(), []
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if read:
                chunks.append(chunk)
    after = path.stat()
    if (digest.hexdigest() != expected or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError(f"legacy artifact checksum/change mismatch: {name}")
    return b"".join(chunks) if read else path


def _load(directory, manifest, manifest_sha):
    policy_raw = _artifact(directory, manifest, "policy.json", MAX_MANIFEST_BYTES, read=True)
    policy = TopicPolicy.model_validate(_json(policy_raw))
    if (policy.catalog.taxonomy_version != manifest.get("taxonomy_version")
            or policy.policy_version != manifest.get("policy_version")):
        raise ValueError("legacy policy/taxonomy version mismatch")
    raw = _artifact(directory, manifest, "content_labels.jsonl", MAX_CONTENT_BYTES, read=True)
    labels, keys = [], set()
    for line in raw.splitlines():
        if not line or len(line) > MAX_LINE_BYTES:
            raise ValueError("invalid legacy content-label line")
        row = _json(line)
        if not isinstance(row, dict):
            raise ValueError("legacy content label must be an object")
        tenant = _text(row.get("tenant_id"), "tenant_id", 128)
        news_id = _text(row.get("news_id"), "news_id")
        _text(row.get("title"), "title", 1024)
        _sha(row.get("content_sha256"), "content")
        if tenant not in manifest["tenants"] or (tenant, news_id) in keys:
            raise ValueError("legacy content tenant/duplicate mismatch")
        if (row.get("taxonomy_version") != policy.catalog.taxonomy_version
                or row.get("tagging_policy_version") != policy.policy_version
                or row.get("labels_are_inferred") is not True
                or row.get("provenance") not in {"rule_inferred", "synthetic_category_fallback", "unclassified"}):
            raise ValueError("legacy content label version/provenance mismatch")
        policy.catalog.validate_ids(row.get("topic_ids"))
        if not isinstance(row.get("topic_scores"), dict) or not isinstance(row.get("matches"), list):
            raise ValueError("invalid legacy topic evidence")
        keys.add((tenant, news_id))
        labels.append(row)
    count = manifest["artifacts"]["content_labels.jsonl"].get("rows")
    if (type(count) is not int or len(labels) != count
            or manifest.get("counts", {}).get("content_labels") != count):
        raise ValueError("legacy content label count mismatch")
    binding = {"package_version": PACKAGE_VERSION, "manifest_sha256": manifest_sha,
        "identity_sha256": manifest["identity_sha256"],
        "policy_sha256": manifest["artifacts"]["policy.json"]["sha256"],
        "content_labels_sha256": manifest["artifacts"]["content_labels.jsonl"]["sha256"],
        "taxonomy_version": policy.catalog.taxonomy_version, "policy_version": policy.policy_version}
    return {"policy": policy.model_dump(mode="json"), "labels": labels, "binding": binding}


def load_legacy_tags(directory) -> dict:
    """Verify only the manifest, policy and content labels; never personal rows."""
    return _load(*_manifest(directory))


def query_legacy_behavior(directory, *, expected_manifest_sha256, source_tenant_id,
                          news_id, content_sha256, window_start, window_end, limit=20) -> dict:
    """Return separate hourly/scenario aggregates with an explicit legacy tenant.

    No UV or other metric is summed. The source is a local simulation regardless
    of whether the associated article is a genuine public news article.
    """
    _sha(expected_manifest_sha256, "expected manifest")
    _sha(content_sha256, "content")
    _text(source_tenant_id, "source_tenant_id", 128)
    _text(news_id, "news_id")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("legacy query limit must be 1..100")
    start, end = _time(window_start), _time(window_end)
    directory, manifest, manifest_sha = _manifest(directory)
    if manifest_sha != expected_manifest_sha256:
        raise ValueError("legacy manifest differs from the frozen binding")
    window = manifest["source_window"]
    if start >= end or start < _time(window["window_start"]) or end > _time(window["window_end"]):
        raise ValueError("legacy source does not cover the requested time window")
    loaded = _load(directory, manifest, manifest_sha)
    result = {"status": "unavailable", "source_mode": "synthetic",
        "source_tenant_id": source_tenant_id, "news_id": news_id, "content_sha256": content_sha256,
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "binding": loaded["binding"], "rows": [], "truncated": False}
    if source_tenant_id not in manifest["tenants"]:
        return {**result, "reason": "source_tenant_not_in_package"}
    content = next((row for row in loaded["labels"]
                    if row["tenant_id"] == source_tenant_id and row["news_id"] == news_id), None)
    if content is None:
        return {**result, "reason": "news_not_in_legacy_package"}
    if content["content_sha256"] != content_sha256:
        return {**result, "reason": "content_hash_mismatch"}
    index = _artifact(directory, manifest, "label_index.sqlite", MAX_SQLITE_BYTES)
    before = index.stat()
    rows = []
    connection = sqlite3.connect(index.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_LINE_BYTES)
        indexed = connection.execute("SELECT payload_json FROM content_labels WHERE tenant_id=? AND news_id=?",
                                     (source_tenant_id, news_id)).fetchone()
        if indexed is None or sha256(canonical_json(_json(indexed[0])).encode()).hexdigest() != sha256(
                canonical_json(content).encode()).hexdigest():
            raise ValueError("legacy SQLite content payload differs from verified content JSONL")
        found = connection.execute("""SELECT window_start,window_end,source_dataset_version,
            scenario_id,metric_definition_id,payload_json FROM behavior_labels
            WHERE tenant_id=? AND news_id=? AND julianday(window_start)>=julianday(?)
            AND julianday(window_end)<=julianday(?)
            ORDER BY julianday(window_start),julianday(window_end),source_dataset_version,
                     scenario_id,metric_definition_id LIMIT ?""",
            (source_tenant_id, news_id, start.isoformat(), end.isoformat(), limit + 1)).fetchall()
        for a, b, dataset, scenario, definition, payload in found:
            row = _json(payload)
            expected = {"tenant_id": source_tenant_id, "news_id": news_id,
                "content_sha256": content_sha256, "window_start": a, "window_end": b,
                "source_dataset_version": dataset, "scenario_id": scenario,
                "metric_definition_id": definition, "behavior_rule_version": BEHAVIOR_RULE_VERSION,
                "behavior_label_scope": "news_window_aggregate", "behavior_labels_are_inferred": True}
            if (not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items())
                    or not start <= _time(a) < _time(b) <= end):
                raise ValueError("legacy behavior payload identity/window mismatch")
            rows.append({**{k: v for k, v in row.items() if k in _BEHAVIOR_FIELDS},
                "source_mode": "synthetic", "source_tenant_id": source_tenant_id,
                "source_package_sha256": manifest_sha})
    finally:
        connection.close()
    after = index.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("legacy SQLite changed during query")
    if not rows:
        return {**result, "reason": "no_aggregate_for_requested_window"}
    return {**result, "status": "available", "rows": rows[:limit], "truncated": len(rows) > limit}
