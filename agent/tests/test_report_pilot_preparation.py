"""Offline source and privacy contracts for report-pilot data preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluation import prepare_report_pilot as pilot


@pytest.fixture
def source_package(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    cache_dir = tmp_path / "news/data/local_recall/articles-v1"
    cache_dir.mkdir(parents=True)
    sources, cases = [], []
    for event_id, split, _ in pilot.SELECTION:
        source_ids = []
        for index in range(2 if split == "development" else 1):
            source_id = f"{event_id}-{index}"
            source_ids.append(source_id)
            body = f"公开报道正文 {source_id}：营收100万元。"
            path = cache_dir / f"{source_id}.json"
            cache = {"news_id": source_id, "title": "报道标题", "url": f"https://news.qq.com/{source_id}",
                     "content": body}
            path.write_text(json.dumps(cache), encoding="utf-8")
            sources.append({**cache, "source_id": source_id, "published_at": "2026-08-01T00:00:00+08:00",
                            "cache_reference": str(path.relative_to(tmp_path)),
                            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                            "verification_status": "cache_only",
                            "facts": [{"fact_id": source_id + "-FACT", "statement": "PRIVATE_FACT_LABEL",
                                       "evidence_locator": "content.chars[0:2]"}]})
        for stage in ("research", "write", "review"):
            cases.append({"case_id": f"{event_id}-{stage}", "event_group_id": event_id,
                          "split": split, "task": stage,
                          "input": {"request": "根据材料整理并解读数据", "source_ids": source_ids,
                                    "audience": "public", "as_of": "2026-08-02T00:00:00+08:00",
                                    "numeric_inputs": {"hidden": "PRIVATE_NUMERIC_INPUT"}},
                          "expected": {"required_fact_ids": ["PRIVATE_FACT_ID"],
                                       "numeric_checks": [{"check_id": "PRIVATE_NUMERIC_LABEL"}],
                                       "required_caveats": ["PRIVATE_CAVEAT"],
                                       "forbidden_claims": ["PRIVATE_FORBIDDEN"]}})
    (source_dir / "seed.json").write_text(json.dumps({
        "dataset_version": "synthetic-source", "sources": sources, "cases": cases,
    }), encoding="utf-8")
    return tmp_path, source_dir, tmp_path / "new-pilot"


def test_preparation_whitelists_original_documents_and_keeps_labels_private(source_package):
    root, source, output = source_package
    manifest = pilot.prepare(output, source_dir=source, repo_root=root)
    raw = (output / "inputs.jsonl").read_text()
    assert "PRIVATE_" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 3
    assert set(rows[0]["input"]) == {"topic", "requirements"}
    requirements = rows[0]["input"]["requirements"]
    assert requirements["target_word_count"] == 600
    assert requirements["max_sections"] == 2
    assert set(requirements["source_documents"][0]) == {
        "source_id", "news_id", "url", "title", "published_at", "content",
    }
    assert "公开报道正文" in requirements["source_documents"][0]["content"]
    assert manifest["case_count"] == 3 and manifest["source_count"] == 5
    assert [case["structural_source_ready"] for case in manifest["cases"]] == [True, True, False]
    assert manifest["cases"][2]["structural_blockers"] == ["missing_second_source"]
    assert manifest["workflow_execution"] == manifest["scoring_status"] == "not_run"
    assert "PRIVATE_FACT_LABEL" in (output / "private/labels.draft.jsonl").read_text()
    assert "PRIVATE_NUMERIC_INPUT" in (output / "private/reviewer-probes.draft.jsonl").read_text()
    annotations = json.loads((output / "private/annotation-template.json").read_text())
    assert all(case["reviewer"] is None and case["critical_error_definition"] is None
               for case in annotations["cases"])
    for name, identity in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == identity["sha256"]


def test_hash_mismatch_blocks_before_creating_destination(source_package):
    root, source, output = source_package
    seed = json.loads((source / "seed.json").read_text())
    path = root / seed["sources"][0]["cache_reference"]
    cache = json.loads(path.read_text())
    cache["content"] += " changed"
    path.write_text(json.dumps(cache))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        pilot.prepare(output, source_dir=source, repo_root=root)
    assert not output.exists()


def test_existing_output_is_never_overwritten(source_package):
    root, source, output = source_package
    pilot.prepare(output, source_dir=source, repo_root=root)
    before = {str(path.relative_to(output)): path.read_bytes()
              for path in output.rglob("*") if path.is_file()}
    with pytest.raises(FileExistsError, match="output already exists"):
        pilot.prepare(output, source_dir=source, repo_root=root)
    assert before == {str(path.relative_to(output)): path.read_bytes()
                      for path in output.rglob("*") if path.is_file()}


def test_cache_reference_cannot_escape_allowed_directory(source_package):
    root, source, output = source_package
    seed_path = source / "seed.json"
    seed = json.loads(seed_path.read_text())
    seed["sources"][0]["cache_reference"] = "outside/never-read.json"
    seed_path.write_text(json.dumps(seed))
    with pytest.raises(ValueError, match="must remain under news/data/local_recall"):
        pilot.prepare(output, source_dir=source, repo_root=root)
    assert not output.exists()
