"""Versioned topic labels attached to a frozen content release, not gold labels."""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
import re
import shutil
import tempfile

from app.modules.behavior_tags import tagging
from app.modules.behavior_tags.tagging import TopicPolicy, tag_article
from app.modules.audience import taxonomy
from app.modules.news_storage.model import canonical_json, digest, validate_package
from app.modules.news_storage.package_io import _json


SCHEMA = "unified-news-topics-v1"
DERIVATION = "verified-text-only-topics-v1"
STATUSES = {"tagged", "unclassified", "insufficient_input", "metadata_review"}
LABEL_KEYS = {"news_id", "record_sha256", "body_sha256", "label_status", "input_scope",
              "topic_ids", "topic_scores", "matches", "provenance", "labels_are_inferred",
              "legacy_refs", "label_sha256"}
REF_KEYS = {"source_tenant_id", "news_id", "content_sha256", "label_sha256", "provenance",
            "compatible", "topic_ids"}


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _identity(package):
    manifest = {k: v for k, v in package["manifest"].items()
                if k not in {"identity_sha256", "tag_release_id", "counts"}}
    return {"manifest": manifest, "policy": package["policy"], "labels": package["labels"]}


def _counts(rows):
    counts = Counter(row["label_status"] for row in rows)
    return {"news_count": len(rows), **{f"{state}_count": counts[state] for state in sorted(STATUSES)},
            "legacy_verified_count": sum(r["provenance"] == "legacy_verified" for r in rows),
            "with_legacy_refs_count": sum(bool(r["legacy_refs"]) for r in rows),
            "topic_memberships_count": sum(len(r["topic_ids"]) for r in rows)}


def _selected(content_package):
    versions = {(r["news_id"], r["record_sha256"]): r for r in content_package["versions"]}
    return {item["news_id"]: (versions[(item["news_id"], item["record_sha256"])], item)
            for item in content_package["items"]}


def _scope(record, item):
    if "metadata_review_required" in item["quality_flags"]:
        return "metadata_review", "none"
    if not record["title"] or record["content_state"] == "fragments_only":
        return "insufficient_input", "none"
    if len(record["title"]) > 1024 or len(record["body"] or "") > 1_000_000:
        return "insufficient_input", "none"
    return None, "title_body" if record["body"] is not None else "title_only"


def build_tag_package(content_package: dict, legacy: dict) -> dict:
    validate_package(content_package)
    policy = TopicPolicy.model_validate(legacy["policy"])
    old_by_id = defaultdict(list)
    for row in legacy["labels"]:
        if row.get("news_content_synthetic") is False:
            old_by_id[row["news_id"]].append(row)
    rows = []
    tenant_id = content_package["manifest"]["tenant_id"]
    for news_id, (record, item) in sorted(_selected(content_package).items()):
        blocked, scope = _scope(record, item)
        if blocked:
            topics, scores, matches = [], {}, []
            status, provenance = blocked, blocked
        else:
            # No simulated category is supplied. Only actual captured text can
            # produce topics; fragments with missing metadata are not stitched.
            result = tag_article({"tenant_id": tenant_id, "news_id": news_id,
                                  "title": record["title"], "content": record["body"] or ""}, policy)
            topics, scores, matches = result["topic_ids"], result["topic_scores"], result["matches"]
            status = "tagged" if topics else "unclassified"
            provenance = "rule_inferred" if topics else "unclassified"
        refs = []
        input_body_hash = sha256((record["body"] or "").encode()).hexdigest()
        for old in old_by_id[news_id]:
            # Only exact text versions are linked; do not attach behavior for a
            # stale body, nor infer tenant equivalence from a matching ID alone.
            if old["content_sha256"] != input_body_hash or old["title"] != record["title"]:
                continue
            compatible = bool(not blocked and old["provenance"] == "rule_inferred"
                and old["topic_ids"] == topics and old["topic_scores"] == scores and old["matches"] == matches
                and old["taxonomy_version"] == policy.catalog.taxonomy_version
                and old["tagging_policy_version"] == policy.policy_version)
            refs.append({"source_tenant_id": old["tenant_id"], "news_id": news_id,
                         "content_sha256": old["content_sha256"], "label_sha256": digest(old),
                         "provenance": old["provenance"], "compatible": compatible,
                         "topic_ids": old["topic_ids"]})
        refs.sort(key=lambda r: (r["source_tenant_id"], r["label_sha256"]))
        if status == "tagged" and any(ref["compatible"] for ref in refs):
            provenance = "legacy_verified"
        row = {"news_id": news_id, "record_sha256": record["record_sha256"],
               "body_sha256": record["body_sha256"], "label_status": status, "input_scope": scope,
               "topic_ids": topics, "topic_scores": scores, "matches": matches,
               "provenance": provenance, "labels_are_inferred": True, "legacy_refs": refs}
        row["label_sha256"] = digest(row)
        rows.append(row)
    content_manifest = content_package["manifest"]
    code_hash = digest({name: sha256(Path(path).read_bytes()).hexdigest() for name, path in (
        ("tagger", tagging.__file__), ("taxonomy", taxonomy.__file__), ("derivation", __file__))})
    package = {"manifest": {
        "tenant_id": tenant_id, "content_release_id": content_manifest["release_id"],
        "content_identity_sha256": content_manifest["identity_sha256"], "schema_version": SCHEMA,
        "derivation_version": DERIVATION, "policy_version": policy.policy_version,
        "taxonomy_version": policy.catalog.taxonomy_version, "tagger_sha256": code_hash,
        "legacy_source": legacy["binding"],
    }, "policy": policy.model_dump(mode="json"), "labels": rows}
    identity = digest(_identity(package))
    package["manifest"].update(identity_sha256=identity, tag_release_id=f"tags-{identity[:24]}", counts=_counts(rows))
    validate_tag_package(package, content_package, verify_derivation=False)
    return package


def validate_label(row: dict, policy: TopicPolicy, record=None, item=None, *, verify_derivation=False) -> None:
    if not isinstance(row, dict) or set(row) != LABEL_KEYS:
        raise ValueError("invalid topic label fields")
    if row["labels_are_inferred"] is not True or row["label_status"] not in STATUSES:
        raise ValueError("invalid topic label state")
    if not isinstance(row["news_id"], str) or not 1 <= len(row["news_id"]) <= 160:
        raise ValueError("invalid topic news identity")
    if not _hash(row["record_sha256"]) or (row["body_sha256"] is not None and not _hash(row["body_sha256"])):
        raise ValueError("invalid topic content hash")
    if digest({k: v for k, v in row.items() if k != "label_sha256"}) != row["label_sha256"]:
        raise ValueError("topic label/hash mismatch")
    if not isinstance(row["topic_ids"], list) or row["topic_ids"] != sorted(set(row["topic_ids"])):
        raise ValueError("topics must be sorted unique IDs")
    policy.catalog.validate_ids(row["topic_ids"])
    if not isinstance(row["matches"], list) or not isinstance(row["legacy_refs"], list):
        raise ValueError("invalid topic evidence")
    rules = {r.rule_id: r for r in policy.rules}
    expected_scores = {}
    for match in row["matches"]:
        if set(match) != {"topic_id", "rule_id", "field", "term", "start", "end", "score", "source"}:
            raise ValueError("invalid match fields")
        rule = rules.get(match["rule_id"])
        if rule is None or rule.topic_id != match["topic_id"] or match["source"] != "rule_keyword_match":
            raise ValueError("unknown or simulated tag rule")
        field = match["field"]
        if field not in {"title", "content"}:
            raise ValueError("only captured text can provide topics")
        terms, weight = (rule.title_terms, rule.title_weight) if field == "title" else (rule.body_terms, rule.body_weight)
        if match["term"] not in terms or match["score"] != weight:
            raise ValueError("match differs from topic policy")
        start, end = match["start"], match["end"]
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ValueError("invalid match offsets")
        if row["input_scope"] == "title_only" and field != "title":
            raise ValueError("title-only labels cannot claim body evidence")
        if record is not None:
            value = record["title"] if field == "title" else record["body"] or ""
            if value is None or end > len(value) or value[start:end].casefold() != match["term"].casefold():
                raise ValueError("tag evidence does not match frozen text")
        for topic in policy.catalog.ancestor_ids(rule.topic_id):
            expected_scores[topic] = max(expected_scores.get(topic, 0), weight)
    if row["topic_scores"] != expected_scores or row["topic_ids"] != sorted(expected_scores):
        raise ValueError("topics differ from evidence")
    state, scope, provenance = row["label_status"], row["input_scope"], row["provenance"]
    if state == "tagged":
        if not row["topic_ids"] or scope not in {"title_body", "title_only"} or provenance not in {"legacy_verified", "rule_inferred"}:
            raise ValueError("invalid tagged record")
    elif row["topic_ids"] or row["matches"] or provenance != state:
        raise ValueError("empty or blocked labels must not have inferred topics")
    if (state in {"insufficient_input", "metadata_review"} and scope != "none") or (state == "unclassified" and scope not in {"title_body", "title_only"}):
        raise ValueError("invalid topic input scope")
    refs_seen = set()
    for ref in row["legacy_refs"]:
        if set(ref) != REF_KEYS or ref["news_id"] != row["news_id"] or type(ref["compatible"]) is not bool:
            raise ValueError("invalid legacy tag reference")
        if not isinstance(ref["source_tenant_id"], str) or not ref["source_tenant_id"] or not _hash(ref["label_sha256"]) or not _hash(ref["content_sha256"]):
            raise ValueError("invalid legacy reference identity")
        policy.catalog.validate_ids(ref["topic_ids"])
        if ref["content_sha256"] != (row["body_sha256"] or sha256(b"").hexdigest()):
            raise ValueError("legacy tag body differs from current content")
        key = (ref["source_tenant_id"], ref["label_sha256"])
        if key in refs_seen:
            raise ValueError("duplicate legacy reference")
        refs_seen.add(key)
        if ref["compatible"] and (ref["provenance"] != "rule_inferred" or ref["topic_ids"] != row["topic_ids"] or state != "tagged"):
            raise ValueError("incompatible legacy label declared verified")
    if (provenance == "legacy_verified") != any(r["compatible"] for r in row["legacy_refs"]):
        raise ValueError("legacy verification status differs from evidence")
    if record is not None:
        if any(row[k] != record[k] for k in ("news_id", "record_sha256", "body_sha256")):
            raise ValueError("label belongs to another content record")
        blocked, expected_scope = _scope(record, item)
        if scope != expected_scope or (blocked is not None and state != blocked):
            raise ValueError("label state differs from frozen content eligibility")
        if verify_derivation and blocked is None:
            expected = tag_article({"tenant_id": "derivation-verification", "news_id": row["news_id"],
                                    "title": record["title"], "content": record["body"] or ""}, policy)
            if any(row[k] != expected[k] for k in ("topic_ids", "topic_scores", "matches")):
                raise ValueError("topic labels differ from complete text-rule derivation")
            if state != ("tagged" if expected["topic_ids"] else "unclassified"):
                raise ValueError("topic label state differs from rule derivation")


def validate_tag_package(package: dict, content_package: dict | None = None, *, verify_derivation=True) -> None:
    if set(package) != {"manifest", "policy", "labels"}:
        raise ValueError("invalid tag package")
    manifest = package["manifest"]
    if set(manifest) != {"tenant_id", "content_release_id", "content_identity_sha256", "schema_version",
        "derivation_version", "policy_version", "taxonomy_version", "tagger_sha256", "legacy_source",
        "identity_sha256", "tag_release_id", "counts"}:
        raise ValueError("invalid tag manifest")
    if manifest["schema_version"] != SCHEMA or manifest["derivation_version"] != DERIVATION:
        raise ValueError("unsupported tag schema")
    for key in ("tenant_id", "content_release_id"):
        if not isinstance(manifest[key], str) or not 1 <= len(manifest[key]) <= 160:
            raise ValueError("invalid tag release binding")
    if not _hash(manifest["content_identity_sha256"]) or not _hash(manifest["tagger_sha256"]):
        raise ValueError("invalid tag provenance hash")
    policy = TopicPolicy.model_validate(package["policy"])
    if policy.policy_version != manifest["policy_version"] or policy.catalog.taxonomy_version != manifest["taxonomy_version"]:
        raise ValueError("tag policy version mismatch")
    source = manifest["legacy_source"]
    if not isinstance(source, dict) or set(source) != {"package_version", "manifest_sha256", "identity_sha256",
        "policy_sha256", "content_labels_sha256", "taxonomy_version", "policy_version"}:
        raise ValueError("invalid legacy source fields")
    if source.get("package_version") != "behavior-tags-local-v1" or source.get("policy_version") != policy.policy_version or source.get("taxonomy_version") != policy.catalog.taxonomy_version:
        raise ValueError("legacy policy binding mismatch")
    if any(not _hash(source.get(k)) for k in ("manifest_sha256", "identity_sha256", "policy_sha256", "content_labels_sha256")):
        raise ValueError("invalid legacy source hash")
    selected = None
    if content_package is not None:
        validate_package(content_package)
        cm = content_package["manifest"]
        if (manifest["tenant_id"], manifest["content_release_id"], manifest["content_identity_sha256"]) != (cm["tenant_id"], cm["release_id"], cm["identity_sha256"]):
            raise ValueError("tag package is bound to a different content release")
        selected = _selected(content_package)
    ids = []
    for row in package["labels"]:
        if selected is not None:
            if row["news_id"] not in selected:
                raise ValueError("tag package contains an unknown news identity")
            record, item = selected[row["news_id"]]
        else:
            record, item = None, None
        validate_label(row, policy, record, item, verify_derivation=verify_derivation)
        ids.append(row["news_id"])
    if not ids or ids != sorted(set(ids)) or (selected is not None and set(ids) != set(selected)):
        raise ValueError("tags must cover every selected news identity exactly once")
    if manifest["counts"] != _counts(package["labels"]):
        raise ValueError("tag counts mismatch")
    identity = digest(_identity(package))
    if identity != manifest["identity_sha256"] or manifest["tag_release_id"] != f"tags-{identity[:24]}":
        raise ValueError("tag release identity mismatch")


def save_tag_package(package: dict, output: Path) -> dict:
    validate_tag_package(package)
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError("tag output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    reserved, published, moved = False, False, []
    try:
        with (staging / "labels.jsonl").open("xb") as stream:
            for row in package["labels"]:
                stream.write((canonical_json(row) + "\n").encode())
        (staging / "policy.json").write_text(canonical_json(package["policy"]) + "\n", encoding="utf-8")
        files = {}
        for name in ("labels.jsonl", "policy.json"):
            raw = (staging / name).read_bytes()
            files[name] = {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
        wrapper = {"release": package["manifest"], "files": files}
        (staging / "manifest.json").write_text(canonical_json(wrapper) + "\n", encoding="utf-8")
        load_tag_package(staging)
        output.mkdir()
        reserved = True
        for name in ("labels.jsonl", "policy.json", "manifest.json"):
            (staging / name).rename(output / name)
            moved.append(name)
        published = True
        return wrapper
    finally:
        if reserved and not published:
            for name in moved:
                (output / name).unlink(missing_ok=True)
            try:
                output.rmdir()
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)


def load_tag_package(directory: Path) -> dict:
    directory = Path(directory).resolve(strict=True)
    path = directory / "manifest.json"
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("invalid tag manifest file")
    wrapper = _json(path.read_bytes())
    if set(wrapper) != {"release", "files"} or set(wrapper["files"]) != {"policy.json", "labels.jsonl"}:
        raise ValueError("invalid tag files")
    payloads = {}
    for name, expected in wrapper["files"].items():
        member = directory / name
        if member.is_symlink() or member.stat().st_size != expected["bytes"]:
            raise ValueError("tag file size/path mismatch")
        raw = member.read_bytes()
        if sha256(raw).hexdigest() != expected["sha256"]:
            raise ValueError("tag file hash mismatch")
        payloads[name] = raw
    package = {"manifest": wrapper["release"], "policy": _json(payloads["policy.json"]),
               "labels": [_json(line) for line in payloads["labels.jsonl"].splitlines()]}
    validate_tag_package(package)
    return package
