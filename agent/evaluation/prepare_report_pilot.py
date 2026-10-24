"""Prepare three archived-news report tasks offline; never run models or score."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).parent / "datasets/writing/data_writing_august_2026_v1"
DEFAULT_OUTPUT = Path(__file__).parent / "datasets/writing/report_pilot_v1"
SELECTION = (
    ("E01-TENCENT-Q2", "development", "腾讯二季度财报与数据口径解读"),
    ("E07-OPENAI-BUSINESS", "development", "OpenAI收入、亏损与上市计划解读"),
    ("E02-BANK-H1", "validation", "银行业半年盈利与净息差变化解读"),
)
BOUNDARY = (
    "仅依据给定缓存报道形成数据短报告，保留来源归属、统计期间和不确定性。"
    "缓存报道未经独立核真；八月发表元数据不代表八月当日留存版本。"
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def jsonl_bytes(rows: list[dict]) -> bytes:
    return ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                              allow_nan=False) + "\n" for row in rows)).encode("utf-8")


def read_source_document(source: dict, repo_root: Path) -> tuple[dict, dict]:
    allowed = (repo_root / "news/data/local_recall").resolve()
    path = (repo_root / source["cache_reference"]).resolve()
    if not path.is_relative_to(allowed) or path.suffix != ".json":
        raise ValueError("cache_reference must remain under news/data/local_recall")
    raw = path.read_bytes()
    cache = json.loads(raw)
    content = cache.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"empty cached content: {source['source_id']}")
    if sha256(content.encode("utf-8")) != source["content_sha256"]:
        raise ValueError(f"cached content SHA-256 mismatch: {source['source_id']}")
    for key in ("news_id", "url", "title"):
        if cache.get(key) != source[key]:
            raise ValueError(f"cached identity mismatch: {source['source_id']}/{key}")
    document = {key: source[key] for key in
                ("source_id", "news_id", "url", "title", "published_at")}
    document["content"] = content
    provenance = {
        "source_id": source["source_id"], "cache_reference": str(path.relative_to(repo_root.resolve())),
        "cache_sha256": sha256(raw), "content_sha256": source["content_sha256"],
        "content_characters": len(content), "fetched_at": source.get("fetched_at"),
        "verification_status": source["verification_status"],
    }
    return document, provenance


def prepare(output_dir: Path, *, source_dir: Path = SOURCE, repo_root: Path = ROOT) -> dict:
    """Positive-whitelist model inputs; all original labels stay under private/."""
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output already exists; choose a new directory: {output_dir}")
    seed_path = source_dir / "seed.json"
    seed_raw = seed_path.read_bytes()
    seed = json.loads(seed_raw)
    sources = {row["source_id"]: row for row in seed["sources"]}
    if len(sources) != len(seed["sources"]):
        raise ValueError("duplicate source IDs")
    case_ids = [row["case_id"] for row in seed["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate source case IDs")

    inputs, labels, probes, annotations, cases = [], [], [], [], []
    provenance_by_id = {}
    for event_id, split, topic in SELECTION:
        members = [row for row in seed["cases"] if row["event_group_id"] == event_id]
        stages = {row["task"]: row for row in members}
        if len(members) != 3 or set(stages) != {"research", "write", "review"}:
            raise ValueError(f"expected exactly three stage tasks for {event_id}")
        if any(row["split"] != split for row in members):
            raise ValueError(f"source split changed: {event_id}")
        research, writing, review = (stages[key] for key in ("research", "write", "review"))
        source_ids = research["input"]["source_ids"]
        if not source_ids or any(set(row["input"]["source_ids"]) != set(source_ids) for row in members):
            raise ValueError(f"inconsistent sources across stages: {event_id}")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(f"duplicate source references: {event_id}")
        if len({row["input"]["as_of"] for row in members}) != 1:
            raise ValueError(f"inconsistent as_of across stages: {event_id}")
        documents = []
        for source_id in source_ids:
            document, provenance = read_source_document(sources[source_id], repo_root)
            documents.append(document)
            provenance_by_id[source_id] = provenance
        case_id = f"{event_id}-REPORT"
        task_description = (BOUNDARY + "\n研究任务：" + research["input"]["request"]
                            + "\n写作任务：" + writing["input"]["request"])
        inputs.append({
            "case_id": case_id, "event_group_id": event_id, "split": split,
            "input": {"topic": topic, "requirements": {
                "language": "zh-CN", "audience": writing["input"]["audience"],
                "target_word_count": 600, "max_sections": 2,
                "as_of": writing["input"]["as_of"], "task_description": task_description,
                "source_documents": documents,
            }},
        })
        labels.append({
            "case_id": case_id, "event_group_id": event_id, "split": split,
            "status": "draft_pending_human_review", "model_input_allowed": False,
            "stage_labels": {stage: {"original_case_id": row["case_id"],
                                      "expected": deepcopy(row["expected"])}
                             for stage, row in stages.items()},
            "source_fact_gold": [{"source_id": source_id,
                                  "facts": deepcopy(sources[source_id]["facts"])}
                                 for source_id in source_ids],
        })
        probes.append({
            "case_id": review["case_id"], "report_case_id": case_id, "split": split,
            "status": "draft_pending_human_review", "model_input_allowed": False,
            "warning": "构造 ReviewInput 需要人工金标研究包；不能将本记录整条发给模型。",
            "input": deepcopy(review["input"]), "expected": deepcopy(review["expected"]),
        })
        annotations.append({
            "case_id": case_id, "event_group_id": event_id, "split": split,
            "reviewer": None, "reviewed_at": None, "status": "pending_human_review",
            "source_review_status": "cache_only_not_independently_verified",
            "required_facts_draft": sorted({fact_id for row in members
                                            for fact_id in row["expected"]["required_fact_ids"]}),
            "numeric_expectations_draft": [
                {"stage": stage, **deepcopy(check)} for stage, row in stages.items()
                for check in row["expected"]["numeric_checks"]
            ],
            "required_caveats_draft": sorted({value for row in members
                                              for value in row["expected"]["required_caveats"]}),
            "forbidden_claims_draft": sorted({value for row in members
                                             for value in row["expected"]["forbidden_claims"]}),
            "critical_error_definition": None, "editor_usable_definition": None,
            "gold_research_packet": None, "review_decisions": [], "notes": None,
        })
        cases.append({
            "case_id": case_id, "event_group_id": event_id, "split": split,
            "source_ids": source_ids, "source_count": len(documents),
            "structural_source_ready": len(documents) >= 2,
            "structural_blockers": [] if len(documents) >= 2 else ["missing_second_source"],
            "semantic_review_status": "pending_human_review",
            "execution_status": "not_run",
        })

    files = {
        "inputs.jsonl": jsonl_bytes(inputs),
        "private/labels.draft.jsonl": jsonl_bytes(labels),
        "private/reviewer-probes.draft.jsonl": jsonl_bytes(probes),
        "private/annotation-template.json": json_bytes({
            "status": "pending_human_review", "model_input_allowed": False,
            "instructions": "复核草案事实和数值，人工定义严重错误及可用标准；未填写不等于审核通过。",
            "cases": annotations,
        }),
    }
    manifest = {
        "schema_version": 1, "dataset_version": "report-pilot-v1",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft_pending_human_review", "model_inference_performed": False,
        "workflow_execution": "not_run", "scoring_status": "not_run",
        "scope": "full cached-news documents for Research/Writer/Reviewer report-pilot preparation only",
        "source_boundary": BOUNDARY, "historical_snapshot_verified": False,
        "independent_source_verification": False,
        "source_dataset": {"path": str(seed_path.resolve()), "sha256": sha256(seed_raw),
                           "dataset_version": seed["dataset_version"]},
        "case_count": len(cases), "event_group_count": len(cases),
        "source_count": len(provenance_by_id), "cases": cases,
        "sources": list(provenance_by_id.values()),
        "structural_readiness_definition": "At least two hash-verified cached documents; not semantic approval or runnable workflow acceptance.",
        "model_input_boundary": "Only each inputs.jsonl record.input; never private/, manifest or seed.",
        "files": {name: {"sha256": sha256(content), "bytes": len(content)}
                  for name, content in files.items()},
    }
    # Complete source checks before creating a new destination; never overwrite.
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "private").mkdir()
    for name, content in {**files, "manifest.json": json_bytes(manifest)}.items():
        with (output_dir / name).open("xb") as stream:
            stream.write(content)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        manifest = prepare(args.output_dir)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"Report pilot: {exc}\n")
    print(f"Prepared {manifest['case_count']} report tasks / {manifest['source_count']} cached sources: {args.output_dir.resolve()}")
    print("Models/workflows/scoring not run; semantic labels still require human review.")


if __name__ == "__main__":
    main()
