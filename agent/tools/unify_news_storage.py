#!/usr/bin/env python3
"""Freeze existing public news and import/read an explicitly selected database.

Run from repository root: agent/.venv/bin/python agent/tools/unify_news_storage.py --help
No .env discovery, enterprise behavior, crawling, embedding or evaluation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.news_storage.model import build_package, canonical_json
from app.modules.news_storage.package_io import load_package, save_package
from app.modules.news_storage.sources import load_existing_sources, source_summary


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _database(args):
    from sqlalchemy import create_engine
    from app.modules.news_storage.store import FrozenNewsStore

    url = os.environ.get(args.database_url_env)
    if not url:
        raise ValueError(f"set {args.database_url_env} explicitly; .env files are not read")
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    return engine, FrozenNewsStore(engine)


def export_records(store, args):
    """Export input material, not labels or an evaluation dataset."""
    _summary(store, args)  # fail before creating a file
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts, last_id, checksum = Counter(), "", sha256()
    # Exclusive creation prevents replacing an already-used evaluation input.
    with path.open("xb") as handle:
        try:
            while True:
                batch = _list_records(store, args, after_news_id=last_id, limit=500)
                if not batch:
                    break
                for record in batch:
                    if len(record["body"] or "") < args.min_body_chars:
                        counts["excluded_short_body"] += 1
                        continue
                    if args.exclude_metadata_review and "metadata_review_required" in record["selection_quality_flags"]:
                        counts["excluded_metadata_review"] += 1
                        continue
                    line = (canonical_json(record) + "\n").encode("utf-8")
                    handle.write(line)
                    checksum.update(line)
                    counts["exported"] += 1
                last_id = batch[-1]["news_id"]
        except BaseException:
            handle.close()
            path.unlink(missing_ok=True)
            raise
    return {"tenant_id": args.tenant_id, "release_id": args.release_id,
            "tag_release_id": getattr(args, "tag_release_id", None),
            "output": str(path), "sha256": checksum.hexdigest(), "counts": dict(counts),
            "filters": {"body_only": args.body_only, "min_body_chars": args.min_body_chars,
                        "exclude_metadata_review": args.exclude_metadata_review,
                        "topic_ids": getattr(args, "topic", [])},
            "artifact_kind": "source_material_with_inferred_topics_not_evaluation_gold" if getattr(args, "tag_release_id", None)
                             else "source_material_not_evaluation_labels"}


def _tag_store(store):
    from app.modules.news_storage.tag_store import FrozenNewsTagStore
    return FrozenNewsTagStore(store)


def _summary(store, args):
    if getattr(args, "tag_release_id", None):
        return _tag_store(store).summary(args.tenant_id, args.release_id, args.tag_release_id)
    return store.summary(args.tenant_id, args.release_id)


def _list_records(store, args, *, after_news_id, limit):
    common = dict(after_news_id=after_news_id, limit=limit, body_only=getattr(args, "body_only", False))
    if getattr(args, "tag_release_id", None):
        return _tag_store(store).list_records(args.tenant_id, args.release_id, args.tag_release_id,
                                             topic_ids=tuple(getattr(args, "topic", [])), **common)
    if getattr(args, "topic", []):
        raise ValueError("--topic requires an explicit --tag-release-id")
    return store.list_records(args.tenant_id, args.release_id, **common)


def _get_record(store, args):
    if getattr(args, "tag_release_id", None):
        return _tag_store(store).get_record(args.tenant_id, args.release_id, args.tag_release_id, args.news_id)
    return store.get_record(args.tenant_id, args.release_id, args.news_id)


def _behavior(store, args):
    from app.modules.news_storage.legacy_tags import query_legacy_behavior

    record = _get_record(store, args)
    if record is None:
        raise ValueError("news_id is absent from the selected release")
    if record["tags"]["label_status"] == "metadata_review":
        raise ValueError("news metadata needs review before attaching aggregate behavior")
    refs = [r for r in record["tags"]["legacy_refs"] if r["source_tenant_id"] == args.source_tenant_id]
    if not refs:
        return {"status": "unavailable", "reason": "no_verified_legacy_content_binding_for_source_tenant",
                "news_id": args.news_id, "source_mode": "synthetic", "rows": []}
    summary = _summary(store, args)
    behavior = query_legacy_behavior(args.legacy_package,
        expected_manifest_sha256=summary["legacy_source"]["manifest_sha256"],
        source_tenant_id=args.source_tenant_id, news_id=args.news_id,
        content_sha256=refs[0]["content_sha256"], window_start=args.window_start,
        window_end=args.window_end, limit=args.limit)
    return {"tenant_id": args.tenant_id, "content_release_id": args.release_id,
            "tag_release_id": args.tag_release_id, "news_id": args.news_id,
            "topic_ids": record["tags"]["topic_ids"], "behavior": behavior}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    root_default = Path(__file__).resolve().parents[2]
    inventory = commands.add_parser("inventory", help="verify and count the existing public source caches")
    inventory.add_argument("--root", type=Path, default=root_default)
    build = commands.add_parser("build", help="create a new immutable content package")
    build.add_argument("--root", type=Path, default=root_default)
    build.add_argument("--tenant-id", required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-package", help="verify all files and content identities without a database")
    verify.add_argument("--package", type=Path, required=True)
    tags = commands.add_parser("build-tags", help="attach existing versioned text rules to the frozen public content")
    tags.add_argument("--package", type=Path, required=True)
    tags.add_argument("--legacy-package", type=Path, required=True)
    tags.add_argument("--output", type=Path, required=True)
    verify_tags = commands.add_parser("verify-tags", help="verify a topic package and optionally its content binding")
    verify_tags.add_argument("--tags-package", type=Path, required=True)
    verify_tags.add_argument("--package", type=Path)
    database_commands = {}
    for name in ("import", "import-tags", "summary", "get", "list", "export", "topics", "behavior"):
        sub = commands.add_parser(name)
        sub.add_argument("--database-url-env", default="UNIFIED_NEWS_DATABASE_URL")
        if name == "import":
            sub.add_argument("--package", type=Path, required=True)
            sub.add_argument("--initialize", action="store_true", help="create missing content tables/view only")
        elif name == "import-tags":
            sub.add_argument("--tags-package", type=Path, required=True)
            sub.add_argument("--initialize", action="store_true", help="create missing topic tables/view only")
        else:
            sub.add_argument("--tenant-id", required=True)
            sub.add_argument("--release-id", required=True)
            sub.add_argument("--tag-release-id", required=name in {"topics", "behavior"})
        database_commands[name] = sub
    database_commands["get"].add_argument("--news-id", required=True)
    listing = database_commands["list"]
    listing.add_argument("--after-news-id", default="")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--topic", action="append", default=[])
    listing.add_argument("--body-only", action="store_true")
    export = database_commands["export"]
    export.add_argument("--output", required=True)
    export.add_argument("--body-only", action="store_true")
    export.add_argument("--min-body-chars", type=int, default=0)
    export.add_argument("--exclude-metadata-review", action="store_true")
    export.add_argument("--topic", action="append", default=[])
    behavior = database_commands["behavior"]
    behavior.add_argument("--legacy-package", type=Path, required=True)
    behavior.add_argument("--news-id", required=True)
    behavior.add_argument("--source-tenant-id", required=True)
    behavior.add_argument("--window-start", type=datetime.fromisoformat, required=True)
    behavior.add_argument("--window-end", type=datetime.fromisoformat, required=True)
    behavior.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if getattr(args, "min_body_chars", 0) < 0:
        parser.error("--min-body-chars must be nonnegative")
    if getattr(args, "topic", []) and not getattr(args, "tag_release_id", None):
        parser.error("--topic requires --tag-release-id")
    if args.command in {"inventory", "build"}:
        candidates = load_existing_sources(args.root)
        inventory = source_summary(candidates)
        if args.command == "inventory":
            _print(inventory)
        else:
            package = build_package(candidates, tenant_id=args.tenant_id)
            wrapper = save_package(package, args.output, inventory=inventory)
            _print({"output": str(args.output), **wrapper})
        return
    if args.command == "verify-package":
        _print(load_package(args.package)["manifest"])
        return
    if args.command in {"build-tags", "verify-tags", "import-tags"}:
        from app.modules.news_storage.topic_labels import build_tag_package, load_tag_package, save_tag_package, validate_tag_package
        if args.command == "build-tags":
            from app.modules.news_storage.legacy_tags import load_legacy_tags
            package = build_tag_package(load_package(args.package), load_legacy_tags(args.legacy_package))
            _print({"output": str(args.output), **save_tag_package(package, args.output)})
            return
        tag_package = load_tag_package(args.tags_package)
        if args.command == "verify-tags":
            if args.package:
                validate_tag_package(tag_package, load_package(args.package))
            _print(tag_package["manifest"])
            return
    package = load_package(args.package) if args.command == "import" else None
    engine, store = _database(args)
    try:
        if args.command == "import":
            if args.initialize:
                store.initialize()
            _print(store.import_release(package))
        elif args.command == "import-tags":
            tags = _tag_store(store)
            if args.initialize:
                tags.initialize()
            _print(tags.import_release(tag_package))
        elif args.command == "summary":
            _print(_summary(store, args))
        elif args.command == "get":
            record = _get_record(store, args)
            if record is None:
                raise ValueError("news_id is absent from the selected release")
            _print(record)
        elif args.command == "list":
            rows = _list_records(store, args, after_news_id=args.after_news_id, limit=args.limit)
            records = []
            for row in rows:
                record = {key: row[key] for key in ("news_id", "title", "url", "publish_time", "content_state", "record_sha256")}
                if "tags" in row:
                    record["tags"] = {key: row["tags"][key] for key in (
                        "tag_release_id", "topic_ids", "topic_names", "label_status", "input_scope", "provenance")}
                records.append(record)
            _print({"records": records, "next_after_news_id": rows[-1]["news_id"] if rows else None})
        elif args.command == "topics":
            _print(_tag_store(store).get_topics(args.tenant_id, args.release_id, args.tag_release_id))
        elif args.command == "behavior":
            _print(_behavior(store, args))
        else:
            _print(export_records(store, args))
    finally:
        engine.dispose()


if __name__ == "__main__":
    # Database exceptions can contain SQL parameters or credential-bearing URLs.
    # Print only the exception class; integrity failures use explicit ValueError.
    try:
        main()
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"operation failed ({type(exc).__name__}); inspect database connectivity and schema", file=sys.stderr)
        sys.exit(1)
