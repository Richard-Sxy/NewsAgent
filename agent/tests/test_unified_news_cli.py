"""CLI joins, paging and legacy-behavior scope remain tied to explicit releases."""
from argparse import Namespace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine

from app.modules.news_storage.model import build_package
from app.modules.news_storage.store import FrozenNewsStore
from app.modules.news_storage.tag_store import FrozenNewsTagStore
from app.modules.news_storage.topic_labels import build_tag_package


_SPEC = importlib.util.spec_from_file_location("unified_news_cli_under_test",
    Path(__file__).resolve().parents[1] / "tools" / "unify_news_storage.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)


def _candidate(news_id, title, body):
    return {"news_id": news_id, "title": title, "body": body,
        "url": f"https://news.qq.com/rain/a/{news_id}", "publish_time": "2026-10-13T00:00:00+08:00",
        "content_type": "article", "content_state": "body_available", "fragments": [], "quality_flags": [],
        "priority": 10, "source": {"package": "cli-fixture", "path": f"fixture/{news_id}.json",
        "file_sha256": "a" * 64, "manifest_path": None, "manifest_sha256": None,
        "source_content_version": "1", "fetched_at": None}}


@pytest.fixture
def joined(tmp_path):
    policy = {"schema_version": 1, "policy_version": "policy-cli-v1",
        "catalog": {"schema_version": 1, "taxonomy_version": "topics-cli-v1", "status": "draft",
            "topics": [{"topic_id": "tech", "name": "科技", "provenance": "user_confirmed"},
                       {"topic_id": "finance", "name": "财经", "provenance": "user_confirmed"}]},
        "rules": [{"rule_id": "tech-v1", "topic_id": "tech", "title_terms": ["科技"]},
                  {"rule_id": "finance-v1", "topic_id": "finance", "title_terms": ["财经"]}]}
    binding = {"package_version": "behavior-tags-local-v1", "manifest_sha256": "a" * 64,
        "identity_sha256": "b" * 64, "policy_sha256": "c" * 64, "content_labels_sha256": "d" * 64,
        "taxonomy_version": "topics-cli-v1", "policy_version": "policy-cli-v1"}
    package = build_package([_candidate("a", "财经报道", "财经内容" * 30),
        _candidate("b", "财经快讯", "财经内容" * 30), _candidate("c", "科技新闻", "科技正文" * 30),
        _candidate("d", "科技快讯", "科技正文" * 30)], tenant_id="content-tenant")
    engine = create_engine(f"sqlite:///{tmp_path / 'cli.sqlite'}")
    store = FrozenNewsStore(engine)
    store.initialize()
    store.import_release(package)
    tagged = FrozenNewsTagStore(store)
    tagged.initialize()
    topic_package = build_tag_package(package, {"policy": policy, "labels": [], "binding": binding})
    tagged.import_release(topic_package)
    args = Namespace(tenant_id="content-tenant", release_id=package["manifest"]["release_id"],
        tag_release_id=topic_package["manifest"]["tag_release_id"], topic=["tech"], body_only=True)
    yield store, args
    engine.dispose()


def test_list_cli_applies_topic_before_limit_and_preserves_next_cursor(joined, monkeypatch, capsys):
    store, args = joined
    monkeypatch.setattr(cli, "_database", lambda _: (store.engine, store))
    argv = ["unify_news_storage.py", "list", "--tenant-id", args.tenant_id,
        "--release-id", args.release_id, "--tag-release-id", args.tag_release_id,
        "--topic", "tech", "--body-only", "--limit", "1"]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    first = json.loads(capsys.readouterr().out)
    assert [row["news_id"] for row in first["records"]] == ["c"]
    assert first["records"][0]["tags"]["topic_names"] == ["科技"]
    assert "body" not in first["records"][0]
    assert first["next_after_news_id"] == "c"
    monkeypatch.setattr(sys, "argv", argv + ["--after-news-id", first["next_after_news_id"]])
    cli.main()
    second = json.loads(capsys.readouterr().out)
    assert [row["news_id"] for row in second["records"]] == ["d"]


def test_export_returns_frozen_tags_and_topic_filtered_source_material(joined, tmp_path):
    store, args = joined
    args.output = tmp_path / "tagged.jsonl"
    args.min_body_chars = 100
    args.exclude_metadata_review = True
    result = cli.export_records(store, args)
    raw = args.output.read_bytes()
    records = [json.loads(row) for row in raw.splitlines()]
    assert [row["news_id"] for row in records] == ["c", "d"]
    assert all(row["body"] and row["tags"]["topic_ids"] == ["tech"] for row in records)
    assert all(row["tags"]["tag_release_id"] == args.tag_release_id for row in records)
    assert result["sha256"] == sha256(raw).hexdigest()
    assert result["artifact_kind"] == "source_material_with_inferred_topics_not_evaluation_gold"
    with pytest.raises(FileExistsError):
        cli.export_records(store, args)
    assert args.output.read_bytes() == raw


def test_export_advances_past_entire_excluded_page_and_preserves_existing_file(tmp_path, monkeypatch):
    records = [{"news_id": f"n{i:04d}", "body": "短", "selection_quality_flags": [],
                "tags": {"topic_ids": ["tech"]}} for i in range(500)]
    records += [{"news_id": "n0500", "body": "待核查正文" * 30,
                 "selection_quality_flags": ["metadata_review_required"], "tags": {"topic_ids": []}},
                {"news_id": "n0501", "body": "科技正文" * 30,
                 "selection_quality_flags": [], "tags": {"topic_ids": ["tech"]}}]
    cursors = []
    class Pages:
        def summary(self, *scope):
            return {"status": "frozen"}

        def list_records(self, *scope, after_news_id, limit, body_only, topic_ids):
            cursors.append(after_news_id)
            assert topic_ids == ("tech",) and body_only is True
            if len(cursors) > 4:
                pytest.fail("export did not advance after a fully excluded page")
            return [r for r in records if r["news_id"] > after_news_id][:limit]
    monkeypatch.setattr(cli, "_tag_store", lambda _: Pages())
    args = Namespace(tenant_id="t", release_id="content", tag_release_id="tags", topic=["tech"],
        body_only=True, output=tmp_path / "paged.jsonl", min_body_chars=100, exclude_metadata_review=True)
    result = cli.export_records(object(), args)
    assert cursors == ["", "n0499", "n0501"]
    assert result["counts"] == {"excluded_short_body": 500, "excluded_metadata_review": 1, "exported": 1}
    assert [json.loads(line)["news_id"] for line in args.output.read_text().splitlines()] == ["n0501"]
    cursors.clear()
    with pytest.raises(FileExistsError):
        cli.export_records(object(), args)
    assert cursors == []


def test_failed_export_does_not_leave_partial_source_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_summary", lambda *_: {})
    calls = []
    def pages(*_, **kwargs):
        calls.append(kwargs["after_news_id"])
        if len(calls) == 1:
            return [{"news_id": "n1", "body": "one", "selection_quality_flags": []}]
        raise ValueError("simulated source integrity failure")
    monkeypatch.setattr(cli, "_list_records", pages)
    args = Namespace(tenant_id="t", release_id="r", output=tmp_path / "partial.jsonl",
                     body_only=False, min_body_chars=0, exclude_metadata_review=False)
    with pytest.raises(ValueError, match="integrity failure"):
        cli.export_records(object(), args)
    assert not args.output.exists()


def _behavior_args(**overrides):
    return Namespace(**({"tenant_id": "unified-tenant", "release_id": "content-release",
        "tag_release_id": "tag-release", "news_id": "news-1", "source_tenant_id": "source-b",
        "legacy_package": Path("not-opened-by-test"), "window_start": "2026-10-13T00:00:00+08:00",
        "window_end": "2026-10-14T00:00:00+08:00", "limit": 12} | overrides))


def test_behavior_without_matching_legacy_reference_never_opens_sqlite(monkeypatch):
    from app.modules.news_storage import legacy_tags
    monkeypatch.setattr(cli, "_get_record", lambda *_: {"tags": {"label_status": "tagged",
        "legacy_refs": [{"source_tenant_id": "source-a", "content_sha256": "b" * 64}]}})
    monkeypatch.setattr(cli, "_summary", lambda *_: pytest.fail("unavailable reference must not load summary"))
    monkeypatch.setattr(legacy_tags, "query_legacy_behavior", lambda *a, **k: pytest.fail("must not open SQLite"))
    result = cli._behavior(object(), _behavior_args())
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_verified_legacy_content_binding_for_source_tenant"
    assert result["rows"] == []


def test_behavior_metadata_review_rejected_before_reading_legacy(monkeypatch):
    from app.modules.news_storage import legacy_tags
    monkeypatch.setattr(cli, "_get_record", lambda *_: {"tags": {"label_status": "metadata_review"}})
    monkeypatch.setattr(legacy_tags, "query_legacy_behavior", lambda *a, **k: pytest.fail("must not open SQLite"))
    with pytest.raises(ValueError, match="metadata needs review"):
        cli._behavior(object(), _behavior_args())


def test_behavior_passes_explicit_original_tenant_exact_content_and_manifest_binding(monkeypatch):
    from app.modules.news_storage import legacy_tags
    monkeypatch.setattr(cli, "_get_record", lambda *_: {"tags": {"label_status": "tagged",
        "topic_ids": ["tech"], "legacy_refs": [
            {"source_tenant_id": "source-a", "content_sha256": "a" * 64},
            {"source_tenant_id": "source-b", "content_sha256": "b" * 64}]}})
    monkeypatch.setattr(cli, "_summary", lambda *_: {"legacy_source": {"manifest_sha256": "c" * 64}})
    calls = []
    def query(directory, **kwargs):
        calls.append((directory, kwargs))
        return {"status": "available", "source_mode": "synthetic", "rows": [{"unique_users": 3}]}
    monkeypatch.setattr(legacy_tags, "query_legacy_behavior", query)
    args = _behavior_args()
    result = cli._behavior(object(), args)
    assert calls == [(args.legacy_package, {"expected_manifest_sha256": "c" * 64,
        "source_tenant_id": "source-b", "news_id": "news-1", "content_sha256": "b" * 64,
        "window_start": args.window_start, "window_end": args.window_end, "limit": 12})]
    assert result["tenant_id"] == "unified-tenant"
    assert result["content_release_id"] == "content-release"
    assert result["tag_release_id"] == "tag-release"
    assert result["behavior"]["rows"] == [{"unique_users": 3}]


@pytest.mark.parametrize("command", ["list", "export", "behavior"])
def test_parser_requires_tag_release_for_topics_and_explicit_behavior_source_tenant(monkeypatch, command):
    argv = ["unify_news_storage.py", command, "--tenant-id", "t", "--release-id", "r"]
    if command == "list":
        argv += ["--topic", "tech"]
    elif command == "export":
        argv += ["--topic", "tech", "--output", "unused.jsonl"]
    else:
        argv += ["--tag-release-id", "tags", "--legacy-package", "unused", "--news-id", "n1",
                 "--window-start", "2026-10-13T00:00:00+08:00", "--window-end", "2026-10-14T00:00:00+08:00"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "_database", lambda _: pytest.fail("must validate options before connecting"))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
